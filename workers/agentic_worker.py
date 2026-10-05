#!/usr/bin/env python3
"""The job worker for bpmn/agentic-incident-triage.bpmn: all seven job
types in one process, dispatched by type. Four use a live TypeSafe System One
call (Choice, Noul, Noul, Score); the other three are deterministic code,
exactly as the design in the README ("Two kinds of step") says they should
be. A judgment is never faked with code, and code is never dressed up as a
judgment.

    enrich-event                 deterministic (Service Task)
    typesafe-classify-severity   TypeSafe Choice   (severity level)
    typesafe-match-pattern       TypeSafe Noul     (does incident match the
                                 code-selected candidate playbook?)
    auto-remediate                deterministic (Service Task)
    typesafe-verify-fix          TypeSafe Noul     (did the symptom clear?)
    generate-postmortem           deterministic (Service Task)
    typesafe-cluster-problem     TypeSafe Score    (similarity to each known
                                 problem cluster, ordered scale, best of N)

Fixtures (workers/fixtures/*.json) stand in for the telemetry/playbook-catalog
/problem-tracker systems a real deployment would call out to; the point of
this worker is the judgment <-> gate wiring, not those integrations.

Usage:
    python3 workers/agentic_worker.py --once   # drain whatever is queued, then exit
    python3 workers/agentic_worker.py          # poll continuously (Ctrl+C to stop)
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import common

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
POSTMORTEMS_DIR = os.path.join(os.path.dirname(__file__), "postmortems")
POLL_INTERVAL_S = 2

SEVERITY_CRITERIA = {
    "cosmetic": "No user-facing impact; purely internal or cosmetic (e.g. a log message, a UI label).",
    "minor": "Limited, low user impact: a small percentage of requests affected, or a non-critical feature degraded, with no error-rate trend upward.",
    "major": "Significant user impact: a meaningful fraction of requests failing or a core feature degraded, especially if the error rate is actively climbing.",
    "critical": "Severe, widespread impact: most or all users affected, a core revenue-path or safety-critical function is down or failing at high volume.",
}

CLUSTER_SCORE_CRITERIA = [
    "No resemblance to this known problem cluster.",
    "Weak or partial resemblance to this known problem cluster.",
    "Strong match to this known, named problem cluster.",
]

VERIFY_THRESHOLD = 0.7  # matches section 4's "set the acting threshold from observed outcomes" guidance


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return json.load(f)


def _tokenize(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _select_candidate(incident_text, candidates, text_field):
    """Deterministic (no model call) candidate pick: word overlap weighted by
    inverse document frequency across `candidates`, so words shared by every
    candidate (e.g. 'downstream', 'is') score near zero and words specific to
    one or two candidates (e.g. 'cache', 'queue') dominate the ranking. This
    is the "select instead of generate" pattern's code half — a real
    deployment would use embeddings/vector search here; IDF-weighted overlap
    is a dependency-free stand-in that behaves the same way for this fixture
    size."""
    incident_words = _tokenize(incident_text)
    doc_words = [_tokenize(c[text_field]) for c in candidates]
    n = len(candidates)

    vocab = set().union(*doc_words) if doc_words else set()
    idf = {w: math.log(n / sum(1 for d in doc_words if w in d)) for w in vocab}

    best_candidate, best_score = None, -1.0
    for candidate, words in zip(candidates, doc_words):
        score = sum(idf.get(w, 0.0) for w in (incident_words & words))
        if score > best_score:
            best_candidate, best_score = candidate, score
    return best_candidate


# ---- deterministic (Service Task) handlers --------------------------------

def h_enrich_event(job, api_key):
    description = job["Variables"].get("incidentDescription", "")
    words = _tokenize(description)       # whole words, so "key" does not match "keyboard"
    phrases = description.lower()
    if words & {"cache", "redis"} or "session store" in phrases:
        area = "cache-tier"
    elif words & {"queue", "backlog", "consumer"}:
        area = "queue-tier"
    elif words & {"web", "api", "checkout", "request", "requests"}:
        area = "web-tier"
    elif words & {"credential", "credentials", "key", "keys", "token", "tokens", "access"}:
        area = "identity"
    else:
        area = "unknown"
    return {
        "serviceAreaGuess": area,
        "enrichedAt": datetime.now(timezone.utc).isoformat(),
    }


def h_auto_remediate(job, api_key):
    playbooks = {p["id"]: p for p in load_fixture("playbooks.json")}
    playbook_id = job["Variables"].get("matchedPlaybookId")
    playbook = playbooks.get(playbook_id)
    if not playbook:
        return {"actionTaken": "none (no matched playbook)", "postActionTelemetry": "n/a"}

    action_taken = playbook["action"]
    # Simulate real-world variance: the action usually works, but not always.
    # This is what gives typesafe-verify-fix a genuine, non-trivial call to make.
    # A caller can pin the outcome with the optional `fixOutcome` variable
    # ("works" or "fails"); the viewer's third scenario uses this.
    forced = job["Variables"].get("fixOutcome")
    # Seeded by the job key, so if the engine hands this job out twice the outcome is the same.
    roll = random.Random(job["Key"]).random()
    works = (forced == "works") if forced in ("works", "fails") else roll < 0.8
    if works:
        telemetry = playbook["expectedOutcome"]
    else:
        telemetry = (
            "No significant change observed in the affected metric 5 minutes "
            "after the action; the original symptom is still present."
        )
    return {"actionTaken": action_taken, "postActionTelemetry": telemetry}


def h_generate_postmortem(job, api_key):
    v = job["Variables"]
    process_instance = job["Process Instance"]
    lines = [
        f"# Postmortem — process instance {process_instance}",
        "",
        f"**Incident:** {v.get('incidentDescription', '(none provided)')}",
        f"**Severity:** {v.get('severity', '?')} (confidence {v.get('severityConfidence', '?')})",
    ]
    if "matchedPlaybookId" in v:
        lines.append(
            f"**Pattern match:** {v['matchedPlaybookId']} "
            f"(confidence {v.get('matchConfidence', '?')}, reversible={v.get('reversible', '?')})"
        )
    if "actionTaken" in v:
        lines.append(f"**Action taken:** {v['actionTaken']}")
    if "postActionTelemetry" in v:
        lines.append(f"**Post-action signal:** {v['postActionTelemetry']}")
    if "verified" in v:
        lines.append(f"**Verified resolved:** {v['verified']} (probability {v.get('verifyProbability', '?')})")
    if "triageOutcome" in v:
        lines.append(f"**Human triage outcome:** {v['triageOutcome']}")
    # The record is written before the cluster check and the human CSI decision, so
    # those live in the process variables (rootCauseConfirmed, csiDecision), not here.

    os.makedirs(POSTMORTEMS_DIR, exist_ok=True)
    path = os.path.join(POSTMORTEMS_DIR, f"{process_instance}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return {"postmortemPath": os.path.relpath(path, common.REPO_ROOT)}


# ---- TypeSafe (Business Rule Task) handlers --------------------------------

def h_classify_severity(job, api_key):
    incident = job["Variables"].get("incidentDescription", "")
    choice, confidence, probabilities, model = common.typesafe_choice(
        "severity",
        "Given `incident`, what severity should this incident be classified as?",
        SEVERITY_CRITERIA,
        {"incident": incident, "service_area": job["Variables"].get("serviceAreaGuess", "unknown")},
        api_key,
    )
    print(f"    TypeSafe Choice ({model}): severity={choice} confidence={confidence} probs={probabilities}")
    return {"severity": choice, "severityConfidence": confidence, "severityProbabilities": probabilities}


def h_match_pattern(job, api_key):
    incident = job["Variables"].get("incidentDescription", "")
    playbooks = load_fixture("playbooks.json")
    candidate = _select_candidate(incident, playbooks, "symptoms")

    noul, model = common.typesafe_noul(
        "matches_playbook",
        "Given `incident` and `candidate_playbook_symptoms`, does the incident closely "
        "match the described symptom pattern, confidently enough to trust this specific "
        "remediation playbook?",
        {"incident": incident, "service_area": job["Variables"].get("serviceAreaGuess", "unknown"),
         "candidate_playbook_symptoms": candidate["symptoms"]},
        api_key,
    )
    print(f"    TypeSafe Noul ({model}): matchConfidence={noul} candidate={candidate['id']} "
          f"reversible={candidate['reversible']} (code-selected, code-sourced reversibility)")
    return {
        "matchConfidence": noul,
        "reversible": candidate["reversible"],
        "matchedPlaybookId": candidate["id"],
    }


def h_verify_fix(job, api_key):
    v = job["Variables"]
    noul, model = common.typesafe_noul(
        "resolved",
        "Given `incident`, `action_taken`, and `post_action_telemetry`, has the original "
        "symptom been resolved?",
        {
            "incident": v.get("incidentDescription", ""),
            "action_taken": v.get("actionTaken", ""),
            "post_action_telemetry": v.get("postActionTelemetry", ""),
        },
        api_key,
    )
    verified = noul >= VERIFY_THRESHOLD
    print(f"    TypeSafe Noul ({model}): verifyProbability={noul} -> verified={verified} "
          f"(threshold {VERIFY_THRESHOLD})")
    return {"verified": verified, "verifyProbability": noul}


def h_cluster_problem(job, api_key):
    v = job["Variables"]
    incident_summary = v.get("incidentDescription", "") + " " + v.get("postActionTelemetry", "")
    clusters = load_fixture("problem_clusters.json")

    # One request, one Score question per cluster: the questions are independent and read
    # the same incident, so they are asked together (one round trip, well inside the job timeout).
    state = {"incident_summary": incident_summary}
    questions = {}
    for n, cluster in enumerate(clusters):
        state[f"cluster_{n}"] = cluster["description"]
        questions[f"similarity_{n}"] = {
            "type": "score",
            "instructions": f"How similar is `incident_summary` to `cluster_{n}`?",
            "criteria": CLUSTER_SCORE_CRITERIA,
        }
    answers, model = common.typesafe_ask(state, questions, api_key)

    all_scores, best = {}, None   # best = (normalized, cluster)
    for n, cluster in enumerate(clusters):
        ans = answers[f"similarity_{n}"]
        normalized = ans["score"] / (len(CLUSTER_SCORE_CRITERIA) - 1)
        all_scores[cluster["id"]] = round(normalized, 3)
        print(f"    TypeSafe Score ({model}): cluster={cluster['id']} score={ans['score']:.2f} "
              f"(normalized {normalized:.2f}) confidence={ans.get('confidence')}")
        if best is None or normalized > best[0]:
            best = (normalized, cluster)

    normalized, cluster = best
    # A strong match to a cluster that means "not a recurring pattern" is a reason NOT to
    # raise a problem record. It must never open the gate that asks a person to prioritise.
    if not cluster.get("recurring", True):
        print(f"    best match is {cluster['id']}, which is not a recurring problem: clusterScore forced to 0")
        normalized = 0.0
    return {"clusterScore": normalized, "matchedClusterId": cluster["id"], "clusterScores": all_scores}


HANDLERS = {
    "enrich-event": h_enrich_event,
    "typesafe-classify-severity": h_classify_severity,
    "typesafe-match-pattern": h_match_pattern,
    "auto-remediate": h_auto_remediate,
    "typesafe-verify-fix": h_verify_fix,
    "generate-postmortem": h_generate_postmortem,
    "typesafe-cluster-problem": h_cluster_problem,
}


# The complete set of variables this process ever produces or consumes.
# c8ctl's --fetchVariable is an allow-list, not a default-to-all: omitting a
# name here means a handler downstream simply never sees it, silently (this
# bit a first version of this worker — generate-postmortem's human-triage
# fields went missing because they weren't in a shorter, ad hoc list).
ALL_VARIABLES = [
    "incidentDescription", "serviceAreaGuess", "enrichedAt",
    "severity", "severityConfidence",
    "matchConfidence", "reversible", "matchedPlaybookId",
    "actionTaken", "postActionTelemetry",
    "verified", "verifyProbability",
    "triageOutcome", "rootCauseConfirmed", "csiDecision",
    "postmortemPath", "clusterScore", "matchedClusterId",
    "fixOutcome", "severityProbabilities", "clusterScores",
]


def run_once(api_key):
    handled = 0
    for job_type, handler in HANDLERS.items():
        jobs = common.activate_jobs(job_type, fetch_variables=ALL_VARIABLES)
        for job in jobs:
            job_key = job["Key"]
            print(f"[worker] {job_type} job {job_key} (pi {job['Process Instance']})")
            try:
                result_vars = handler(job, api_key)
            except Exception as e:       # a failed judgment must fail the JOB, not kill the worker
                retries = max(int(job.get("Retries", 3)) - 1, 0)
                print(f"[worker] {job_type} job {job_key} failed: {e} (retries left: {retries})")
                try:
                    common.c8ctl("fail", "job", str(job_key), "--retries", str(retries),
                                 "--errorMessage", str(e)[:300])
                except RuntimeError as e2:
                    print(f"[worker] could not report the failure: {e2}")
                continue
            common.complete_job(job_key, result_vars)
            print(f"[worker] {job_type} job {job_key}: completed with {result_vars}")
            handled += 1
    return handled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Drain currently queued jobs once, then exit.")
    args = parser.parse_args()

    api_key = common.load_api_key()
    print("[worker] agentic_worker: handling " + ", ".join(HANDLERS.keys()))

    if args.once:
        handled = run_once(api_key)
        print(f"[worker] done, handled {handled} job(s)")
        return 0

    try:
        while True:
            try:
                handled = run_once(api_key)
            except Exception as e:
                # Transient engine or CLI trouble: say so, wait, try again.
                # An unfinished job is re-activated by the engine after its timeout.
                print(f"[worker] {e}; retrying in {POLL_INTERVAL_S * 3}s")
                time.sleep(POLL_INTERVAL_S * 3)
                continue
            if handled == 0:
                time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("\n[worker] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
