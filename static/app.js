/* ClearAP front end: live run view (SSE), dashboard, reference data. No build step. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_LABEL = { APPROVED: "Approved", NEEDS_REVIEW: "Held for review", REJECTED: "Rejected", ERROR: "Error" };
const STAMP_LABEL = { APPROVED: "Approved", NEEDS_REVIEW: "On hold", REJECTED: "Rejected" };
const GLYPH = { pass: "✓", info: "i", warn: "!", hold: "‖", reject: "✕" };
const FIELD_LABELS = {
  vendor_name: "Vendor", vendor_gstin: "Vendor GSTIN", buyer_gstin: "Billed-to GSTIN", invoice_number: "Invoice number",
  invoice_date: "Invoice date", due_date: "Due date", po_reference: "PO reference", subtotal: "Subtotal (pre-tax)",
  tax_total: "Tax", total: "Total", bank_name: "Bank", bank_account: "Account number", ifsc: "IFSC",
};
const AMOUNT_FIELDS = new Set(["subtotal", "tax_total", "total"]);
const STAGE_HELP = {
  intake: "Accept the PDF and fingerprint it.",
  read: "Text layer, or OCR for scans.",
  extract: "Pull out every field, with confidence.",
  validate: "Arithmetic, GSTIN, GST type, mandatory fields.",
  vendor: "Known, active, and paid to the verified account?",
  po: "Find the PO, check price, quantity and remaining balance.",
  duplicate: "Exact, reformatted and same-amount matches.",
  decide: "Approve, hold or reject: worst rule wins.",
  route: "Explain, assign an owner, draft the vendor query.",
};

const state = { config: null, samples: [], es: null, clock: null, runId: null, fields: null, filter: "",
  batch: false, results: {}, worst: "pass" };
const SEV = { pass: 0, info: 1, warn: 2, hold: 3, reject: 4 };

function inr(x, keepPaise = false) {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  const neg = n < 0;
  let [w, f] = Math.abs(n).toFixed(2).split(".");
  if (w.length > 3) {
    const tail = w.slice(-3);
    let head = w.slice(0, -3);
    const groups = [];
    while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) groups.unshift(head);
    w = groups.join(",") + "," + tail;
  }
  return (neg ? "-" : "") + "₹" + w + (f === "00" && !keepPaise ? "" : "." + f);
}
function niceDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}
function niceTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + ", " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.show();
  clearTimeout(t._h);
  t._h = setTimeout(() => t.close(), 2600);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

/* ------------------------------------------------------------------ tabs */
function showView(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "dashboard") loadDashboard();
  if (name === "reference") loadReference();
  history.replaceState(null, "", name === "process" ? location.pathname : "#" + name);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

/* ------------------------------------------------------------------ init */
async function init() {
  state.config = await api("/api/config");
  const m = $("#mode");
  m.textContent = state.config.ai_enabled ? `Reading with ${state.config.provider} + rules` : "Reading with rules + OCR";
  m.classList.toggle("ai", state.config.ai_enabled);
  m.title = state.config.ai_enabled ? `Model: ${state.config.model}, cross-checked by the rule-based parser`
    : "No AI key set: deterministic parser and OCR only";
  $("#buyer").textContent = state.config.buyer;
  renderHow();
  loadPolicy();
  state.samples = await api("/api/samples");
  renderSamples();
  renderLanding();
  setDoc(null);
  const h = location.hash.replace("#", "");
  if (["dashboard", "reference", "how"].includes(h)) showView(h);
}

function renderSamples() {
  const groups = {};
  state.samples.forEach((s) => (groups[s.group] = groups[s.group] || []).push(s));
  const btns = (items) => items.map((s) => `
        <button class="sample" data-file="${esc(s.file)}" title="${esc(s.story)}">
          <span class="sample-title">${esc(s.title)}</span>
          <span class="expect ${s.expect}" title="Expected outcome for this test invoice">${esc(STATUS_LABEL[s.expect])}</span>
          <span class="sample-file">${esc(s.file)}</span>
        </button>`).join("");
  $("#samples").innerHTML = Object.entries(groups).map(([g, items]) => g.startsWith("Other")
    ? `<details class="sample-group more"><summary class="sample-group-name">${esc(g)} (${items.length})</summary>${btns(items)}</details>`
    : `<div class="sample-group"><div class="sample-group-name">${esc(g)}</div>${btns(items)}</div>`).join("");
  $$(".sample").forEach((b) => b.addEventListener("click", () => startRun({ sample: b.dataset.file })));
  paintResults();
}

function markActive(file) {
  $$(".sample").forEach((x) => x.classList.toggle("active", x.dataset.file === file));
}

/* After a run, the sidebar shows whether each test invoice came out as expected. */
function paintResults() {
  $$(".sample").forEach((b) => {
    const r = state.results[b.dataset.file];
    b.classList.toggle("ran", !!r);
    const exp = state.samples.find((x) => x.file === b.dataset.file)?.expect;
    b.classList.toggle("mismatch", !!r && r !== exp);
    b.querySelector(".ran-mark")?.remove();
    if (r) b.querySelector(".sample-title").insertAdjacentHTML("afterbegin",
      `<span class="ran-mark" title="${r === exp ? "Ran: outcome as expected" : "Ran: outcome differs from expected"}">${r === exp ? "✓" : "!"}</span>`);
  });
}

function renderLanding() {
  $("#landingFlow").innerHTML = state.config.stages.map((s) => `<li><span>${esc(s.title)}</span></li>`).join("");
  $("#startHappy").onclick = () => startRun({ sample: state.samples[0].file });
  $("#startBatch").onclick = runBatch;
}

/* ------------------------------------------------------------------ upload */
const drop = $("#drop"), fileInput = $("#fileInput");
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) startRun({ file: fileInput.files[0] }); fileInput.value = ""; });
$("#batchBtn").addEventListener("click", () => runBatch());
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) startRun({ file: f });
});

/* ------------------------------------------------------------------ run */
async function startRun({ file, sample, fromBatch = false }) {
  if (state.batch && !fromBatch) { toast("A batch is running. Let it finish first."); return null; }
  showView("process");
  if (sample) markActive(sample);
  $("#decision").hidden = true;
  $("#stamp").hidden = true;
  const fd = new FormData();
  if (file) fd.append("file", file);
  if (sample) fd.append("sample", sample);
  if (fromBatch) fd.append("fast", "true");
  let res;
  try {
    res = await api("/api/runs", { method: "POST", body: fd });
  } catch (e) {
    toast(e.message);
    return null;
  }
  const name = file ? file.name : sample;
  const src = file ? URL.createObjectURL(file) : "/samples/" + encodeURIComponent(sample);
  beginRunView(res.run_id, name, file ? "Uploaded file" : "Test invoice", src);
  const story = sample && state.samples.find((x) => x.file === sample)?.story;
  $("#runStory").hidden = !story;
  $("#runStory").textContent = story || "";
  return stream(res.run_id);
}

/* ------------------------------------------------------------------ batch */
async function runBatch() {
  if (state.batch) return;
  if (!confirm(`Process all ${state.samples.length} test invoices one after another?\n\nRun this right after "Reset demo". Invoices you've already processed will (correctly) come back as duplicates.`)) return;
  state.batch = true;
  document.body.classList.add("batching");
  $("#batchBtn").disabled = true;
  $("#batchStatus").hidden = false;
  const n = state.samples.length;
  let i = 0;
  for (const s of state.samples) {
    i += 1;
    $("#batchText").textContent = `Processing ${i} of ${n}…`;
    $("#batchFill").style.width = `${(100 * (i - 1)) / n}%`;
    const rec = await startRun({ sample: s.file, fromBatch: true });
    if (!rec) break;
    $("#batchFill").style.width = `${(100 * i) / n}%`;
    await new Promise((r) => setTimeout(r, 700));
  }
  $("#batchText").textContent = i === n ? `Done: ${n} invoices processed.` : `Stopped after ${i} of ${n}.`;
  state.batch = false;
  document.body.classList.remove("batching");
  $("#batchBtn").disabled = false;
  setTimeout(() => { $("#batchStatus").hidden = true; }, 6000);
  showView("dashboard");
}

function beginRunView(runId, name, sourceLabel, docSrc) {
  state.runId = runId;
  state.fields = null;
  if (state.es) state.es.close();
  clearInterval(state.clock);
  $("#runEmpty").hidden = true;
  $("#runView").hidden = false;
  $("#runFile").textContent = name;
  $("#runMeta").textContent = `${sourceLabel} · run ${runId}`;
  $("#decision").hidden = true;
  $("#decision").className = "decision";
  $("#stamp").hidden = true;
  state.worst = "pass";
  setProgress(0, null);
  $("#timeline").innerHTML = state.config.stages.map((s) => `
    <li class="stage pending" data-stage="${s.id}">
      <span class="dot" aria-hidden="true"></span>
      <button class="stage-row" disabled aria-expanded="false">
        <span class="stage-title">${esc(s.title)}<span class="chev" aria-hidden="true"></span></span>
        <span class="stage-ms"></span>
        <span class="stage-sum">${esc(STAGE_HELP[s.id] || "")}</span>
      </button>
      <div class="stage-body"></div>
    </li>`).join("");
  $$("#timeline .stage-row").forEach((btn) => btn.addEventListener("click", () => {
    const li = btn.closest(".stage");
    li.classList.toggle("open");
    btn.setAttribute("aria-expanded", li.classList.contains("open"));
  }));
  setDoc(docSrc);
}

const DOC_ICON = `<svg width="56" height="68" viewBox="0 0 56 68" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M8 3h28l14 14v46a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M36 3v14h14M15 30h26M15 39h26M15 48h16"/></svg>`;

function setDoc(src) {
  $("#docFrame").innerHTML = src
    ? `<iframe title="Invoice preview" src="${src}#toolbar=0&navpanes=0&view=FitH"></iframe>`
    : `<div class="doc-empty">${DOC_ICON}<p>The invoice you run appears here, with the decision stamped on it.</p></div>`;
}

function stream(runId) {
  return new Promise((resolve) => {
  const t0 = Date.now();
  $("#runClock").textContent = "0.0s";
  state.clock = setInterval(() => ($("#runClock").textContent = ((Date.now() - t0) / 1000).toFixed(1) + "s"), 100);
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.es = es;
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (runId !== state.runId) return es.close();
    if (ev.type === "queued") {
      $("#runStep").textContent = `Waiting for ${ev.ahead > 1 ? ev.ahead + " invoices" : "the previous invoice"} to finish…`;
      $$("#timeline .stage")[0]?.querySelector(".stage-sum") &&
        ($$("#timeline .stage")[0].querySelector(".stage-sum").textContent = "Queued: invoices are processed one at a time so PO balances stay correct.");
    } else if (ev.type === "stage") applyStage(ev);
    else if (ev.type === "done") {
      es.close();
      clearInterval(state.clock);
      if (ev.record.duration_ms) $("#runClock").textContent = (ev.record.duration_ms / 1000).toFixed(1) + "s";
      finish(ev.record, true);
      resolve(ev.record);
    } else if (ev.type === "error") toast(ev.message);
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) return;
    es.close();
    clearInterval(state.clock);
    // fall back to fetching the finished record
    setTimeout(() => api(`/api/runs/${runId}`)
      .then((r) => { r.events.forEach(applyStage); finish(r, true); resolve(r); })
      .catch(() => { toast("Lost connection to the run."); resolve(null); }), 600);
  };
  });
}

function applyStage(ev) {
  const li = $(`.stage[data-stage="${ev.stage}"]`);
  if (!li) return;
  if (ev.status === "running") {
    li.className = "stage running";
    setProgress($$("#timeline .stage.done").length, ev.title);
    li.querySelector(".stage-sum").textContent = "Working…";
    li.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return;
  }
  li.className = "stage done";
  li.dataset.o = ev.outcome || "pass";
  if (SEV[ev.outcome] > SEV[state.worst] && ev.stage !== "decide" && ev.stage !== "route") state.worst = ev.outcome;
  setProgress($$("#timeline .stage.done").length, ev.stage === "route" ? "done" : ev.title);
  li.querySelector(".dot").textContent = GLYPH[ev.outcome] || "✓";
  li.querySelector(".stage-sum").textContent = ev.summary || "";
  li.querySelector(".stage-ms").textContent = ev.ms != null ? (ev.ms >= 1000 ? (ev.ms / 1000).toFixed(1) + "s" : ev.ms + " ms") : "";
  const btn = li.querySelector(".stage-row");
  btn.disabled = false;
  li.querySelector(".chev").textContent = "›";
  if (ev.stage === "extract") state.fields = ev.data.fields;
  li.querySelector(".stage-body").innerHTML = stageBody(ev);
  if (["hold", "reject"].includes(ev.outcome) && ev.stage !== "decide") {
    li.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
  }
}

function setProgress(done, label) {
  const total = state.config.stages.length;
  $("#progressFill").style.width = `${(100 * done) / total}%`;
  $("#progress").dataset.o = state.worst;
  $("#progress").setAttribute("aria-valuenow", done);
  $("#runStep").textContent = label === "done" ? `${total} of ${total} stages` : label ? `Stage ${Math.min(done + 1, total)} of ${total} · ${label}` : "";
}

/* ------------------------------------------------------------------ stage bodies */
function checksHtml(checks) {
  if (!checks || !checks.length) return "";
  return `<ul class="checks">${checks.map((c) => `
    <li class="check ${c.outcome}">
      <span class="g" aria-label="${c.outcome}">${GLYPH[c.outcome]}</span>
      <div><div class="check-name">${esc(c.name)}${c.owner && ["hold", "reject"].includes(c.outcome) ? `<span class="owner">→ ${esc(c.owner)}</span>` : ""}</div>
      <div class="check-msg">${esc(c.message)}</div></div>
    </li>`).join("")}</ul>`;
}

function confHtml(c) {
  const pct = Math.round((c || 0) * 100);
  const cls = c >= 0.85 ? "" : c >= 0.7 ? "mid" : "low";
  return `<span class="conf"><span class="bar"><i class="${cls}" style="width:${pct}%"></i></span>${pct}%</span>`;
}

function stageBody(ev) {
  const d = ev.data || {};
  switch (ev.stage) {
    case "intake":
      return `<div class="kv"><span>File</span><b>${esc(d.filename)}</b><span>Size</span><b>${(d.bytes / 1024).toFixed(1)} KB</b>
        <span>Fingerprint</span><b>${esc(d.sha256)}…</b><span>Source</span><b>${esc(d.source)}</b></div>`;
    case "read":
      return `<div class="kv"><span>Method</span><b>${d.method === "ocr" ? "OCR (Tesseract): no text layer, scanned image" : "Native PDF text layer"}</b>
        <span>Pages</span><b>${d.pages}</b>${d.ocr_confidence != null ? `<span>OCR confidence</span><b>${d.ocr_confidence}% mean per word</b>` : ""}</div>
        <details class="more"><summary>Show raw text</summary><div class="raw">${esc(d.text)}</div></details>`;
    case "extract": {
      const ag = d.agreement || {};
      const agreeLabel = { agree: "both readers agree", ai_only: "AI only", rules_only: "rules only", disagree: "readers disagree",
        minor_diff: "minor wording difference", resolved: "resolved by arithmetic" };
      const rows = Object.entries(FIELD_LABELS).map(([k, label]) => {
        let v = d.fields[k];
        let shown;
        if (v === null || v === undefined || v === "") {
          shown = k === "invoice_date" && d.invoice_date_raw ? `<span class="missing">unreadable: "${esc(d.invoice_date_raw)}"</span>`
            : `<span class="missing">not found</span>`;
        } else if (AMOUNT_FIELDS.has(k)) shown = esc(inr(v, true));
        else if (k === "invoice_date" || k === "due_date") shown = esc(niceDate(v));
        else shown = esc(v);
        const a = ag[k] ? `<span class="agree ${ag[k]}">${esc(agreeLabel[ag[k]] || ag[k])}${["disagree", "resolved", "minor_diff"].includes(ag[k]) ? `: other reading "${esc(AMOUNT_FIELDS.has(k) ? inr(ag[k + "_alt"], true) : ag[k + "_alt"])}"` : ""}</span>` : "";
        return `<tr><td>${label}</td><td class="v">${shown}${a}</td><td>${v != null && v !== "" ? confHtml(d.confidence[k]) : ""}</td></tr>`;
      }).join("");
      const items = (d.line_items || []).map((li) => `<tr><td>${esc(li.description)}</td><td class="n">${li.quantity ?? "—"}</td>
        <td class="n">${inr(li.unit_price, true)}</td><td class="n">${inr(li.amount, true)}</td></tr>`).join("");
      const tax = (d.tax_lines || []).map((t) => `${esc(t.type)} ${t.rate ?? ""}% ${inr(t.amount, true)}`).join(" + ");
      return `<div class="small muted" style="margin-bottom:6px">${esc(d.mode === "ai+rules" ? `${d.model} read it in ${(d.llm_ms / 1000).toFixed(1)}s, cross-checked field by field against the rule-based parser.` : d.llm_error ? `${d.llm_error}, so the rule-based parser read it alone. The decision rules don't depend on the AI, so the outcome is the same.` : "Rule-based parser: labels, patterns and arithmetic. Set GEMINI_API_KEY (free tier) or ANTHROPIC_API_KEY to add an LLM as a second reader.")}</div>
        <table class="fields">${rows}</table>
        ${items ? `<div class="sub-h">Line items</div><table class="mini"><tr><th>Description</th><th class="n">Qty</th><th class="n">Rate</th><th class="n">Amount</th></tr>${items}</table>` : `<div class="sub-h">No line items could be read.</div>`}
        ${tax ? `<div class="small muted" style="margin-top:8px">Tax: ${tax}${d.tax_inclusive ? " (included in prices; backed out)" : ""}</div>` : ""}
        ${d.bank_change_notice ? `<div class="check hold" style="margin-top:8px"><span class="g">!</span><div class="check-msg">The invoice text announces a change of bank details.</div></div>` : ""}`;
    }
    case "vendor": {
      const v = d.vendor;
      return (v ? `<div class="kv" style="margin-bottom:10px"><span>Vendor</span><b>${esc(v.name)} (${esc(v.vendor_id)})</b>
        <span>Matched by</span><b>${esc(d.match_method)}${d.match_method === "name" ? ` (similarity ${Math.round(d.match_score * 100)}%)` : ""}</b>
        <span>Terms</span><b>Net ${v.payment_terms_days} days</b></div>` : "") + checksHtml(ev.checks);
    }
    case "po": {
      let html = "";
      const po = d.po;
      if (po) {
        const sub = state.fields?.subtotal || 0;
        const billed = d.billed_before || 0;
        const total = Math.max(po.subtotal, billed + sub);
        const pct = (x) => (100 * x / total).toFixed(2) + "%";
        const within = Math.max(0, Math.min(sub, po.subtotal - billed));
        const over = Math.max(0, billed + sub - po.subtotal);
        html += `<div class="kv"><span>PO</span><b>${esc(po.po_number)}: ${esc(po.description)}</b>
          <span>Found by</span><b>${esc(d.method)}</b><span>PO value</span><b>${inr(po.subtotal)} + ${Math.round(po.tax_rate * 100)}% GST</b></div>
          <div class="pobar" role="img" aria-label="PO balance"><i class="used" style="width:${pct(billed)}"></i><i class="this" style="width:${pct(within)}"></i><i class="over" style="width:${pct(over)}"></i></div>
          <div class="legend"><span><i style="background:var(--ink)"></i>Billed before ${inr(billed)}</span><span><i style="background:var(--ok)"></i>This invoice ${inr(sub)}</span>${over > 0.5 ? `<span><i style="background:var(--bad)"></i>Over PO ${inr(over)}</span>` : ""}<span>Tolerance ${inr(d.tolerance)}</span></div>`;
        if (d.prior_invoices?.length) {
          html += `<div class="sub-h">Earlier invoices on this PO</div><table class="mini"><tr><th>Invoice</th><th>Date</th><th class="n">Pre-tax</th><th>Status</th></tr>
            ${d.prior_invoices.map((p) => `<tr><td>${esc(p.invoice_number)}</td><td>${niceDate(p.invoice_date)}</td><td class="n">${inr(p.subtotal)}</td><td><span class="pill ${p.status}">${esc(p.status)}</span></td></tr>`).join("")}</table>`;
        }
      }
      const inf = d.inference;
      if (inf?.candidates?.length) {
        html += `<div class="sub-h">PO inference: vendor's open POs scored</div><table class="mini"><tr><th>PO</th><th>Description</th><th class="n">Amount fit</th><th class="n">Description</th><th class="n">Score</th></tr>
          ${inf.candidates.map((c) => `<tr><td>${esc(c.po_number)}${c.po_number === inf.chosen ? " ✓" : ""}</td><td>${esc(c.description)}</td><td class="n">${Math.round(c.amount_fit * 100)}%</td><td class="n">${Math.round(c.description_match * 100)}%</td><td class="n"><b>${c.score.toFixed(2)}</b></td></tr>`).join("")}</table>`;
      }
      const lm = (ev.checks || []).find((c) => c.id === "line_match");
      if (lm?.data?.lines?.length) {
        html += `<div class="sub-h">Line match (invoice vs PO)</div><table class="mini"><tr><th>Invoice line</th><th>PO item</th><th class="n">Qty</th><th class="n">Unit</th><th class="n">PO unit</th><th class="n">Var.</th></tr>
          ${lm.data.lines.map((l) => `<tr><td>${esc(l.invoice)}</td><td>${esc(l.po_item)}</td><td class="n">${l.inv_qty}/${l.po_qty}</td><td class="n">${inr(l.inv_unit, true)}</td><td class="n">${inr(l.po_unit)}</td><td class="n" style="color:${l.variance_pct > state.config.tolerance_pct ? "var(--bad)" : "inherit"}">${l.variance_pct > 0 ? "+" : ""}${l.variance_pct}%</td></tr>`).join("")}</table>`;
      }
      return html + `<div style="margin-top:10px">${checksHtml(ev.checks)}</div>`;
    }
    case "decide": {
      const c = d.decision.counts || {};
      return `<div class="small">Rules evaluated: ${["pass", "info", "warn", "hold", "reject"].filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(", ")}. The most severe outcome decides; nothing is averaged away.</div>`;
    }
    case "route":
      return `<div class="small">${esc(ev.summary)}</div>`;
    default:
      return checksHtml(ev.checks);
  }
}

/* ------------------------------------------------------------------ decision */
function finish(rec, animate) {
  const d = rec.decision || {};
  const inv = rec.invoice || {};
  const el = $("#decision");
  el.className = "decision " + d.status + (rec.review ? " reviewed-" + rec.review.action : "");
  const inferred = rec.po_meta?.method === "inferred";
  const cols = [
    ["Vendor", rec.vendor?.name || inv.vendor_name || "Unknown"],
    ["Invoice", inv.invoice_number || "No number"],
    ["Amount", inr(inv.total, true)],
    ["Purchase order", rec.po ? rec.po.po_number + (inferred ? " (inferred)" : "") : "None matched"],
    rec.review ? [rec.review.action === "release" ? "Payment date" : "Decided by", rec.review.action === "release" ? niceDate(rec.review.pay_on) : rec.review.reviewer]
      : d.status === "APPROVED" ? ["Payment date", niceDate(d.pay_on)] : ["With", d.owner || "AP"],
  ];
  const steps = rec.review
    ? (rec.review.action === "release"
      ? [`Pay in the run on ${niceDate(rec.review.pay_on)}, to the verified account on file.`, "No further action needed."]
      : ["Returned. The vendor can resubmit a corrected invoice; it won't be flagged as a duplicate."])
    : rec.next_steps || [];
  const email = rec.vendor_email;
  el.innerHTML = `
    <div class="d-top"><span class="d-status">${esc(rec.review ? (rec.review.action === "release" ? "Released by reviewer" : "Rejected by reviewer") : (STATUS_LABEL[d.status] || d.status))}</span>
      <span class="d-headline">${esc(d.status === "APPROVED" ? (inferred ? "PO inferred and matched" : "All checks passed") : rec.review ? "Originally held: " + d.headline : d.headline)}</span>
      ${d.owner && d.status !== "APPROVED" && !rec.review ? `<span class="d-owner">Assigned to <b>${esc(d.owner)}</b></span>` : ""}</div>
    <p class="d-summary">${esc(rec.summary || "")}${rec.summary_source === "ai" ? `<span class="ai-tag">written by ${esc(state.config.provider)} from the rule results</span>` : ""}</p>
    <div class="d-cols">${cols.map(([k, v]) => `<div><span>${k}</span><b>${esc(v)}</b></div>`).join("")}</div>
    <div class="d-sub">Next steps</div>
    <ol class="d-steps">${steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
    ${email && !rec.review ? `<div class="d-sub">Query to vendor (draft)</div>
      <div class="email"><div class="email-head"><span><b>Subject:</b> ${esc(email.subject)}</span><button class="btn-small" id="copyEmail">Copy</button></div><pre>${esc(email.body)}</pre></div>` : ""}
    ${reviewHtml(rec)}
    <details class="audit"><summary>Audit trail (${(rec.audit || []).length} entries)</summary>
      <ol class="audit-list">${(rec.audit || []).map((a) => `<li><span class="a-when">${esc(niceTime(a.at))}</span><b>${esc(a.actor)}</b> · ${esc(a.action)}<div class="a-detail">${esc(a.detail || "")}</div></li>`).join("")}</ol>
      <a class="btn-small" href="/api/runs/${encodeURIComponent(rec.id)}/audit.json" download>Download audit record (JSON)</a>
    </details>`;
  el.hidden = false;
  if (email && !rec.review) $("#copyEmail").onclick = () => navigator.clipboard.writeText(`Subject: ${email.subject}\n\n${email.body}`).then(() => toast("Draft copied"));
  bindReview(rec);
  stamp(rec, animate);
  if (animate === true && d.status && state.samples.some((x) => x.file === rec.filename)) {
    state.results[rec.filename] = d.status;
    paintResults();
  }
  if (animate !== "keep-scroll") $(".run").scrollTop = 0;
}

function stamp(rec, animate) {
  const d = rec.decision || {};
  const rv = rec.review;
  let cls = d.status, label = STAMP_LABEL[d.status], sub = d.status === "APPROVED" ? "Pay " + niceDate(d.pay_on) : d.headline;
  if (rv?.final === "RELEASED") { cls = "APPROVED"; label = "Released"; sub = "Pay " + niceDate(rv.pay_on); }
  if (rv?.final === "REJECTED_BY_REVIEWER") { cls = "REJECTED"; label = "Rejected"; sub = "by " + rv.reviewer; }
  if (!label) return;
  const st = $("#stamp");
  st.className = "stamp " + cls;
  st.innerHTML = `${esc(label)}<small>${esc(sub)}</small>`;
  st.hidden = false;
  if (animate) { void st.offsetWidth; st.classList.add("land"); }
}

function reviewHtml(rec) {
  const rv = rec.review;
  if (rv) {
    const released = rv.action === "release";
    return `<div class="review done ${released ? "ok" : "bad"}">
      <div class="r-title">${released ? `Released for payment on ${esc(niceDate(rv.pay_on))}` : "Rejected by reviewer"}</div>
      <div class="r-meta">${esc(rv.reviewer)} · ${esc(niceTime(rv.at))}</div>
      <blockquote>${esc(rv.note)}</blockquote>
      <div class="small muted">${esc(rv.ledger)}.</div></div>`;
  }
  const o = rec.review_allowed;
  if (!o || (!o.release && !o.reject)) return "";
  return `<div class="review">
    <div class="r-title">Your review</div>
    ${o.why_not ? `<p class="small r-why">Release isn't available: ${esc(o.why_not)}</p>` : ""}
    <label class="r-label" for="rvNote">Note for the audit trail</label>
    <textarea id="rvNote" rows="2" placeholder="${esc(o.hint || "What did you check, and with whom?")}"></textarea>
    <div class="r-row">
      <input id="rvName" aria-label="Reviewer name" placeholder="Your name" value="${esc(localGet("clearap.reviewer") || "")}">
      ${o.release ? `<button class="btn btn-ok" data-act="release">Release for payment</button>` : ""}
      <button class="btn btn-bad" data-act="reject">Reject invoice</button>
    </div></div>`;
}

function localGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }

function bindReview(rec) {
  $$("#decision .review [data-act]").forEach((b) => b.addEventListener("click", async () => {
    const note = $("#rvNote").value.trim();
    const reviewer = $("#rvName").value.trim();
    if (note.length < 10) { toast("Add a short note first. It goes into the audit trail."); $("#rvNote").focus(); return; }
    localSet("clearap.reviewer", reviewer);
    $$("#decision .review button").forEach((x) => (x.disabled = true));
    try {
      const updated = await api(`/api/runs/${encodeURIComponent(rec.id)}/review`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: b.dataset.act, note, reviewer }),
      });
      finish(updated, "keep-scroll");
      toast(b.dataset.act === "release" ? "Released for payment" : "Invoice rejected");
    } catch (e) {
      toast(e.message);
      $$("#decision .review button").forEach((x) => (x.disabled = false));
    }
  }));
}

async function replay(runId) {
  showView("process");
  const rec = await api(`/api/runs/${runId}`);
  beginRunView(runId, rec.filename, `Replayed from history · ${niceTime(rec.created_at)}`, `/api/runs/${runId}/file`);
  const story = state.samples.find((x) => x.file === rec.filename)?.story;
  $("#runStory").hidden = !story;
  $("#runStory").textContent = story || "";
  (rec.events || []).forEach(applyStage);
  $("#runClock").textContent = ((rec.duration_ms || 0) / 1000).toFixed(1) + "s";
  finish(rec, false);
}

/* ------------------------------------------------------------------ dashboard */
async function loadDashboard() {
  const [s, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  const k = [
    ["Invoices processed", s.runs, s.avg_ms ? `${(s.avg_ms / 1000).toFixed(1)}s average per invoice` : "none yet", ""],
    ["Approved with no human touch", s.touchless_rate != null ? Math.round(s.touchless_rate * 100) + "%" : "—", `${s.approved} of ${s.runs} invoice(s)`, "ok"],
    ["Value approved", inr(s.value_approved), s.released ? `incl. ${s.released} released by a reviewer` : "scheduled for payment", "ok"],
    ["Value waiting on a person", inr(s.value_held), `${s.review} invoice(s) on hold`, "hold"],
    ["Value stopped", inr(s.value_blocked), `${s.rejected} rejected`, "bad"],
  ];
  $("#kpis").innerHTML = k.map(([l, v, e, c]) => `<div class="kpi ${c}"><span>${l}</span><b>${esc(v)}</b><em>${esc(e)}</em></div>`).join("");
  renderValueBar(s);
  renderQueue(runs);
  const max = Math.max(1, ...s.reasons.map((r) => r[1]));
  $("#reasons").innerHTML = s.reasons.length ? s.reasons.map(([r, n]) => `<div class="reason"><div><span>${esc(r)}</span><b>${n}</b></div><div class="rb"><i style="width:${100 * n / max}%"></i></div></div>`).join("")
    : `<p class="muted small">Nothing has been held or rejected yet.</p>`;
  state.runs = runs;
  renderRuns();
}
function waited(iso) {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `${h} h ${mins % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

function renderQueue(runs) {
  const q = runs.filter((r) => r.decision === "NEEDS_REVIEW" && !r.review_status)
    .sort((a, b) => (b.total || 0) - (a.total || 0));
  $("#queueCount").textContent = q.length ? q.length : "";
  $(".queue").classList.toggle("is-empty", !q.length);
  $("#queueTbl").innerHTML = q.length
    ? `<tr><th>Invoice</th><th class="n">Amount</th><th>Why it's held</th><th>With</th><th class="n">Waiting</th><th></th></tr>` +
      q.map((r) => `<tr class="click" data-id="${esc(r.id)}" tabindex="0">
        <td><b>${esc(r.vendor || (r.reason === "Not an invoice" ? "Not an invoice" : "Unknown vendor"))}</b><div class="small muted">${esc(r.invoice_number || "No invoice number")}</div></td>
        <td class="n nw"><b>${inr(r.total, true)}</b></td>
        <td>${esc(r.reason || "")}</td>
        <td class="nw">${esc(r.owner || "AP")}</td>
        <td class="n nw muted">${esc(waited(r.created_at))}</td>
        <td class="n nw"><span class="open-link">Review ›</span></td></tr>`).join("")
    : `<tr><td class="empty-row queue-empty"><span class="ok-dot" aria-hidden="true">✓</span>Nothing is waiting. Every held invoice has been reviewed.</td></tr>`;
  $$("#queueTbl tr.click").forEach((tr) => {
    tr.addEventListener("click", () => replay(tr.dataset.id));
    tr.addEventListener("keydown", (e) => e.key === "Enter" && replay(tr.dataset.id));
  });
}

function renderValueBar(s) {
  const parts = [
    { k: "ok", label: "Approved for payment", v: s.value_approved, n: s.approved + s.released },
    { k: "hold", label: "Waiting on a person", v: s.value_held, n: s.review },
    { k: "bad", label: "Stopped", v: s.value_blocked, n: s.rejected },
  ];
  const total = parts.reduce((a, p) => a + (p.v || 0), 0);
  $("#valueTotal").textContent = total ? `${inr(total)} across ${s.runs} invoice(s)` : "";
  const bar = $("#valueBar");
  if (!total) {
    bar.innerHTML = `<div class="vb-empty">Process an invoice to see where the money goes.</div>`;
    bar.setAttribute("aria-label", "No invoices processed yet");
    $("#valueLegend").innerHTML = "";
    return;
  }
  const pct = (v) => (100 * v) / total;
  bar.innerHTML = parts.filter((p) => p.v > 0).map((p) =>
    `<i class="vb ${p.k}" style="flex-basis:${pct(p.v)}%" tabindex="0"
        title="${esc(p.label)}: ${esc(inr(p.v))} (${Math.round(pct(p.v))}%), ${p.n} invoice(s)"></i>`).join("");
  bar.setAttribute("aria-label", parts.map((p) => `${p.label} ${inr(p.v)}`).join(", "));
  $("#valueLegend").innerHTML = parts.map((p) => `<div class="vl"><i class="sw ${p.k}" aria-hidden="true"></i>
      <span>${esc(p.label)}</span><b>${esc(inr(p.v))}</b><em>${p.v ? Math.round(pct(p.v)) + "%" : "—"}</em></div>`).join("");
}

function renderRuns() {
  const finalOf = (r) => r.review_status === "RELEASED" ? "APPROVED" : r.review_status === "REJECTED_BY_REVIEWER" ? "REJECTED" : r.decision;
  const rows = (state.runs || []).filter((r) => !state.filter || finalOf(r) === state.filter);
  $("#runsTbl").innerHTML = `<tr><th>When</th><th>Invoice</th><th class="n">Amount</th><th>PO</th><th>Outcome</th><th>Why, and who has it</th><th class="n">Time</th></tr>` +
    (rows.length ? rows.map((r) => `<tr class="click" data-id="${esc(r.id)}" tabindex="0" title="Open this run: ${esc(r.filename)}">
      <td class="nw">${esc(niceTime(r.created_at))}</td>
      <td><b>${esc(r.vendor || (r.reason === "Not an invoice" ? "Not an invoice" : "Unknown vendor"))}</b><div class="small muted">${esc(r.invoice_number || "No invoice number")}</div></td>
      <td class="n nw">${inr(r.total, true)}</td><td class="nw">${esc(r.po_number || "—")}</td>
      <td class="nw"><span class="pill ${r.decision}">${esc(STATUS_LABEL[r.decision] || r.decision)}</span>${r.review_status ? `<div class="small ${r.review_status === "RELEASED" ? "txt-ok" : "txt-bad"}">→ ${r.review_status === "RELEASED" ? "Released" : "Rejected"} by reviewer</div>` : ""}</td>
      <td>${r.decision === "APPROVED" ? `<span class="muted">—</span>` : `${esc(r.reason)}<div class="small muted">${esc(r.owner || "")}</div>`}</td>
      <td class="n nw">${((r.duration_ms || 0) / 1000).toFixed(1)}s</td></tr>`).join("")
      : `<tr><td colspan="7" class="empty-row">No runs yet. Process an invoice and it shows up here.</td></tr>`);
  $$("#runsTbl tr.click").forEach((tr) => {
    tr.addEventListener("click", () => replay(tr.dataset.id));
    tr.addEventListener("keydown", (e) => e.key === "Enter" && replay(tr.dataset.id));
  });
}
$$("#filters button").forEach((b) => b.addEventListener("click", () => {
  $$("#filters button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.filter = b.dataset.f;
  renderRuns();
}));

/* ------------------------------------------------------------------ reference */
async function loadReference() {
  const r = await api("/api/reference");
  $("#poTbl").innerHTML = `<tr><th>PO</th><th>Vendor</th><th>Description</th><th class="n">Value (pre-tax)</th><th class="n">GST</th><th>Billed / remaining</th><th>Status</th></tr>` +
    r.pos.map((p) => {
      const used = Math.min(100, 100 * p.billed / p.subtotal);
      return `<tr><td class="nw"><b>${esc(p.po_number)}</b></td><td>${esc(p.vendor)}</td><td>${esc(p.description)}<div class="small muted">${p.lines.map((l) => `${l.quantity} × ${esc(l.item)} @ ${inr(l.unit_price)}`).join("; ")}</div></td>
        <td class="n">${inr(p.subtotal)}</td><td class="n">${Math.round(p.tax_rate * 100)}%</td>
        <td class="balance"><div class="pobar"><i class="used" style="width:${used}%"></i></div><span class="small">${inr(p.billed)} billed · ${inr(p.remaining)} left</span>${p.invoices.length ? `<div class="small muted">${p.invoices.map(esc).join(", ")}</div>` : ""}</td>
        <td><span class="pill ${p.status.replace(/\s+/g, "_")}">${esc(p.status)}</span></td></tr>`;
    }).join("");
  $("#vendorTbl").innerHTML = `<tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>State</th><th>Verified bank account</th><th>Terms</th><th>Status</th></tr>` +
    r.vendors.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td><b>${esc(v.name)}</b></td><td>${esc(v.gstin)}</td><td>${esc(v.state)}</td>
      <td>${esc(v.bank_name)} ••••${esc(v.bank_last4)} <span class="small muted">${esc(v.ifsc)}</span></td><td>Net ${v.payment_terms_days}</td>
      <td><span class="pill ${v.status}">${esc(v.status)}</span>${v.status_note ? `<div class="small muted">${esc(v.status_note)}</div>` : ""}</td></tr>`).join("");
  const vname = Object.fromEntries(r.vendors.map((v) => [v.vendor_id, v.name]));
  $("#ledgerTbl").innerHTML = `<tr><th>Vendor</th><th>Invoice no.</th><th>Date</th><th>PO</th><th class="n">Pre-tax</th><th class="n">Total</th><th>Status</th><th>Source</th></tr>` +
    r.ledger.map((l) => `<tr><td>${esc(vname[l.vendor_id] || l.vendor_id)}</td><td>${esc(l.invoice_number || "—")}</td><td>${niceDate(l.invoice_date)}</td><td>${esc(l.po_number || "—")}</td>
      <td class="n">${inr(l.subtotal, true)}</td><td class="n">${inr(l.total, true)}</td><td><span class="pill ${l.status}">${esc(l.status.replace("_", " "))}</span></td>
      <td>${l.source === "history" ? "Opening ledger" : "This session"}</td></tr>`).join("");
}

/* ------------------------------------------------------------------ policy */
async function loadPolicy() {
  try {
    const r = await api("/api/policy");
    fillPolicy(r.policy);
  } catch (_) { /* the static text still renders */ }
}

function fillPolicy(p) {
  $("#pfTol").value = p.tolerance_pct;
  $("#pfCap").value = p.tolerance_cap;
  $("#pfWin").value = p.duplicate_window_days;
  Object.assign(state.config, { tolerance_pct: p.tolerance_pct, tolerance_cap: p.tolerance_cap,
    duplicate_window_days: p.duplicate_window_days });
  renderHow();
}

function policyMsg(text, kind) {
  const m = $("#pfMsg");
  m.textContent = text;
  m.className = "pf-msg small " + (kind || "");
}

$("#policyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { tolerance_pct: Number($("#pfTol").value), tolerance_cap: Number($("#pfCap").value),
    duplicate_window_days: Number($("#pfWin").value) };
  if ([$("#pfTol"), $("#pfCap"), $("#pfWin")].some((i) => i.value.trim() === "")) return policyMsg("Fill in all three values.", "err");
  try {
    const r = await api("/api/policy", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    fillPolicy(r.policy);
    policyMsg("Saved. New runs use this policy.", "ok");
  } catch (err) {
    policyMsg(err.message, "err");
  }
});
$("#pfReset").addEventListener("click", async () => {
  const r = await api("/api/policy", { method: "DELETE" });
  fillPolicy(r.policy);
  policyMsg("Defaults restored.", "ok");
});

/* ------------------------------------------------------------------ how it works */
function renderHow() {
  $("#howFlow").innerHTML = state.config.stages.map((s, i) => `<div><b>${i + 1}</b><strong>${esc(s.title)}</strong><span>${esc(STAGE_HELP[s.id])}</span></div>`).join("");
  const c = state.config;
  $("#policy").innerHTML = `
    <dt>How tolerance works</dt><dd>${Number(c.tolerance_pct) === 0 || Number(c.tolerance_cap) === 0
      ? "No over-billing is allowed: any amount above the PO is held for a person."
      : `Over-billing up to ${c.tolerance_pct}% of the PO value (never more than ${inr(c.tolerance_cap)}) is approved with a note; beyond that it's held.`} Under-billing is never a problem.</dd>
    <dt>Extraction confidence floor</dt><dd>${Math.round(c.min_confidence * 100)}% on vendor, invoice number, date, total, GSTIN and bank account.</dd>
    <dt>Duplicate rules</dt><dd>Exact number (any format) → reject. Same vendor, amount and PO or date with a similar number → reject. Same vendor and amount within ${c.duplicate_window_days ?? 45} days → hold.</dd>
    <dt>Bank details</dt><dd>Must match the verified account in the vendor master. Any difference holds for treasury call-back on the number on file.</dd>
    <dt>Reading</dt><dd>${c.ai_enabled ? `${esc(c.provider)} (${esc(c.model)}) plus rule-based parser, cross-checked.` : "Rule-based parser and OCR. Add GEMINI_API_KEY (free tier) or ANTHROPIC_API_KEY to add an LLM as a second reader."}</dd>`;
}

/* ------------------------------------------------------------------ reset */
$("#resetBtn").addEventListener("click", async () => {
  if (state.batch) return toast("A batch is running. Let it finish first.");
  if (!confirm("Clear all runs and restore the opening ledger? Useful before a fresh demo.")) return;
  await api("/api/reset", { method: "POST" });
  if (state.es) state.es.close();
  clearInterval(state.clock);
  state.runId = null;
  $("#runView").hidden = true;
  $("#runEmpty").hidden = false;
  $("#stamp").hidden = true;
  setDoc(null);
  $$(".sample").forEach((x) => x.classList.remove("active"));
  state.results = {};
  paintResults();
  loadPolicy();
  policyMsg("", "");
  toast("Demo reset: ledger restored, run history cleared, policy back to defaults");
  const active = $(".tabs button[aria-selected='true']").dataset.view;
  if (active === "dashboard") loadDashboard();
  if (active === "reference") loadReference();
});

init().catch((e) => toast("Couldn't load: " + e.message));
