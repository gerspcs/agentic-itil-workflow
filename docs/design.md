# Design notes: why the process looks the way it does

*For the reader who has run the lab and now asks why. The [README](../README.md) shows the lab working. This page gives the reasons behind it.*

---

## 1. A model, not a picture

A workflow described in prose, or drawn as a flowchart, is an intention. A BPMN model with `isExecutable="true"` is a contract. You can drop it into an engine, run it, and inspect it halfway through.

That matters most to someone learning. With the model they can:

1. Open the `.bpmn` file in Camunda Modeler and read the exact gate conditions, not a paraphrase.
2. Deploy it to a local Camunda 8 engine and watch a token move through it.
3. Change a threshold, redeploy, and see the behaviour change. It is the fastest way to build a feel for what a confidence threshold does.

---

## 2. Judgment shapes become task types

An "agentic" step is not one thing. It is one of a few shapes of judgment, and each fits a different BPMN task type. Getting this mapping right is what stops a workflow diagram from collapsing into a box that says "AI".

| Primitive | What the answer means | BPMN task | Why |
|---|---|---|---|
| **Choice** | Picks one option from a defined set | Business rule task | State goes to a decision step and a typed answer comes back. |
| **Noul** | The probability that one yes-or-no statement is true | Business rule task | Same reasoning. Use one Noul per label when several can hold at once. |
| **Score** | A position on an ordered, described scale | Business rule task | For a graded signal, such as how strongly a case resembles a known problem. |
| *plain code* | Fixed logic: fetch, execute, format | Service task | No judgment, only execution. |
| *a person* | A decision the agent may not make | User task | Scheduled through a task list, which is what Camunda's Tasklist is. |

Two rules follow, and both are load-bearing in the model.

- **A judgment is never the gate.** The gate is an exclusive gateway evaluating a condition *over* the judgment's output, such as `matchConfidence >= 0.8`. If the model's output could trigger the next step directly, a typed answer would guarantee the *interface* but not the *truth*, and the workflow would become a black box nobody can review.
- **Verification is a separate judgment from the one that authorised the action.** `Task_MatchPattern` asks whether the incident matches a playbook. `Task_VerifyFix` asks whether the symptom actually cleared. They are two different tasks. The judgment that acted does not get to grade its own result.

---

## 3. Which judgment for which ITIL activity

| ITIL activity | Type | Context the judgment needs | Pattern |
|---|---|---|---|
| Incident classification | Choice | The enriched event: symptom, service, recent deploys, prior incidents | Route, then fill known arguments. |
| Incident pattern match (a remediation candidate) | Noul | The classified event and the catalogue of validated playbooks | Verify and escalate. Pair it with a code-side reversibility check. |
| Remediation verification | Noul | Post-action telemetry for the same symptom | Verify and escalate. A second, independent judgment. |
| Problem clustering | Score | The new postmortem and the existing problem records | Find and judge evidence. Rank, do not just say yes or no. |
| Change-risk classification | Choice | The proposed change and past change outcomes | Route to auto-approve, normal approval or major approval. |
| Design conformance check | Noul, one per control | The proposed design and the control framework | Verify and escalate. One Noul per control, because several can fail independently. |
| Runbook relevance check | Score | Candidate runbook entries and the current symptoms | Find and judge evidence, so a human is not handed the whole knowledge base. |
| Demand-forecast anomaly flag | Noul | Historical telemetry and the current forecast | Respond to changing state. A stale forecast invalidates the flag. |
| Supplier scorecard drafting | Score, per dimension | SLA terms and actuals | Score each dimension once, let code apply the weights a human sets. |

The lab implements the first four rows. The rest are there so you can see how the pattern extends.

Two structural notes:

- **Ask independent judgments together.** If two questions read the same state and neither needs the other's answer, put them in one request, not two round trips.
- **A second judgment is warranted only when the first one's answer is needed to fetch new evidence or choose the next options.** That is why `Task_VerifyFix` runs strictly after `Task_AutoRemediate`: it needs the *result* of the action, which did not exist before.

---

## 4. When a human must decide

Three things decide whether a step gets a human gate, however confident the model sounds.

1. **Reversibility of the candidate action.** A confident judgment that authorises an irreversible action still needs a person. This is why Gate 1 reads `matchConfidence >= 0.8 and reversible = true`. The `and` is the whole point.
2. **Confidence, read correctly.** A Choice or Score confidence says how concentrated the answer is. It does not say the answer is right, and it is not permission to act. Set the acting threshold against measured outcomes on your own data, not a borrowed default. A Noul near 0.5 means yes and no look about equally likely. Treat it as low confidence, not as a middle answer.
3. **Altitude.** An agent proven trustworthy for *this incident, this pod* is not thereby qualified to say *should we change the architecture*, or *should we still offer this service*. Gate 3 sends a strong cluster signal to a person for *prioritisation*, and never to auto-fund an improvement. That decision sits one level up.

| Gate | What it checks | Default branch | Why the default is safe |
|---|---|---|---|
| `Gateway_AutoCandidate` | `matchConfidence >= 0.8 and reversible` | Human on-call triage | An unmatched or irreversible case always reaches a person before anything runs. |
| `Gateway_Resolved` | `verified = true`, from an independent Noul | Escalate to a human | A failed or unclear verification never retries silently. |
| `Gateway_CSISignal` | `clusterScore >= 0.6` | Closed, with "no CSI action" recorded | A weak signal still gets a record, but does not spend a person's attention. |

The thresholds in the lab (0.8, 0.7 for verify, 0.6) are starting points chosen for the demo. They were not derived from outcome data. Treat them as the first thing to measure and tune in a real deployment.

---

## 5. Every path writes a record

There is no sequence flow that reaches an end event without first passing through `Task_Postmortem`. Auto-resolved, human-triaged and escalated-after-a-failed-fix cases all converge on it. The rule that every operational action leaves a record is enforced by the shape of the diagram, not by a habit.

---

## 6. What the live runs caught

Wiring real workers to a real engine found defects that no static check had. They are worth reading, because each one is the kind a newcomer should expect.

**Two in the model**, caught by deploying to a live engine. Zeebe validates element order against the BPMN schema more strictly than `bpmn-js` does, so the file imported cleanly in a browser and was still rejected on deploy.

1. Inside `bpmn:startEvent`, `outgoing` must come before `messageEventDefinition`.
2. Inside `bpmn:userTask`, `documentation` must come before `extensionElements`.

The lesson: a clean `bpmn-js` import is necessary but not sufficient. Only a live deploy exercises the engine's own validation.

**Five in the worker and the tooling**, caught by running real incidents:

3. `c8ctl --json` output keys are capitalised (`Key`, `Variables`, `Process Instance`). Code that assumed lowercase crashed on the first real completion.
4. `c8ctl activate jobs` returns a status object, not an empty list, when nothing is waiting. Code that iterates it crashes on the next job type. The fix is to normalise any non-list to `[]`.
5. Counting shared words picks the wrong playbook. The first candidate selection chose `scale-out-web-tier` for a Redis incident, because words every playbook shares ("downstream", "is", "request") outvoted the few cache-specific ones. Weighting each word by `log(n / playbooks containing it)` fixes it. A real system would use embeddings here. The weighted overlap is a dependency-free stand-in that behaves the same at this size.
6. `--fetchVariable` is an allow-list, not a default. Omit a name and the handler silently never sees it. An early postmortem lost the engineer's diagnosis this way, even though it existed in the process's scope the whole time. The worker now fetches the full set in `ALL_VARIABLES`. The lesson generalises: a job worker sees only the state it explicitly asked for.
7. A hung `c8ctl` call froze the whole worker. It happened while preparing this repository for release. Every call now has a 30 second deadline, and the loop retries after an error. The engine re-activates an unfinished job after its timeout, so a job can run twice. Handlers should be safe to repeat.

---

## 7. Element conformance (OMG BPMN 2.0, formal/2011-01-03)

Every element used was checked against the specification, not against what a tutorial assumes.

*The section and page numbers below were carried over from the original write-up and were not re-checked when this repository was prepared. Verify them against the specification before you rely on them.*

| Element | Spec reference | How the model uses it |
|---|---|---|
| One participant with a lane set of two lanes | §9.2.1, pp. 114 and 120 | Agent and human are two lanes inside one process: one organisation doing incident management. Separate pools would model separate organisations, which is not the case here. |
| Message start event | §10.4.2, p. 240 | `StartEvent_Alert` carries a message event definition: an external alert starts the process. |
| Business rule task | p. 163 | The four judgment steps send state to a decision step and get typed output back. |
| Service task | p. 158 | `Task_Enrich`, `Task_AutoRemediate` and `Task_Postmortem` are automated steps with no judgment. |
| User task | p. 163 | The three human steps are scheduled through Camunda's Tasklist, the "task list manager" the spec describes. |
| Exclusive gateway with a default flow | §10.5.2, pp. 287 to 290, Fig. 10.105 | Each decision gateway has one conditional branch and one default branch. Default flows carry no condition. |
| Converging exclusive gateway | §10.5.2 | `Gateway_Converge` merges three incoming flows with no condition. |
| Several end events in one process | §10.4.3, p. 246 | `End_ClosedNoAction` and `End_ClosedCSI` are both valid terminal points. |

---

## 8. Checklist before you wire in a real judgment

- [ ] The judgment's *type* matches the need: Choice for one-of-a-set, Noul for one yes-or-no, Score for a graded dimension. Do not force a Choice to stand in for a threshold.
- [ ] The gate reading the output is an explicit BPMN condition, never the model's raw output triggering the next step.
- [ ] Verification of an action is a separate judgment from the one that authorised it.
- [ ] The acting threshold was set against this workflow's own observed outcomes.
- [ ] Every path reaches the structured-record step. Check it in the diagram, not by intent.
- [ ] Reversibility is known, an escalation path exists, no existing human checkpoint was removed, and the agent's altitude has not crept upward.

---

## Sources

- TypeSafe documentation (<https://docs.typesafe.ai>): the primitives (Choice, Noul, Score), the meaning of confidence, and the composition patterns referred to above.
- Camunda 8 documentation (<https://docs.camunda.io>).
- *Business Process Model and Notation (BPMN), Version 2.0*, Object Management Group, formal/2011-01-03.
- ITIL, as the common vocabulary for incident, problem and continual improvement. This lab uses the terms, and does not reproduce any ITIL text.

This page states no statistics.
