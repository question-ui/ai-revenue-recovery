const $ = (id) => document.getElementById(id);
let SYM = "\u20b9";
let lastAnomalyKey = null;

function money(n) {
  if (n == null) return "\u2014";
  if (n >= 1e7) return SYM + (n / 1e7).toFixed(2) + " Cr";
  if (n >= 1e5) return SYM + (n / 1e5).toFixed(2) + " L";
  if (n >= 1e3) return SYM + (n / 1e3).toFixed(1) + "K";
  return SYM + Math.round(n).toLocaleString("en-IN");
}
function pct(x) { return (x * 100).toFixed(1) + "%"; }
function srClass(sr) { return sr >= 0.9 ? "sr-good" : sr >= 0.75 ? "sr-warn" : "sr-bad"; }

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const which = t.dataset.tab;
    $("tab-payments").classList.toggle("hidden", which !== "payments");
    $("tab-recurring").classList.toggle("hidden", which !== "recurring");
    if (which === "recurring") loadSubscriptions();
  });
});

/* ---------------- payments stream ---------------- */
function render(s) {
  SYM = s.currency_symbol || SYM;
  $("tick-chip").textContent = "tick " + s.tick;
  const llm = $("llm-chip");
  llm.textContent = "LLM: " + (s.llm.enabled ? s.llm.provider : "rules");
  llm.classList.toggle("on", s.llm.enabled);

  $("at-risk").textContent = money(s.at_risk_amount);
  $("recovered").textContent = money(s.recovered_amount);
  $("recovered-txns").textContent = s.recovered_txns
    ? s.recovered_txns.toLocaleString("en-IN") + " payments clawed back"
    : "";

  const sr = s.overall.success_rate;
  $("overall-sr").textContent = pct(sr);
  const fill = $("gauge-fill");
  fill.style.width = (sr * 100).toFixed(1) + "%";
  fill.style.background = sr >= 0.9 ? "var(--recover)" : sr >= 0.8 ? "var(--anomaly)" : "var(--leak)";
  $("overall-vol").textContent = s.overall.total.toLocaleString("en-IN") + " txns in window";

  renderHealth(s.segments, s.anomaly);
  renderDiagnosis(s);
  renderFeed(s.events);

  $("btn-incident-gateway").disabled = s.incident_active;
  $("btn-incident-bank").disabled = s.incident_active;
  const label = s.incident_active ? "Degradation active" : null;
  $("btn-incident-gateway").textContent = label || "Simulate gateway timeout";
  $("btn-incident-bank").textContent = label || "Simulate bank declines";
}

function renderHealth(segments, anomaly) {
  const body = $("health-body");
  body.innerHTML = segments.map((g) => {
    const onRoute = anomaly && g.method === anomaly.method && g.issuer === anomaly.issuer;
    const implicated = onRoute && g.gateway === anomaly.gateway;
    return `<tr class="${implicated ? "row-degraded" : ""}">
      <td class="route">${g.method}/${g.issuer}/${g.gateway}${implicated ? " \u25c0" : ""}</td>
      <td class="sr-cell ${srClass(g.success_rate)}">${pct(g.success_rate)}</td>
      <td class="num">${pct(g.baseline_rate)}</td>
      <td class="num">${g.total}</td>
      <td>${g.dominant_error ? `<span class="err-chip">${g.dominant_error}</span>` : "\u2014"}</td>
      <td class="num">${money(g.at_risk_amount)}</td>
    </tr>`;
  }).join("");
}

function renderDiagnosis(s) {
  const panel = $("diagnosis-panel");
  const empty = $("diagnosis-empty");
  const bodyEl = $("diagnosis-body");

  if (!s.root_cause || !s.action) {
    empty.classList.remove("hidden");
    bodyEl.classList.add("hidden");
    lastAnomalyKey = null;
    return;
  }
  empty.classList.add("hidden");
  bodyEl.classList.remove("hidden");

  const rc = s.root_cause, act = s.action;
  const aKey = s.anomaly ? s.anomaly.segment : rc.anomaly_id;
  if (aKey !== lastAnomalyKey) {
    panel.classList.remove("firing"); void panel.offsetWidth; panel.classList.add("firing");
    lastAnomalyKey = aKey;
  }

  $("rca-src").textContent = rc.source;
  $("anomaly-tag").innerHTML = s.anomaly
    ? `\u26a0 ${s.anomaly.segment} \u00b7 ${pct(s.anomaly.current_rate)} (was ${pct(s.anomaly.baseline_rate)}) \u00b7 z=${s.anomaly.z_score}`
    : `\u2713 resolved`;
  $("rca-narrative").textContent = rc.narrative;
  $("conf-fill").style.width = (rc.confidence * 100) + "%";
  $("conf-pct").textContent = Math.round(rc.confidence * 100) + "%";

  $("action-kind").textContent = act.kind;
  $("action-title").textContent = act.title;
  $("action-detail").textContent = act.detail;
  const proj = $("action-projection");
  if (act.projected_recovered_txns > 0) {
    proj.classList.remove("none");
    proj.textContent = `Projected recovery: ~${act.projected_recovered_txns} txns \u00b7 ${money(act.projected_recovered_amount)}`;
  } else {
    proj.classList.add("none");
    proj.textContent = "No automated revenue recovery \u2014 alert / manual path.";
  }
  const card = $("action-card");
  card.classList.toggle("applied", act.applied);
  const btn = $("btn-apply");
  btn.textContent = act.applied ? "Applied \u2713" : "Apply recovery";
  btn.disabled = act.applied;
}

function renderFeed(events) {
  $("feed").innerHTML = (events || []).map((e) =>
    `<li><span class="k k-${e.kind}">${e.kind}</span><span class="t">${e.text}</span></li>`
  ).join("");
}

/* ---------------- actions ---------------- */
$("btn-incident-gateway").addEventListener("click", async () => {
  await fetch("/api/incident/trigger?kind=gateway", { method: "POST" });
});
$("btn-incident-bank").addEventListener("click", async () => {
  await fetch("/api/incident/trigger?kind=bank", { method: "POST" });
});
$("btn-apply").addEventListener("click", async () => {
  const btn = $("btn-apply"); btn.disabled = true; btn.textContent = "Applying\u2026";
  await fetch("/api/action/apply", { method: "POST" });
});

/* ---------------- subscriptions ---------------- */
$("btn-run-subs").addEventListener("click", async () => {
  const btn = $("btn-run-subs"); btn.disabled = true; btn.textContent = "Planning\u2026";
  const r = await fetch("/api/subscriptions/recover", { method: "POST" });
  renderSubscriptions(await r.json());
  btn.disabled = false; btn.textContent = "Re-run recovery agent";
});

async function loadSubscriptions() {
  const r = await fetch("/api/subscriptions");
  renderSubscriptions(await r.json());
}

function renderSubscriptions(s) {
  SYM = s.currency_symbol || SYM;
  $("subs-count").textContent = s.count;
  if (!s.planned) {
    $("funnel").innerHTML = `<div class="funnel-empty"><span>${s.count}</span> renewals failed this cycle. Run the agent to plan retries and dunning.</div>`;
    $("queue").innerHTML = `<div class="queue-empty">No recovery plans yet.</div>`;
    $("subs-src").textContent = "";
    return;
  }
  const f = s.funnel;
  let bars = Object.entries(f.by_reason).map(([reason, b]) => {
    const w = b.at_risk ? (b.expected / b.at_risk) * 100 : 0;
    return `<div class="rbar">
      <span class="rlabel">${reason}</span>
      <div class="track"><i class="atrisk"></i><i class="exp" style="width:${w}%"></i></div>
      <span class="rval">${money(b.expected)} / ${money(b.at_risk)}</span>
    </div>`;
  }).join("");

  $("funnel").innerHTML = `
    <div class="fstat risk"><div class="v">${money(f.at_risk_mrr)}</div><div class="l">MRR at risk</div></div>
    <div class="fstat rec"><div class="v">${money(f.expected_recovered_mrr)}</div><div class="l">Projected recovered MRR</div></div>
    <div class="fstat"><div class="v">${pct(f.recovery_rate)}</div><div class="l">Blended recovery rate</div></div>
    <div class="reason-bars">${bars}</div>`;

  const src = s.plans.find((p) => p.message_source.startsWith("llm"));
  $("subs-src").textContent = src ? src.message_source : "template messages";

  $("queue").innerHTML = s.plans.map((p) => `
    <div class="qcard">
      <div class="qwho">
        <span class="qname">${p.customer}</span>
        <span class="qmeta">${p.plan} \u00b7 ${money(p.mrr)}/mo</span>
        <span class="qreason">${p.failure_reason}</span>
      </div>
      <div class="qmsg ${p.channel === "NONE" ? "silent" : ""}">${p.channel === "NONE" ? "Silent retry \u2014 no customer message" : p.message}</div>
      <div class="qaction">
        <div class="qact-name">${p.action}${p.retry_in_days != null && p.retry_in_days > 0 ? ` \u00b7 +${p.retry_in_days}d` : ""}</div>
        <div class="qprob">${Math.round(p.recovery_probability * 100)}<span class="u">% likely</span></div>
        <div class="qexp">exp ${money(p.expected_recovered_mrr)}</div>
      </div>
    </div>`).join("");
}

/* ---------------- SSE ---------------- */
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => { try { render(JSON.parse(ev.data)); $("live-dot").classList.remove("stale"); } catch (e) {} };
  es.onerror = () => { $("live-dot").classList.add("stale"); };
}
connect();
