"""Offline tests: no engine, no network, no TypeSafe key.

    python3 -m unittest discover -s tests -v

They cover the deterministic half of the design: candidate selection, the
fix-outcome knob, configuration precedence, and that the viewer's story covers
every element of the BPMN model.
"""
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

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

    def test_whole_words_only(self):
        out = w.h_enrich_event({"Variables": {"incidentDescription": "my keyboard is rapidly failing"}}, None)
        self.assertEqual(out["serviceAreaGuess"], "unknown")


class FixOutcome(unittest.TestCase):
    def job(self, outcome):
        v = {"matchedPlaybookId": "restart-cache-node"}
        if outcome:
            v["fixOutcome"] = outcome
        return {"Key": 1, "Variables": v}

    def test_pinned_outcomes(self):
        for _ in range(20):
            self.assertIn("drops back", w.h_auto_remediate(self.job("works"), None)["postActionTelemetry"])
            self.assertIn("still present", w.h_auto_remediate(self.job("fails"), None)["postActionTelemetry"])

    def test_unpinned_outcome_is_repeatable_per_job(self):
        job = {"Key": 123, "Variables": {"matchedPlaybookId": "restart-cache-node"}}
        first = w.h_auto_remediate(job, None)["postActionTelemetry"]
        self.assertTrue(all(w.h_auto_remediate(job, None)["postActionTelemetry"] == first for _ in range(20)))


class Config(unittest.TestCase):
    def setUp(self):
        for name in ("TYPESAFE_API_KEY", "TYPESAFE_MODEL"):
            patcher = mock.patch.dict(os.environ)       # restores the environment after each test
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(name, None)
        self.addCleanup(setattr, common, "ENV_FILE", common.ENV_FILE)

    def dotenv(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write(text)
        f.close()
        common.ENV_FILE = f.name

    def test_env_beats_dotenv_and_missing_key_explains(self):
        self.dotenv("# comment\nTYPESAFE_API_KEY='from-file'\n")
        self.assertEqual(common.load_api_key(), "from-file")
        os.environ["TYPESAFE_API_KEY"] = "from-env"
        self.assertEqual(common.load_api_key(), "from-env")
        del os.environ["TYPESAFE_API_KEY"]
        common.ENV_FILE = "/nonexistent/.env"
        with self.assertRaises(RuntimeError) as cm:
            common.load_api_key()
        self.assertIn(".env.example", str(cm.exception))

    def test_model_pinned_in_dotenv_is_used(self):
        self.dotenv("TYPESAFE_MODEL=jev-pinned\n")
        self.assertEqual(common.typesafe_model(), "jev-pinned")
        os.environ["TYPESAFE_MODEL"] = "jev-shell"
        self.assertEqual(common.typesafe_model(), "jev-shell")

    def test_worker_reads_nothing_outside_the_repo(self):
        self.assertTrue(common.ENV_FILE.startswith(ROOT))


class TypeSafeFailures(unittest.TestCase):
    def ask(self):
        return common.typesafe_ask({}, {}, "k")

    def test_http_error_becomes_runtime_error_without_the_key(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                self.ask()
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("Bearer", str(cm.exception))

    def test_timeout_and_bad_shape_become_runtime_errors(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            with self.assertRaises(RuntimeError):
                self.ask()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"{}"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(RuntimeError):
                self.ask()

    def test_failed_judgment_fails_the_job_not_the_worker(self):
        job = {"Key": 7, "Process Instance": 1, "Variables": {}, "Retries": 3}
        calls = []
        with mock.patch.object(common, "activate_jobs", side_effect=lambda t, **k: [job] if t == "typesafe-classify-severity" else []), \
             mock.patch.object(common, "typesafe_choice", side_effect=RuntimeError("TypeSafe returned HTTP 429")), \
             mock.patch.object(common, "c8ctl", side_effect=lambda *a: calls.append(a)), \
             mock.patch.object(common, "complete_job") as done:
            self.assertEqual(w.run_once("k"), 0)
        done.assert_not_called()
        self.assertEqual(calls[0][:3], ("fail", "job", "7"))
        self.assertIn("2", calls[0])           # retries left: 3 - 1


class ClusterSemantics(unittest.TestCase):
    def run_with(self, scores):
        answers = {f"similarity_{n}": {"score": sc, "confidence": 1} for n, sc in enumerate(scores)}
        job = {"Variables": {"incidentDescription": "x", "postActionTelemetry": "y"}}
        with mock.patch.object(common, "typesafe_ask", return_value=(answers, "m")) as ask:
            out = w.h_cluster_problem(job, "k")
        self.assertEqual(ask.call_count, 1)    # one batched request, not one per cluster
        return out

    def test_recurring_cluster_wins(self):
        out = self.run_with([2, 0, 0])
        self.assertEqual((out["matchedClusterId"], out["clusterScore"]), ("PRB-CACHE-CPU", 1.0))

    def test_strong_match_to_noise_cluster_never_opens_the_gate(self):
        out = self.run_with([0, 0, 2])
        self.assertEqual(out["matchedClusterId"], "PRB-COSMETIC-NOISE")
        self.assertEqual(out["clusterScore"], 0.0)


class Times(unittest.TestCase):
    def test_epoch_accepts_engine_timestamp_shapes(self):
        base = lab._epoch("2026-10-05T19:44:41.692Z")
        self.assertAlmostEqual(lab._epoch("2026-10-05T19:44:41.692123456+0000"), base, delta=0.001)
        self.assertAlmostEqual(lab._epoch("2026-10-05T19:44:41.692+00:00"), base, delta=0.001)


class ModelFacts(unittest.TestCase):
    def test_thresholds_come_from_the_bpmn(self):
        self.assertEqual(lab.MODEL["thresholds"], {"matchConfidence": 0.8, "clusterScore": 0.6})


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

    def test_no_api_keys_in_tracked_text(self):
        key = re.compile(r"TYPESAFE_API_KEY\s*=\s*['\"]?(?!your-key-here)[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}")
        hits = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".run", "__pycache__", "recordings", "postmortems")]
            for name in files:
                if name in (".env", "WIP.md", "CLAUDE.md", "test_offline.py") or name.endswith((".png", ".pyc")):
                    continue
                text = open(os.path.join(base, name), errors="ignore").read()
                if key.search(text):
                    hits.append(os.path.relpath(os.path.join(base, name), ROOT))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
