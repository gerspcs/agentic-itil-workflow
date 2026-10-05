# AGENTS.md: instructions for AI coding assistants

You are helping a person set up and run the **Agentic Incident Triage lab** on their own machine: a Camunda 8 engine (via `c8run`) running an executable BPMN model, a Python job worker that makes live TypeSafe calls, and a live viewer. Read `README.md` for the story. This file is the runbook.

## Goal and definition of done
Done means you have **shown the person evidence** of all of these:
1. `scripts/lab.sh check` ran, and you reported its result **before** installing anything.
2. `scripts/lab.sh setup` finished (or you reported why it did not).
3. The engine answers: `scripts/lab.sh status` shows `engine answering`.
4. The model is deployed: `scripts/lab.sh deploy` printed a deployment table with `Process_Incident`.
5. The worker is running: `scripts/lab.sh status` shows `worker running`.
6. At least one incident ran to `COMPLETED`: from the viewer at `http://127.0.0.1:8099` (pick a case, answer the human step), or with `c8ctl publish msg` (see README). Quote the final state and a postmortem file from `workers/postmortems/`.
7. Offline tests pass: `python3 -m unittest discover -s tests -v`.

Do not report success from an exit code alone. Quote the output that proves each item.

## Run order
```bash
scripts/lab.sh check      # read-only. STOP and report if it prints FAIL.
scripts/lab.sh setup      # c8ctl, engine download (~1.1 GB), .env, c8ctl profile. No root.
scripts/lab.sh engine     # starts Camunda 8 and waits until it serves
scripts/lab.sh deploy
scripts/lab.sh worker     # needs TYPESAFE_API_KEY (see below)
scripts/lab.sh ui         # foreground; start it in the background only if your tool supports that
```
`scripts/lab.sh all` does setup through ui. `scripts/lab.sh demo` needs no engine and no key.

Report the **machine check result to the person before setup**. If it says FAIL, do not bypass it by lowering the thresholds in `scripts/lab.sh` unless they explicitly agree after you explain the risk (a swapping machine, an engine that dies of memory pressure).

## The TypeSafe key
The worker needs `TYPESAFE_API_KEY`. It is read from the environment first, then from `.env` in the repo root. **The person adds the key themselves.** Do not ask them to paste it into chat. Tell them to edit `.env`, and check only that it is set (`scripts/lab.sh check` says so without printing it).

## Rules (hard constraints)
- **Never run `sudo`.** If a step needs root (for example installing Java 21), print the exact command in a code block and ask the person to run it.
- **No secrets in git or in your output.** Never print or commit `.env`. It is gitignored; keep it that way. Never echo the key, `cat .env`, or put it in a command line.
- **Do not install system packages** (apt, brew, a Java runtime) without asking. `setup` installs only `c8ctl` (npm) and the engine download (`~/.cache/c8run`).
- **Confirm before destructive actions.** `scripts/lab.sh teardown` is a dry run by default: run it, **show the list**, and add `--apply` only after the person says yes. Each deeper flag (`--purge-data`, `--remove-profile`, `--remove-env`, `--remove-engine`) needs its own yes. `--remove-env` deletes their saved key. `--remove-engine` deletes a download that other projects may share.
- **Do not weaken the process to make something pass.** Do not change a gate condition, a threshold, or the "AND reversible" rule in the BPMN to get a green run. Do not make a human task auto-complete. If a run behaves oddly, diagnose it.
- **Do not fake a judgment.** Four steps use live TypeSafe calls. Never replace one with a canned answer to get past an error. If the key or network is missing, say so.
- **Stay inside this repository** and `~/.cache/c8run`. Do not change the person's global configuration. `setup` adds one `c8ctl` profile (`agentic-lab` by default) and does not switch their active profile.
- **Never use `pkill -f` or `kill -9` on a pattern.** Stop things with `scripts/lab.sh teardown`, or by the PID in `.run/`.

## How it fits together (for debugging)
- Engine: `c8run` (Camunda 8, version pinned by `C8_VERSION`, default `8.10.0-alpha5`), started from its own install directory with `--port $C8_PORT --disable-connectors`. REST at `http://localhost:$C8_PORT/v2`. Log: `.run/engine.log`.
- Model: `bpmn/agentic-incident-triage.bpmn`, process id `Process_Incident`, started by the message `Alert (monitoring/event system)`.
- Worker: `workers/agentic_worker.py` polls seven job types through `c8ctl activate jobs` and completes them. Log: `.run/worker.log`. Four types call TypeSafe (`workers/common.py`).
- Viewer: `ui/server.py` (standard library, loopback only, per-session token on POSTs). It reads one process instance from the REST API (`ui/lab.py`) and turns the element-instance start and end times into a timeline the page walks through one step at a time.
- Demo mode: `ui/sample-recording.json` is the final snapshot of three real runs. Re-record it only from a live run: `python3 ui/record_sample.py`.

## Known gotchas (already solved; do not re-investigate)
- **`c8run` must be started from its own install directory.** Started from anywhere else it fails with `JavaHome` errors. `scripts/lab.sh engine` does this.
- **Port 8080 is often taken.** Set `C8_PORT` in `.env`. The check reports it. 26500 and 9600 are fixed engine ports.
- **`c8ctl --json` keys are capitalised** (`Key`, `Variables`, `Process Instance`).
- **`c8ctl activate jobs` returns a status object, not `[]`,** when there is nothing to activate. `common.activate_jobs` normalises it.
- **`--fetchVariable` is an allow-list.** A variable missing from `ALL_VARIABLES` in `agentic_worker.py` silently never reaches a handler.
- **A hung `c8ctl` call used to freeze the worker.** Every call now has a 30 second deadline and the loop retries.
- **Zeebe validates BPMN element order strictly.** A clean `bpmn-js` import is not enough; only a live deploy proves a hand-edited model.
- **The search index lags the engine by a moment.** A completed user task can still appear open for a poll or two. Never answer the same task twice.
- Word-overlap candidate selection must be IDF-weighted, or generic words pick the wrong playbook (covered by `tests/test_offline.py`).

## Diagnostics
```bash
scripts/lab.sh status
tail -n 30 .run/engine.log .run/worker.log
curl -s http://localhost:8080/v2/topology            # use your C8_PORT; 200 means serving
c8ctl list pi --profile agentic-lab                  # process instances
c8ctl list ut --profile agentic-lab                  # open human tasks
```
"Engine process running" is not the same as "serving": always poll the real endpoint.

## Not built yet (do not claim otherwise)
Real telemetry, real remediation, embeddings for playbook selection, authentication on the viewer, macOS and Windows support. The live path was run end to end once, on one Linux machine. Say so if asked.

## When you finish
Summarise what you ran, quote the evidence for each "done" item, list anything skipped, and state any step you handed back to the person (root commands, the key, a teardown they must confirm).
