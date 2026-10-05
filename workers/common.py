"""Shared helpers for the agentic-incident-triage job worker: a thin
TypeSafe System One client and a thin c8ctl (Camunda 8 CLI) client. No Zeebe
SDK dependency: c8ctl's --json output is the whole integration surface.

Configuration comes from the environment, then from a `.env` file in the repo
root (see `.env.example`). Nothing is read from outside this repository.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
C8CTL_TIMEOUT_S = 30
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env")


def _dotenv():
    """Parse KEY=VALUE lines from the repo's .env. Missing file means empty."""
    values = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return values


def setting(name, default=""):
    """Environment variable first, then .env, then the default."""
    return os.getenv(name) or _dotenv().get(name) or default


def typesafe_model():
    """Read at call time, so a model pinned in .env is honoured."""
    return setting("TYPESAFE_MODEL", "jev-latest")


def load_api_key():
    key = setting("TYPESAFE_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "TYPESAFE_API_KEY is not set. Copy .env.example to .env, put your key "
        "after TYPESAFE_API_KEY=, or export it in your shell. See the README, "
        "section 'Give the lab your TypeSafe key'."
    )


def typesafe_ask(state, questions, api_key):
    """One System One call. `questions` is the {id: {type, instructions, criteria}}
    map exactly as the API expects. Returns the raw `answers` dict."""
    body = {"state": state, "model": typesafe_model(), "questions": questions}
    req = urllib.request.Request(
        TYPESAFE_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["answers"], data.get("model")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TypeSafe returned HTTP {e.code}"
                           + (" (check TYPESAFE_API_KEY)" if e.code in (401, 403) else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"TypeSafe call failed: {getattr(e, 'reason', e)}") from None
    except (ValueError, KeyError):
        raise RuntimeError("TypeSafe returned an answer in an unexpected shape") from None


def typesafe_choice(question_id, instructions, criteria, state, api_key):
    answers, model = typesafe_ask(state, {question_id: {
        "type": "choice", "instructions": instructions, "criteria": criteria,
    }}, api_key)
    ans = answers[question_id]
    return ans["choice"], ans["confidence"], ans.get("probabilities"), model


def typesafe_noul(question_id, instructions, state, api_key):
    """Returns a single probability in [0, 1] (near 1 = strong yes, near 0 =
    strong no, near 0.5 = ambiguous). No separate confidence field."""
    answers, model = typesafe_ask(state, {question_id: {
        "type": "noul", "instructions": instructions,
    }}, api_key)
    ans = answers[question_id]
    return ans["noul"], model


def typesafe_score(question_id, instructions, criteria, state, api_key):
    """`criteria` is an ORDERED list of level descriptions, e.g.
    ["low", "medium", "high"] -> levels 0, 1, 2. Returns
    (score 0..len(criteria)-1, confidence, probabilities-by-level, model)."""
    answers, model = typesafe_ask(state, {question_id: {
        "type": "score", "instructions": instructions, "criteria": criteria,
    }}, api_key)
    ans = answers[question_id]
    return ans["score"], ans.get("confidence"), ans.get("probabilities"), model


def c8ctl(*args):
    cmd = ["c8ctl", *args, "--json"]
    profile = setting("C8CTL_PROFILE")
    if profile:
        cmd += ["--profile", profile]
    try:
        # A hung c8ctl must never freeze the worker: every call has a deadline.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=C8CTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"c8ctl {' '.join(args)} timed out after {C8CTL_TIMEOUT_S}s")
    if result.returncode != 0:
        raise RuntimeError(f"c8ctl {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # Some c8ctl subcommands (e.g. `complete job`) don't emit JSON on
        # every path; the command still succeeded (returncode 0 checked above).
        return {"raw": result.stdout.strip()}


def activate_jobs(job_type, fetch_variables=None, max_jobs=5):
    args = ["activate", "jobs", job_type, "--maxJobsToActivate", str(max_jobs)]
    if fetch_variables:
        args += ["--fetchVariable", ",".join(fetch_variables)]
    result = c8ctl(*args)
    # When there's nothing to activate, c8ctl --json returns a status object,
    # not an empty array — normalize so callers can always iterate a list.
    return result if isinstance(result, list) else []


def complete_job(job_key, variables):
    return c8ctl("complete", "job", str(job_key), "--variables", json.dumps(variables))
