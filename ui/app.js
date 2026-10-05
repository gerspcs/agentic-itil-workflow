/* Agentic Incident Triage, live.
   The BPMN diagram on top, the same story told twice (plain and technical), and a case file that fills in.
   Two ways to run: live (served by ui/server.py, reads a real Camunda 8 engine) or demo
   (any static server; replays a recording of a real run). */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const store = { get(k, d) { try { return localStorage.getItem(k) || d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
  const TOKEN = document.querySelector('meta[name="ui-token"]').content;
  let STEP_MS = 650;                 // one process step at a time, so a person can follow
  const AI = ["choice", "noul", "score"];

  let story, flows = [], thresholds = { matchConfidence: 0.8, clusterScore: 0.6 }, backend = false, recording = null, mode = store.get("mode", "both");
  let run = null;                    // { snap, events, k, scenario, released:Set, speed, replay }
  let ticker = null, poller = null, pinned = null;

  const say = (v, d) => (v === undefined || v === null ? d : v);
  const pct = (x) => (Number(x) >= 1 ? "100" : Math.floor(Number(x) * 100)) + "%";   // round DOWN: 79.6% must not read as 80%, 99.5% not as a certainty
  const gate = (k) => Math.round(thresholds[k] * 100) + "%";
  const sure = (c) => (c >= 0.9 ? "very sure" : c >= 0.65 ? "fairly sure" : "not very sure");

  async function getJson(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return r.json(); }

  async function init() {
    story = await getJson("story.json");
    try { const m = await getJson("api/model"); backend = true; flows = m.flows; thresholds = m.thresholds || thresholds; if (!m.engine) banner("The viewer is running, but it cannot reach the Camunda engine at " + m.rest + ". Run scripts/lab.sh engine."); } catch { backend = false; }
    if (!backend) { recording = await getJson("sample-recording.json"); flows = recording.model.flows; thresholds = recording.model.thresholds || thresholds; banner("Demo mode: you are watching recordings of real runs. To run it live, follow the README (scripts/lab.sh all)."); }
    const svg = await (await fetch("diagram.svg")).text();
    $("#bpmn").innerHTML = svg.slice(svg.indexOf("<svg"));
    document.querySelectorAll(".sc").forEach((b) => { b.textContent = story.scenarios[b.dataset.sc].label; b.onclick = () => start(b.dataset.sc); });
    ["plain", "both", "tech"].forEach((m) => { $("#m-" + m).onclick = () => setMode(m); });
    buildPicker();
    $("#b-fast").onclick = () => { STEP_MS = STEP_MS === 650 ? 200 : 650; $("#b-fast").setAttribute("aria-pressed", STEP_MS === 200); if (run) schedule(); };
    $("#bpmn").addEventListener("click", (e) => {
      const g = e.target.closest("g.djs-element"), id = g && g.getAttribute("data-element-id");
      pinned = id && story.steps[id] && id !== pinned ? id : null; render();
    });
    setMode(mode); render();
    if (backend) { try { const s = await getJson("api/state"); if (s.pi) { /* a run is already in progress */ attach(s, null); } } catch { /* ignore */ } }
  }

  function buildPicker() {
    const box = $("#cards");
    $("#p-intro").textContent = story.idle.plain;
    Object.entries(story.scenarios).forEach(([key, sc]) => {
      const b = mk("button", "pcard"); b.append(mk("strong", null, sc.label), mk("span", null, sc.plain), mk("em", null, sc.watch)); b.onclick = () => start(key); box.append(b);
    });
    const form = $("#own"); form.hidden = !backend;
    form.onsubmit = (e) => { e.preventDefault(); const t = $("#own-text").value.trim(); if (t) start(null, t); };
  }

  function setMode(m) { mode = m; store.set("mode", m); document.body.dataset.mode = m; ["plain", "both", "tech"].forEach((x) => $("#m-" + x).setAttribute("aria-pressed", x === m)); render(); }
  function banner(t) { const b = $("#banner"); b.hidden = !t; b.textContent = t || ""; }
  function toast(t, ms = 5000) { const e = $("#toast"); e.textContent = t; e.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => { e.hidden = true; }, ms); }

  /* ---------- running a scenario ---------- */
  async function start(sc, custom) {
    stop(); pinned = null;
    if (!backend) {
      const rec = recording.scenarios[sc]; if (!rec) return toast("No recording for that scenario.");
      attach(rec.snapshot, sc, true); return;
    }
    document.querySelectorAll(".sc").forEach((b) => { b.disabled = true; });
    try {
      const r = await (await fetch("api/start", { method: "POST", headers: { "X-UI-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(custom ? { incident: custom } : { scenario: sc }) })).json();
      if (!r.ok) { toast(r.message || "Could not start."); } else { run = blank(sc || "custom", false); schedule(); poll(); }
    } catch { toast("Could not reach the lab server."); }
    document.querySelectorAll(".sc").forEach((b) => { b.disabled = false; });
    render();
  }
  function blank(sc, replay) { return { snap: null, events: [], k: 0, scenario: sc, released: new Set(), answered: new Set(), speed: 1, replay }; }
  function attach(snap, sc, replay) { run = blank(sc, !!replay); ingest(snap); if (!replay) { schedule(); poll(); } else schedule(); render(); }
  function stop() { clearInterval(ticker); clearTimeout(poller); ticker = poller = null; run = null; }

  function poll() {
    clearTimeout(poller);
    poller = setTimeout(async () => {
      const mine = run; if (!mine || mine.replay) return;
      let s = null;
      try { s = await getJson("api/state"); banner(""); } catch { banner("Lost the connection to the lab server. Retrying…"); }
      if (run !== mine) return;                      // the person started another run while this poll was in flight
      if (s && s.ok && s.pi) { ingest(s); banner(s.incidentAt && s.incidentAt.length ? "The engine reports a problem at " + s.incidentAt.join(", ") + ": a job failed (see .run/worker.log, or Camunda's Operate). The process is stuck until it is resolved." : ""); }
      else if (s && !s.ok) banner(s.error);
      if (run === mine && !(mine.snap && mine.snap.status !== "ACTIVE" && mine.k >= mine.events.length)) poll();
    }, 800);
  }

  function buildEvents(timeline) {
    const ev = [];
    for (const e of timeline) { ev.push({ t: e.start, id: e.id, kind: "start" }); if (e.end) ev.push({ t: e.end, id: e.id, kind: "end", ok: e.ok !== false }); }
    // Instant steps (gateways) start and end in the same millisecond, and a step can end the
    // instant the next one starts. Ties are broken by position in the process, then start-before-end.
    const rank = ranks();
    ev.sort((a, b) => a.t - b.t || (rank[a.id] || 0) - (rank[b.id] || 0) || (a.kind === b.kind ? 0 : a.kind === "start" ? -1 : 1));
    return ev;
  }
  function ranks() {                 // longest path from the start event; the model has no cycles
    if (ranks.cache) return ranks.cache;
    const r = {}; let changed = true, guard = 0;
    r.StartEvent_Alert = 0;
    while (changed && guard++ < 100) { changed = false; for (const f of flows) if (r[f.source] !== undefined && (r[f.target] === undefined || r[f.target] < r[f.source] + 1)) { r[f.target] = r[f.source] + 1; changed = true; } }
    return (ranks.cache = r);
  }
  function ingest(snap) { run.snap = snap; run.events = buildEvents(snap.timeline || []); if (run.k > run.events.length) run.k = run.events.length; render(); }

  /* The page walks through events one at a time, so fast steps stay readable. */
  function schedule() {
    clearInterval(ticker);
    ticker = setInterval(() => {
      if (!run || run.k >= run.events.length) return;
      const next = run.events[run.k];
      if (run.replay && next.kind === "end" && story.human[next.id] && !run.released.has(next.id)) return;   // wait for the person
      run.k++; render();
    }, STEP_MS);
  }

  function stateAt(k) {
    const el = {}; for (let i = 0; i < k; i++) { const e = run.events[i]; el[e.id] = e.kind === "start" ? "active" : e.ok === false ? "stopped" : "done"; } return el;
  }

  /* ---------- rendering ---------- */
  function render() {
    renderButtons();
    $("#picker").hidden = !!run; $("#story").hidden = !run;
    const el = run ? stateAt(run.k) : {};
    const over = !!(run && run.snap && run.snap.status !== "ACTIVE" && run.k >= run.events.length);
    const finished = over && run.snap.status === "COMPLETED";
    const last = run && run.k > 0 ? run.events[run.k - 1] : null;
    renderBpmn(el, last);
    renderStory(pinned ? { id: pinned } : last, finished && !pinned, el);
    renderGate(el);
    renderCase(el, finished, over);
  }

  function renderButtons() {
    document.querySelectorAll(".sc").forEach((b) => { b.classList.toggle("primary", !!run && run.scenario === b.dataset.sc); });
  }

  function renderBpmn(el, last) {
    document.querySelectorAll("#bpmn g.djs-element").forEach((g) => {
      const id = g.getAttribute("data-element-id");
      if (!story.steps[id]) return;
      g.classList.remove("st-idle", "st-active", "st-done", "focus");
      g.classList.add("st-" + (el[id] || "idle"));
      if (last && last.id === id) g.classList.add("focus");
      g.classList.toggle("pinned", pinned === id);
    });
    const taken = run ? takenFlows(el) : new Set();
    flows.forEach((f) => {
      const g = document.querySelector('#bpmn g.djs-element[data-element-id="' + f.id + '"]'); if (!g) return;
      const isTaken = taken.has(f.id);
      g.classList.remove("fl-idle", "fl-active", "fl-done");
      g.classList.add(!isTaken ? "fl-idle" : el[f.target] === "active" ? "fl-active" : "fl-done");
    });
  }

  /* Which sequence flow did the token actually use to reach each step? Where several flows lead into
     one step (a merge), it is the one whose source finished most recently, not every flow whose
     source was finished. */
  function takenFlows(el) {
    const startT = {}, endT = {}, rank = ranks();
    for (let i = 0; i < run.k; i++) { const e = run.events[i]; (e.kind === "start" ? startT : endT)[e.id] = e.t; }
    const out = new Set();
    for (const id of Object.keys(startT)) {
      const cands = flows.filter((f) => f.target === id && endT[f.source] !== undefined && endT[f.source] <= startT[id]);
      if (!cands.length) continue;
      cands.sort((a, b) => endT[b.source] - endT[a.source] || (rank[b.source] || 0) - (rank[a.source] || 0));
      out.add(cands[0].id);
    }
    return out;
  }

  function lines(id, v, plain) {
    const out = [];
    const add = (s) => out.push(s);
    const has = (k) => v[k] !== undefined;
    if (!plain) { for (const k of story.steps[id].show) if (has(k)) add(k + " = " + (typeof v[k] === "object" ? JSON.stringify(v[k]) : String(v[k]))); return out; }
    switch (id) {
      case "StartEvent_Alert": if (has("incidentDescription")) add("“" + v.incidentDescription + "”"); break;
      case "Task_Enrich": if (has("serviceAreaGuess")) add("Looks like it is in the " + v.serviceAreaGuess + "."); break;
      case "Task_ClassifySeverity": if (has("severity")) add("Verdict: " + v.severity.toUpperCase() + " (" + sure(v.severityConfidence) + ")."); break;
      case "Task_MatchPattern": if (has("matchedPlaybookId")) { add("Closest standard fix: " + v.matchedPlaybookId + "."); add("How well it fits: " + pct(v.matchConfidence) + ". Can it be undone? " + (v.reversible ? "Yes." : "No.")); } break;
      case "Gateway_AutoCandidate": if (has("matchConfidence")) add(v.matchConfidence >= thresholds.matchConfidence && v.reversible ? "Sure enough (" + pct(v.matchConfidence) + " against a rule of " + gate("matchConfidence") + ") and it can be undone, so the agent may act." : "Not sure enough, or it cannot be undone, so a person takes over."); break;
      case "Task_AutoRemediate": if (has("actionTaken")) { add("Did: " + v.actionTaken); add("Afterwards: " + v.postActionTelemetry); } break;
      case "Task_VerifyFix": if (has("verified")) add(v.verified ? "The check says it is fixed (" + pct(v.verifyProbability) + " sure)." : "The check says it is NOT fixed (only " + pct(v.verifyProbability) + " sure it worked)."); break;
      case "Gateway_Resolved": if (has("verified")) add(v.verified ? "Fixed, so the process moves on to the record." : "Not fixed, so a person is called. No silent retry."); break;
      case "Task_HumanTriage": case "Task_EscalateHuman": if (has("triageOutcome")) add("The engineer wrote: “" + v.triageOutcome + "”"); break;
      case "Task_Postmortem": if (has("postmortemPath")) add("Saved as " + v.postmortemPath); break;
      case "Task_ClusterProblem": if (has("matchedClusterId")) add("Closest known repeating problem: " + v.matchedClusterId + " (" + pct(v.clusterScore) + " match)."); break;
      case "Gateway_CSISignal": if (has("clusterScore")) add(v.clusterScore >= thresholds.clusterScore ? "A strong match (" + pct(v.clusterScore) + " against a rule of " + gate("clusterScore") + "), so a person decides about a lasting fix." : "A weak match (" + pct(v.clusterScore) + " against a rule of " + gate("clusterScore") + "), so the case closes with a note that nothing more is needed."); break;
      case "Task_ConfirmRootCause": if (has("csiDecision")) add((v.rootCauseConfirmed ? "Confirmed: " : "Not now: ") + "“" + v.csiDecision + "”"); break;
      default: break;
    }
    return out;
  }

  function renderStory(last, finished, el) {
    const box = $("#story");
    let c, id = last && last.id;
    if (!run) { c = story.idle; id = null; $("#s-kicker").textContent = "Ready"; }
    else if (finished) { c = story.done; $("#s-kicker").textContent = "Finished"; }
    else if (!id) { c = { title: "Waiting for the engine…", plain: "The alert has been sent. The process is starting.", technical: "Waiting for the message start event to create the process instance." }; $("#s-kicker").textContent = "Starting"; }
    else { c = story.steps[id]; $("#s-kicker").textContent = pinned ? "You pinned this step (click it again to follow the run)" : run.replay ? "Recording · step " + run.k + " of " + run.events.length : "Right now"; }
    $("#s-title").textContent = c.title; $("#s-plain").textContent = c.plain; $("#s-tech").textContent = c.technical;
    const kind = id && story.steps[id] ? story.steps[id].kind : null, chip = $("#s-kind");
    chip.hidden = !kind || finished; chip.textContent = kind ? story.kinds[kind] : ""; chip.className = "chip" + (AI.includes(kind) ? " ai" : kind === "human" ? " human" : "");
    box.classList.toggle("ai", AI.includes(kind)); box.classList.toggle("human", kind === "human"); box.classList.toggle("good", finished);
    const v = (run && run.snap && run.snap.vars) || {};
    // A person's answer is shown only once they have given it (the recording holds it from the start).
    const reveal = id && !finished && !(story.human[id] && el[id] !== "done");
    $("#r-plain").textContent = reveal ? lines(id, v, true).join("\n") : ""; $("#r-plain").style.whiteSpace = "pre-wrap";
    $("#r-tech").textContent = reveal ? lines(id, v, false).join("\n") : "";
  }

  function renderGate(el) {
    const g = $("#gate"), open = run && Object.keys(story.human).find((id) => el[id] === "active" && !run.answered.has(id));
    g.hidden = !open; if (!open) { g.dataset.id = ""; return; }
    if (g.dataset.id === open) return;               // keep what the person is typing
    g.dataset.id = open;
    const h = story.human[open];
    $("#g-ask").textContent = h.ask; $("#g-tech").textContent = h.technical;
    $("#g-text").value = h.defaults[run.scenario] || h.defaults.default;
    $("#g-note").textContent = run.replay ? "This is a recording: the answer shown is the one recorded, and what you type is not sent. Run the lab live to have your own answer recorded in the process." : "";
    const bx = $("#g-buttons"); bx.textContent = "";
    h.buttons.forEach((b) => { const btn = mk("button", b.confirm ? "" : "alt", b.label); btn.onclick = () => answer(open, b.confirm); bx.append(btn); });
    g.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function answer(id, confirm) {
    const text = $("#g-text").value;
    if (run.replay) { run.released.add(id); $("#gate").dataset.id = ""; $("#gate").hidden = true; return; }
    try {
      const r = await (await fetch("api/human", { method: "POST", headers: { "X-UI-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify({ text, confirm }) })).json();
      if (!r.ok) toast(r.message || "That did not work."); else { run.answered.add(id); $("#gate").hidden = true; $("#gate").dataset.id = ""; }
    } catch { toast("Could not reach the lab server."); }
  }

  function renderCase(el, finished, over) {
    const v = (run && run.snap && run.snap.vars) || {}, done = (id) => el[id] === "done";
    const tally = $("#tally"); tally.textContent = "";
    const n = { ai: 0, code: 0, human: 0, gate: 0 };
    if (run) for (const id of Object.keys(el)) { if (el[id] !== "done") continue; const k = story.steps[id] && story.steps[id].kind; if (AI.includes(k)) n.ai++; else if (k === "code") n.code++; else if (k === "human") n.human++; else if (k === "gate" && id !== "Gateway_Converge") n.gate++; }
    [["ai", "AI judgments", n.ai], ["code", "plain code steps", n.code], ["gate", "visible rules checked", n.gate], ["human", "people", n.human]].forEach(([c, label, count]) => tally.append(mk("span", "tl " + (c === "gate" ? "code" : c), count + " " + label)));
    const ev = $("#ev"); ev.textContent = "";
    const row = (k, val) => { ev.append(mk("dt", null, k)); ev.append(mk("dd", null, val)); };
    const T = mode === "tech";
    if (!run) { row("Nothing yet", "Press a scenario above."); $("#c-sub").textContent = ""; $("#verdict").hidden = true; return; }
    $("#c-sub").textContent = run.replay ? "from a recording of a real run" : "from the live engine";
    if (done("StartEvent_Alert") || el.StartEvent_Alert) row(T ? "incidentDescription" : "The alert", say(v.incidentDescription, ""));
    if (done("Task_ClassifySeverity")) row(T ? "severity (Choice)" : "How serious", v.severity + (T ? " · confidence " + v.severityConfidence : " (" + sure(v.severityConfidence) + ")"));
    if (done("Task_MatchPattern")) row(T ? "matchedPlaybookId (Noul)" : "Standard fix considered", v.matchedPlaybookId + " · " + pct(v.matchConfidence) + (v.reversible ? " · reversible" : " · not reversible"));
    if (done("Gateway_AutoCandidate")) row(T ? "Gateway_AutoCandidate" : "May the agent act alone?", el.Task_AutoRemediate ? "Yes" : "No, a person takes over");
    if (done("Task_VerifyFix")) row(T ? "verified (Noul)" : "Did the fix work?", (v.verified ? "Yes" : "No") + " · " + pct(v.verifyProbability) + " sure");
    if (done("Task_HumanTriage") || done("Task_EscalateHuman")) row(T ? "triageOutcome" : "What the engineer found", say(v.triageOutcome, ""));
    if (done("Task_Postmortem")) row(T ? "postmortemPath" : "Written record", say(v.postmortemPath, ""));
    if (done("Task_ClusterProblem")) row(T ? "clusterScore (Score)" : "Known repeating problem?", v.matchedClusterId + " · " + pct(v.clusterScore));
    if (done("Task_ConfirmRootCause")) row(T ? "csiDecision" : "Decision on a lasting fix", (v.rootCauseConfirmed ? "Funded: " : "Not now: ") + say(v.csiDecision, ""));
    const vd = $("#verdict"); vd.hidden = !over;
    if (over && !finished) { vd.className = "warn"; vd.textContent = "This run did not finish normally (the engine reports it as " + run.snap.status.toLowerCase() + "). Nothing above should be read as a result."; return; }
    if (finished) {
      const agent = !!el.Task_AutoRemediate, fixed = v.verified === true, csi = !!el.Task_ConfirmRootCause;
      const parts = [agent ? (fixed ? "The agent fixed it on its own and an independent check agreed." : "The agent tried a fix, the check said it had not worked, and a person took over.") : "No safe standard fix matched, so a person handled it from the start.",
        "A written record was made either way.", csi ? "A person decided about a lasting fix." : "It was not a repeating problem, so nothing more was needed."];
      vd.className = fixed || !agent ? "good" : "warn"; vd.textContent = "";
      vd.append(mk("strong", null, "How this case went. "), document.createTextNode(parts.join(" ")));
      let leash;
      if (agent && fixed) leash = "Why the agent was allowed to act: it was " + pct(v.matchConfidence) + " sure of the match (the rule needs " + gate("matchConfidence") + ") and the fix could be undone. Both had to be true.";
      else if (agent) leash = "Why this was safe: a second, separate check judged the fix, and found it only " + pct(v.verifyProbability) + " likely to have worked. The agent handed over instead of trying again.";
      else leash = "Why the agent did not act: it was only " + pct(v.matchConfidence) + " sure of the match (the rule needs " + gate("matchConfidence") + "), or the fix could not be undone. Doing nothing was the safe answer.";
      vd.append(mk("span", "leash", leash));
    }
  }

  init().catch((e) => { $("#s-title").textContent = "Could not start the page"; $("#s-plain").textContent = String(e); });
})();
