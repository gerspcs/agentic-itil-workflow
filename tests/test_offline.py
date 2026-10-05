"""Offline tests: no engine, no network, no TypeSafe key.

    python3 -m unittest discover -s tests -v

They cover the deterministic half of the design: candidate selection, the
fix-outcome knob, configuration precedence, and that the viewer's story covers
every element of the BPMN model.
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "workers"))
sys.path.insert(0, os.path.join(ROOT, "ui"))

import agentic_worker as w  # noqa: E402
import common  # noqa: E402
import lab  # noqa: E402


class Selection(unittest.TestCase):
    def test_cache_incident_picks_cache_playbook(self):
        text = "Payment API returns HTTP 500. The Redis cache node backing the session store is pegged at 100% CPU."
        pick = w._select_candidate(text, w.load_fixture("playbooks.json"), "symptoms")
        self.assertEqual(pick["id"], "restart-cache-node")

    def test_shared_words_do_not_decide(self):
        # Words every playbook shares ("downstream", "is") carry no weight (the bug IDF weighting fixed).
        books = [{"id": "a", "t": "downstream is slow cache"}, {"id": "b", "t": "downstream is slow queue"}]
        self.assertEqual(w._select_candidate("downstream is slow queue", books, "t")["id"], "b")


class Enrich(unittest.TestCase):
    def test_service_area(self):
        out = w.h_enrich_event({"Variables": {"incidentDescription": "redis cache is down"}}, None)
        self.assertEqual(out["serviceAreaGuess"], "cache-tier")
        out = w.h_enrich_event({"Variables": {"incidentDescription": "something odd"}}, None)
        self.assertEqual(out["serviceAreaGuess"], "unknown")


class FixOutcome(unittest.TestCase):
    def job(self, outcome):
        v = {"matchedPlaybookId": "restart-cache-node"}
        if outcome:
            v["fixOutcome"] = outcome
        return {"Variables": v}

    def test_pinned_outcomes(self):
        for _ in range(20):
            self.assertIn("drops back", w.h_auto_remediate(self.job("works"), None)["postActionTelemetry"])
            self.assertIn("still present", w.h_auto_remediate(self.job("fails"), None)["postActionTelemetry"])


class Config(unittest.TestCase):
    def test_env_beats_dotenv_and_missing_key_explains(self):
        old_file, old_env = common.ENV_FILE, os.environ.pop("TYPESAFE_API_KEY", None)
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
                f.write("# comment\nTYPESAFE_API_KEY='from-file'\n")
            common.ENV_FILE = f.name
            self.assertEqual(common.load_api_key(), "from-file")
            os.environ["TYPESAFE_API_KEY"] = "from-env"
            self.assertEqual(common.load_api_key(), "from-env")
            del os.environ["TYPESAFE_API_KEY"]
            common.ENV_FILE = "/nonexistent/.env"
            with self.assertRaises(RuntimeError) as cm:
                common.load_api_key()
            self.assertIn(".env.example", str(cm.exception))
        finally:
            common.ENV_FILE = old_file
            if old_env is not None:
                os.environ["TYPESAFE_API_KEY"] = old_env

    def test_worker_reads_nothing_outside_the_repo(self):
        self.assertTrue(common.ENV_FILE.startswith(ROOT))


class StoryCoversModel(unittest.TestCase):
    story = json.load(open(os.path.join(ROOT, "ui", "story.json")))

    def test_every_element_has_a_step(self):
        missing = [n for n in lab.MODEL["nodes"] if n not in self.story["steps"]]
        self.assertEqual(missing, [])

    def test_every_human_task_has_a_form(self):
        humans = [n for n, d in lab.MODEL["nodes"].items() if d["type"] == "userTask"]
        self.assertEqual(sorted(humans), sorted(self.story["human"]))
        self.assertEqual(sorted(humans), sorted(lab.HUMAN_TASKS))

    def test_every_job_type_has_a_handler(self):
        jobs = {d["job"] for d in lab.MODEL["nodes"].values() if d["job"]}
        self.assertEqual(jobs, set(w.HANDLERS))

    def test_recording_matches_model(self):
        rec = json.load(open(os.path.join(ROOT, "ui", "sample-recording.json")))
        self.assertEqual(sorted(f["id"] for f in rec["model"]["flows"]), sorted(f["id"] for f in lab.MODEL["flows"]))
        self.assertEqual(sorted(rec["scenarios"]), sorted(self.story["scenarios"]))


class NoSecretsInTree(unittest.TestCase):
    def test_no_private_paths_or_keys_in_tracked_text(self):
        bad = [os.path.expanduser("~"), "/home/", "/Users/", ".claude", "@gmail.com", "git.local"]
        offenders = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".run", "__pycache__", "recordings", "postmortems")]
            for name in files:
                if name in (".env", "WIP.md", "CLAUDE.md") or name.endswith((".png", ".pyc")) or name == "test_offline.py":
                    continue
                try:
                    text = open(os.path.join(base, name), errors="ignore").read()
                except OSError:
                    continue
                for b in bad:
                    if b and b in text:
                        offenders.append((os.path.relpath(os.path.join(base, name), ROOT), b))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
