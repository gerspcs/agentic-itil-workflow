"""Engine client for the live viewer: reads a process instance from the
Camunda 8 REST API and turns it into one small JSON "state" the page draws.

Standard library only. Used by server.py (live viewer) and record_sample.py
(which records the demo replay from a real run).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "workers"))
import common  # noqa: E402  (shares .env handling with the worker)

REST = (common.setting("CAMUNDA_REST") or "http://localhost:8080/v2").rstrip("/")
BPMN_FILE = os.path.join(ROOT, "bpmn", "agentic-incident-triage.bpmn")
PROCESS_ID = "Process_Incident"
NS = {"b": "http://www.omg.org/spec/BPMN/20100524/MODEL",
      "z": "http://camunda.org/schema/zeebe/1.0"}

# What each human task writes back into the process (names the postmortem and
# the gateways read). Keys are BPMN element ids.
HUMAN_TASKS = {
    "Task_HumanTriage": "triageOutcome",
    "Task_EscalateHuman": "triageOutcome",
    "Task_ConfirmRootCause": "rootCauseConfirmed",
}


def call(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(REST + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def engine_up():
    try:
        call("GET", "/topology", timeout=3)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_model():
    """Static facts from the BPMN file: nodes, flows and the start message."""
    root = ET.parse(BPMN_FILE).getroot()
    proc = root.find("b:process", NS)
    nodes, flows = {}, []
    for el in proc:
        tag = el.tag.split("}")[1]
        if tag == "sequenceFlow":
            flows.append({"id": el.get("id"), "source": el.get("sourceRef"),
                          "target": el.get("targetRef"), "name": el.get("name") or ""})
        elif tag not in ("laneSet", "documentation", "extensionElements"):
            td = el.find(".//z:taskDefinition", NS)
            nodes[el.get("id")] = {"type": tag, "name": (el.get("name") or "").replace("\n", " "),
                                   "job": td.get("type") if td is not None else None}
    message = root.find("b:message", NS)
    return {"nodes": nodes, "flows": flows, "message": message.get("name") if message is not None else None}


MODEL = load_model()


def start_incident(text, extra=None):
    """Publish the alert message. Returns the new process instance key."""
    t0 = time.time()
    variables = {"incidentDescription": text, **(extra or {})}
    call("POST", "/messages/publication", {
        "name": MODEL["message"], "correlationKey": "viewer-%d" % int(t0 * 1000),
        "timeToLive": 0, "variables": variables})
    deadline = t0 + 15
    while time.time() < deadline:
        time.sleep(0.4)
        found = call("POST", "/process-instances/search", {
            "filter": {"processDefinitionId": PROCESS_ID},
            "sort": [{"field": "startDate", "order": "DESC"}], "page": {"limit": 1}})
        for item in found.get("items", []):
            started = item.get("startDate", "")
            if started and _epoch(started) >= t0 - 1:
                return item["processInstanceKey"]
    raise RuntimeError("The engine accepted the alert but no process instance started. "
                       "Is the process deployed? (scripts/lab.sh deploy)")


def _epoch(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def snapshot(pi):
    """One consistent picture of a process instance."""
    inst = call("GET", "/process-instances/" + str(pi))
    els = call("POST", "/element-instances/search", {
        "filter": {"processInstanceKey": str(pi)}, "page": {"limit": 200}}).get("items", [])
    vars_ = call("POST", "/variables/search", {
        "filter": {"processInstanceKey": str(pi)}, "page": {"limit": 200}}).get("items", [])
    uts = call("POST", "/user-tasks/search", {
        "filter": {"processInstanceKey": str(pi)}, "page": {"limit": 50}}).get("items", [])
    elements = {}
    for e in els:
        if e["elementId"] in MODEL["nodes"]:
            state = "active" if e["state"] == "ACTIVE" else "done"
            if elements.get(e["elementId"]) != "active":
                elements[e["elementId"]] = state
    variables = {}
    for v in vars_:
        try:
            variables[v["name"]] = json.loads(v["value"])
        except (ValueError, TypeError):
            variables[v["name"]] = v["value"]
    tasks = [{"key": u["userTaskKey"], "elementId": u["elementId"], "name": u["name"], "state": u["state"]}
             for u in uts if u["state"] in ("CREATED", "ASSIGNED")]
    incident = any(e.get("hasIncident") for e in els)
    # Start/end instants per element, so the page can walk through fast steps
    # one at a time instead of jumping straight to the end state.
    timeline = [{"id": e["elementId"], "start": int(_epoch(e["startDate"]) * 1000),
                 "end": int(_epoch(e["endDate"]) * 1000) if e.get("endDate") else None}
                for e in els if e["elementId"] in MODEL["nodes"]]
    return {"ok": True, "pi": str(pi), "status": inst["state"], "elements": elements,
            "vars": variables, "tasks": tasks, "hasIncident": incident, "timeline": timeline}


def complete_human_task(pi, answer):
    """Complete the one open human task of this instance. `answer` is the text
    (or yes/no) the person gave; the variable it lands in depends on the task."""
    snap = snapshot(pi)
    if not snap["tasks"]:
        raise RuntimeError("No human task is waiting right now.")
    task = snap["tasks"][0]
    var = HUMAN_TASKS.get(task["elementId"])
    if var is None:
        raise RuntimeError("Unknown human task " + task["elementId"])
    if var == "rootCauseConfirmed":
        variables = {"rootCauseConfirmed": bool(answer.get("confirm")),
                     "csiDecision": str(answer.get("text", ""))[:400]}
    else:
        variables = {"triageOutcome": str(answer.get("text", ""))[:400]}
    call("POST", "/user-tasks/%s/completion" % task["key"], {"variables": variables})
    return task["elementId"]
