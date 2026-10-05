#!/usr/bin/env python3
"""Record ui/sample-recording.json from REAL runs against a live engine.

    python3 ui/record_sample.py

Needs the engine, the deployed model and the worker running (see README).
For each scenario in story.json it sends the alert, answers any human task
with the suggested text, waits for the instance to finish, and keeps the final
snapshot (which includes start/end times, so the page can replay it step by
step). The replay is exactly as real as the run that produced it.
"""
import json
import os
import sys
import time
from datetime import date

import lab

HERE = lab.HERE
STORY = json.load(open(os.path.join(HERE, "story.json")))


def run_scenario(key, sc):
    extra = {"fixOutcome": sc["fixOutcome"]} if sc.get("fixOutcome") else {}
    pi = lab.start_incident(sc["incident"], extra)
    print("scenario %s: instance %s" % (key, pi))
    deadline = time.time() + 180
    answered = set()   # the search index lags the engine by a moment; never answer a task twice
    while time.time() < deadline:
        snap = lab.snapshot(pi)
        if snap["tasks"] and snap["tasks"][0]["key"] not in answered:
            answered.add(snap["tasks"][0]["key"])
            el = snap["tasks"][0]["elementId"]
            h = STORY["human"][el]
            text = h["defaults"].get(key) or h["defaults"]["default"]
            time.sleep(1.0)
            lab.complete_human_task(pi, {"text": text, "confirm": True})
            print("  answered", el)
        if snap["status"] != "ACTIVE":
            return snap
        time.sleep(0.7)
    raise RuntimeError("scenario %s did not finish in 3 minutes (is the worker running?)" % key)


def main():
    if not lab.engine_up():
        sys.exit("Cannot reach the engine at %s" % lab.REST)
    out = {"meta": {"recorded": str(date.today()), "note": "Final snapshots of real runs against Camunda 8 with live TypeSafe judgments."},
           "model": {"flows": lab.MODEL["flows"]}, "scenarios": {}}
    for key, sc in STORY["scenarios"].items():
        snap = run_scenario(key, sc)
        print("  ->", snap["status"], "verified=%s severity=%s cluster=%s" % (snap["vars"].get("verified"), snap["vars"].get("severity"), snap["vars"].get("clusterScore")))
        out["scenarios"][key] = {"snapshot": snap}
    with open(os.path.join(HERE, "sample-recording.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("wrote ui/sample-recording.json")


if __name__ == "__main__":
    main()
