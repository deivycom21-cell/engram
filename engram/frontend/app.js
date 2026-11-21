// ENGRAM frontend — vanilla ES modules. All user/document content is inserted with
// textContent (never innerHTML), so imported documents can't inject markup or scripts.
import { BrainView } from "./brain.js";

const KINDS = ["fact", "idea", "event", "decision", "concept", "observation", "question", "project"];
const KIND_LABEL = { fact: "Fact", idea: "Idea", event: "Event", decision: "Decision", concept: "Concept",
  observation: "Observation", question: "Question", project: "Project" };
const REL_LABEL = {
  follows: "Next in document", references: "Explicit link", semantically_similar: "Similar meaning",
  shares_concepts: "Shared concepts", possible_duplicate: "Possible duplicate",
  possible_contradiction: "Possible contradiction", later_version: "Revisited later",
};
const REL_COLOR = {
  follows: "var(--r-structural)", references: "var(--r-explicit)", semantically_similar: "var(--r-inferred)",
  shares_concepts: "var(--r-inferred)", possible_duplicate: "var(--faint)",
  possible_contradiction: "var(--r-contradiction)", later_version: "var(--r-evolution)",
};
const kc = (k) => `var(--k-${k})`;
const stage = document.getElementById("stage");
let STATUS = null;
let brain = null;

// ------------------------------------------------------------------ DOM helpers
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.setAttribute("style", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const add = (el, ...c) => el.append(...c.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false));
const kindChip = (k) => h("span", { class: "kind", style: `--kc:${kc(k)}` }, KIND_LABEL[k] || k);
const fmtDate = (d) => d || "undated";
const short = (t, n = 160) => { t = (t || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n - 1) + "…" : t; };
const ago = (iso) => {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return iso.slice(0, 10);
};
// Snippets from FTS mark hits with ⟦ ⟧; turn them into <mark> nodes safely.
function highlighted(text) {
  const frag = document.createDocumentFragment();
  for (const part of (text || "").split(/(⟦[^⟧]*⟧)/)) {
    if (part.startsWith("⟦") && part.endsWith("⟧")) frag.append(h("mark", {}, part.slice(1, -1)));
    else if (part) frag.append(document.createTextNode(part));
  }
  return frag;
}
function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = ""), err ? 5000 : 2600);
}
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}
function provLine(g) {
  const p = g.provenance;
  const loc = p.page ? `page ${p.page}` : p.section ? p.section : null;
  return h("span", {}, h("a", { href: `#/engram/${g.id}` }, p.document_title || p.filename),
    loc ? h("span", { class: "faint" }, "  ", loc) : null);
}
// An empty selection means "all types": shown as every chip on, and restored when all are re-enabled.
function kindFilter(selected, onChange) {
  const wrap = h("div", { class: "kfilter", role: "group", "aria-label": "Knowledge types" });
  const all = selected.size === 0;
  for (const k of KINDS) {
    const input = h("input", { type: "checkbox", value: k, checked: all || selected.has(k) });
    input.addEventListener("change", () => {
      if (selected.size === 0) KINDS.forEach((x) => selected.add(x));
      input.checked ? selected.add(k) : selected.delete(k);
      if (selected.size === KINDS.length) selected.clear();
      onChange(selected);
    });
    add(wrap, h("label", { style: `--kc:${kc(k)}` }, input, KIND_LABEL[k]));
  }
  return wrap;
}
function setPage(...nodes) {
  if (brain && !(nodes[0] && nodes[0].classList && nodes[0].classList.contains("brain"))) { brain.dispose(); brain = null; }
  stage.replaceChildren(...nodes);
  stage.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}
const loading = (what) => h("div", { class: "page" }, h("p", { class: "muted" }, `Loading ${what}…`));
function errorView(err) {
  return h("div", { class: "page" }, h("div", { class: "empty" }, h("h2", {}, "Something went wrong"),
    h("p", {}, err.message || String(err)), h("p", { class: "faint" }, "Check the terminal running `engram serve` for details.")));
}

// ------------------------------------------------------------------ overview
async function viewOverview() {
  setPage(loading("overview"));
  const o = await api("/overview");
  const s = o.stats;
  const total = s.engrams || 0;
  const page = h("div", { class: "page" });
  page.append(h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Your memory"),
      h("p", {}, total ? `${total} engrams from ${s.documents} documents, stored only on this computer in ${o.status.data_dir}.`
        : "Nothing remembered yet. Import notes, PDFs or text files to begin.")),
    h("div", {}, h("button", { class: "btn", onclick: runConsolidation }, "Run consolidation"))));

  if (total) {
    page.append(h("div", { class: "readouts" },
      [["engrams", s.engrams], ["documents", s.documents], ["relations", s.relations], ["concepts", s.concepts],
       ["open insights", s.insights_open]].map(([l, v]) => h("div", { class: "readout" }, h("b", {}, v), h("span", {}, l))),
      h("div", { class: "readout" }, h("b", { class: "num", style: "font-size:20px;line-height:2" },
        s.first_date ? `${s.first_date} → ${s.last_date}` : "—"), h("span", {}, "time span of dated memories"))));
    const spec = h("div", { class: "spectrum", role: "img", "aria-label": "Engrams by knowledge type" });
    const legend = h("div", { class: "legend" });
    for (const k of KINDS) {
      const n = s.engrams_by_kind[k] || 0;
      if (!n) continue;
      spec.append(h("i", { style: `--kc:${kc(k)};width:${(100 * n) / total}%`, title: `${KIND_LABEL[k]}: ${n}` }));
      legend.append(h("span", { class: "kind", style: `--kc:${kc(k)}` }, `${KIND_LABEL[k]} `, h("span", { class: "num faint" }, n)));
    }
    page.append(h("section", { class: "block" }, h("h2", {}, "Composition", h("small", {}, "by knowledge type")), spec, legend));
  }

  const left = h("div");
  const right = h("div");
  left.append(uploadBlock());
  if (o.documents.length) {
    const tbody = h("tbody");
    for (const d of o.documents) {
      tbody.append(h("tr", { class: "clickable", tabindex: 0, onclick: () => openDocument(d.id),
        onkeydown: (e) => e.key === "Enter" && openDocument(d.id) },
        h("td", {}, h("div", { style: "color:var(--bone)" }, d.title), h("div", { class: "faint", style: "font-size:12.5px" },
          d.filename, d.previous_version_id ? "  · new version" : "")),
        h("td", {}, d.kind), h("td", { class: "num" }, d.authored_at || "—"), h("td", { class: "r num" }, d.engram_count)));
    }
    left.append(h("section", { class: "block" }, h("h2", {}, "Sources", h("small", {}, "every engram links back to one of these")),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Document"), h("th", {}, "Type"), h("th", {}, "Dated"),
        h("th", { class: "r" }, "Engrams"))), tbody)));
  }
  if (o.top_concepts.length) {
    right.append(h("section", { class: "block" }, h("h2", {}, "Recurring concepts"),
      h("div", {}, o.top_concepts.map((c) => h("button", { class: "chip " + c.kind, title: `${c.engrams} engrams in ${c.documents} documents`,
        onclick: () => (location.hash = `#/search?q=${encodeURIComponent(c.name)}`) }, c.name, " ", h("span", { class: "num" }, c.documents))))));
  }
  right.append(h("section", { class: "block" }, h("h2", {}, "Recent activity"),
    o.activity.length ? h("ul", { class: "ledger" }, o.activity.slice(0, 14).map((a) =>
      h("li", {}, h("time", { datetime: a.at }, ago(a.at)), h("span", {}, describeActivity(a)))))
      : h("p", { class: "muted" }, "No activity yet.")));
  right.append(h("section", { class: "block" }, h("h2", {}, "Keep a copy"),
    h("p", { class: "muted", style: "font-size:14px" }, "Backups include the database and every original file. Exports are portable JSON."),
    h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
      h("button", { class: "btn", onclick: async (e) => {
        e.target.disabled = true;
        try { const r = await api("/backup", { method: "POST" }); toast(`Backup saved: ${r.path.split(/[\\/]/).pop()}`); }
        catch (err) { toast(err.message, true); } finally { e.target.disabled = false; } } }, "Create backup"),
      h("a", { class: "btn", href: "/api/export", download: "" }, "Export JSON"))));
  page.append(h("div", { class: "cols" }, left, right));
  setPage(page);
}
function describeActivity(a) {
  const d = a.detail || {};
  switch (a.action) {
    case "document.ingested": return `Imported ${d.filename} — ${d.engrams} engrams${d.previous_version_id ? " (new version)" : ""}`;
    case "consolidation.run": return `Consolidation: ${Object.values(d.new_relations || {}).reduce((x, y) => x + y, 0)} new relations proposed, nothing deleted`;
    case "reconstruction": return `Reconstructed “${short(d.question, 60)}” from ${d.evidence} fragments`;
    case "relation.status_changed": return `Relation ${d.relation_id} ${d.status}`;
    case "insight.status_changed": return `Insight ${d.insight_id} ${d.status}`;
    case "engram.kind_changed": return `Engram ${d.engram_id} retyped as ${d.kind}`;
    case "engram.status_changed": return `Engram ${d.engram_id} ${d.status}`;
    case "backup.created": return `Backup ${d.path}`;
    case "export.created": return `Export ${d.path}`;
    default: return a.action;
  }
}
async function runConsolidation(e) {
  const btn = e?.target; if (btn) btn.disabled = true;
  try {
    const r = await api("/consolidate", { method: "POST" });
    const rel = Object.values(r.new_relations).reduce((a, b) => a + b, 0);
    const ins = Object.values(r.new_insights).reduce((a, b) => a + b, 0);
    toast(`Consolidated in ${r.seconds}s: ${rel} new relations, ${ins} new insights, nothing deleted`);
    refreshBadge(); route();
  } catch (err) { toast(err.message, true); } finally { if (btn) btn.disabled = false; }
}
async function openDocument(id) {
  const d = await api(`/documents/${id}`);
  if (d.engrams.length) location.hash = `#/engram/${d.engrams[0].id}`;
}

function uploadBlock() {
  const log = h("ul", { class: "upload-log" });
  const input = h("input", { type: "file", multiple: true, accept: ".pdf,.md,.markdown,.txt,.text,.rst,.org,.log", hidden: true });
  const drop = h("div", { class: "drop" },
    h("p", {}, h("strong", {}, "Drop PDF, Markdown or text files here"), " or ",
      h("button", { class: "btn small", onclick: () => input.click() }, "Choose files")),
    h("p", { class: "faint", style: "font-size:13px;margin:0" },
      "Files are copied into your local data folder and never executed. Identical files are recognised and skipped."),
    input, log);
  const send = async (files) => {
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    try {
      const r = await api("/ingest", { method: "POST", body: fd });
      for (const j of r.jobs) {
        const li = h("li", {}, h("span", { class: "faint" }, "…"), h("span", {}, j.filename));
        log.append(li);
        pollJob(j.job_id, li);
      }
    } catch (err) { log.append(h("li", { class: "err" }, err.message)); }
  };
  input.addEventListener("change", () => send([...input.files]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); send([...e.dataTransfer.files]); });
  return h("section", { class: "block" }, h("h2", {}, "Import"), drop);
}
async function pollJob(id, li) {
  for (let i = 0; i < 600; i++) {
    const j = await api(`/jobs/${id}`);
    if (j.status === "done" || j.status === "failed") {
      const r = j.result || {};
      const ok = j.status === "done";
      const msg = r.status === "duplicate" ? "already in memory" : ok ? `${r.engrams} engrams` : (j.error || "failed");
      li.replaceChildren(h("span", { class: ok ? "ok" : "err" }, ok ? "✓" : "✗"), h("span", {}, r.filename || ""),
        h("span", { class: "faint" }, msg));
      if (ok && r.status === "ingested") { refreshBadge(); setTimeout(() => location.hash === "#/overview" && viewOverview(), 900); }
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

// ------------------------------------------------------------------ search & reconstruct
async function viewSearch(params) {
  const mode = params.get("mode") || "recall";
  const q = params.get("q") || "";
  const kinds = new Set((params.get("kinds") || "").split(",").filter(Boolean));
  const input = h("input", { class: "input big", type: "search", value: q, "aria-label": "Question",
    placeholder: mode === "recall" ? "What did I think about…" : "Why did I…  /  How did … change?" });
  const go = () => {
    const p = new URLSearchParams({ q: input.value.trim(), mode });
    if (kinds.size && mode === "recall") p.set("kinds", [...kinds].join(","));
    location.hash = `#/search?${p}`;
  };
  input.addEventListener("keydown", (e) => e.key === "Enter" && go());
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Mode" },
    ["recall", "reconstruct"].map((m) => h("button", { "aria-pressed": String(m === mode), onclick: () => {
      const p = new URLSearchParams({ q: input.value.trim(), mode: m }); location.hash = `#/search?${p}`; } },
      m === "recall" ? "Recall" : "Reconstruct")));
  const out = h("div");
  const page = h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, mode === "recall" ? "Memory search" : "Reconstruct"),
      h("p", {}, mode === "recall"
        ? "Ask in your own words. Results combine keywords, meaning, relations between memories and dates — each one shows where it came from."
        : "Rebuild how a decision or idea came to be: origin, documents, decisions, changes, chronology, and what the sources cannot tell.")), seg),
    h("div", { class: "searchbar" }, input, h("button", { class: "btn primary", onclick: go }, mode === "recall" ? "Search" : "Reconstruct")),
    mode === "recall" ? kindFilter(kinds, () => q && go()) : null, out);
  setPage(page);
  input.focus();
  if (!q) {
    out.append(h("div", { class: "empty" }, h("p", {}, mode === "recall"
      ? "Try “¿Qué había pensado sobre construir una herramienta para agentes?” or a concept from the Overview."
      : "Try “¿Por qué abandoné mi anterior arquitectura?”")));
    return;
  }
  out.append(h("p", { class: "muted" }, mode === "recall" ? "Searching…" : "Reconstructing from evidence…"));
  try {
    if (mode === "recall") renderResults(out, await api(`/search?${new URLSearchParams({ q, limit: 25, ...(kinds.size ? { kinds: [...kinds].join(",") } : {}) })}`));
    else renderReconstruction(out, await api("/reconstruct", { method: "POST", body: { question: q } }), q);
  } catch (err) { out.replaceChildren(h("p", { class: "err" }, err.message)); }
}
function analysisLine(a, sig) {
  const bits = [h("b", {}, "Matched on "), a.terms.length ? a.terms.join(", ") : "meaning only"];
  if (a.kind_hints.length) bits.push(" · favouring ", a.kind_hints.join(", "));
  if (a.prefer) bits.push(" · favouring ", a.prefer === "older" ? "earlier records" : "recent records");
  if (a.date_from) bits.push(" · period ", a.date_from, a.date_to && a.date_to !== a.date_from ? `–${a.date_to}` : "");
  if (sig) bits.push(` · ${sig.fulltext_hits} keyword hits, ${sig.vector_hits} ${sig.vector_neural ? "semantic" : "lexical-vector"} hits`);
  return h("p", { class: "analysis" }, bits);
}
function renderResults(out, res) {
  out.replaceChildren(analysisLine(res.analysis, res.signals));
  if (!res.results.length) {
    out.append(h("div", { class: "empty" }, h("h2", {}, "No memories match"),
      h("p", {}, "Nothing in your imported sources is close to this question. Try other words, or import the notes that would contain it.")));
    return;
  }
  for (const r of res.results) {
    const g = r.engram;
    out.append(h("article", { class: "result" },
      h("div", { class: "relevance" }, h("span", { class: "num" }, r.relevance.toFixed(2)),
        h("div", { class: "meter", title: "relative relevance" }, h("i", { style: `width:${Math.round(r.relevance * 100)}%` }))),
      h("div", {},
        h("div", { class: "src" }, kindChip(g.kind), h("span", { class: "num" }, fmtDate(g.occurred_at || g.created_at)), provLine(g)),
        h("div", { class: "body" }, r.snippet && r.signals.fts_rank !== null && g.content.length > 420 ? highlighted(r.snippet) : short(g.content, 420)),
        h("div", { class: "why" }, h("ul", {}, r.why.map((w) => h("li", {}, w)))))));
  }
}
function citeText(text, onCite) {
  const frag = document.createDocumentFragment();
  for (const part of text.split(/(\[E\d+\])/)) {
    const m = part.match(/^\[(E\d+)\]$/);
    if (m) frag.append(h("button", { class: "cite", title: `Show evidence ${m[1]}`, onclick: () => onCite(m[1]) }, m[1]));
    else if (part) frag.append(document.createTextNode(part));
  }
  return frag;
}
function renderReconstruction(out, rec, question) {
  const jump = (label) => {
    const el = document.getElementById(`ev-${label}`);
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  };
  const wrap = h("div", { class: "recon" });
  add(wrap, analysisLine(rec.analysis), h("p", { class: "faint", style: "font-size:13px" }, rec.method));
  add(wrap, h("ul", { class: "summary" }, rec.summary.map((l) => h("li", {}, citeText(l, jump)))));
  if (STATUS?.llm?.configured && STATUS.llm.configured !== "none") {
    const nbox = h("div");
    const external = STATUS.llm.configured === "claude-cli";
    add(wrap, h("h2", {}, "Narrative"), nbox);
    nbox.append(h("button", { class: "btn", onclick: async (e) => {
      if (external && !confirm("This sends the evidence fragments to an external service (Claude CLI → Anthropic). Continue?")) return;
      e.target.disabled = true;
      try {
        const r = await api("/reconstruct", { method: "POST", body: { question, narrate: true, consent_external: external } });
        const n = r.narrative || {};
        nbox.replaceChildren(h("div", { class: "narrative" }, n.error ? h("p", { class: "err" }, n.error) : [
          h("div", { class: "warn" }, n.warning, n.invalid_citations?.length ? ` Unknown citations: ${n.invalid_citations.join(", ")}.` : ""),
          h("div", { style: "white-space:pre-wrap" }, citeText(n.text || "", jump))]));
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    } }, external ? "Write a narrative with Claude CLI (sends evidence)" : "Write a narrative with the local model"));
  }
  if (rec.original_idea) {
    const o = rec.original_idea;
    add(wrap, h("h2", {}, "Origin"), h("p", {}, h("button", { class: "cite", onclick: () => jump(o.label) }, o.label), " ",
      h("span", { class: "num" }, o.date), "  ", kindChip(o.kind), "  ", h("span", { class: "faint" }, o.document)),
      h("p", { style: "color:var(--bone);max-width:80ch" }, `“${o.excerpt}”`), h("p", { class: "faint", style: "font-size:12.5px" }, `Date from ${o.date_source}`));
  }
  if (rec.chronology.length) {
    add(wrap, h("h2", {}, "Chronology"), h("ol", { class: "chrono" }, rec.chronology.map((c) =>
      h("li", {}, h("span", { class: "num" }, c.date), h("span", { class: "tick", style: `--kc:${kc(c.kind)}` }),
        h("div", {}, h("button", { class: "cite", onclick: () => jump(c.label) }, c.label), " ", kindChip(c.kind), "  ",
          h("span", { class: "faint" }, c.document), h("div", { style: "color:var(--bone)" }, c.excerpt),
          h("div", { class: "src-note" }, `date from ${c.date_source}`))))));
  }
  if (rec.undated.length) {
    add(wrap, h("p", { class: "muted", style: "margin-top:10px" }, "Not placeable in time: ",
      rec.undated.map((u) => h("button", { class: "cite", onclick: () => jump(u.label) }, u.label))));
  }
  if (rec.decisions.length) {
    add(wrap, h("h2", {}, "Decisions recorded"), rec.decisions.map((d) => h("div", { class: "change" },
      h("button", { class: "cite", onclick: () => jump(d.label) }, d.label), " ", h("span", { class: "num" }, fmtDate(d.date)), "  ", d.excerpt)));
  }
  if (rec.reasons.length) {
    add(wrap, h("h2", {}, "Reasons stated in the sources"), rec.reasons.map((r) => h("div", { class: "change" },
      h("button", { class: "cite", onclick: () => jump(r.label) }, r.label), " “", r.sentence, "”")));
  }
  if (rec.changes.length) {
    add(wrap, h("h2", {}, "Changes of mind", h("small", { class: "faint", style: "font:400 13px var(--sans);margin-left:8px" }, "inferred by ENGRAM — review before trusting")),
      rec.changes.map((c) => h("div", { class: "change" },
        h("div", { class: "type" }, REL_LABEL[c.type], " · ", c.status),
        h("button", { class: "cite", onclick: () => jump(c.from) }, c.from), ` ${c.from_date || "undated"} → `,
        h("button", { class: "cite", onclick: () => jump(c.to) }, c.to), ` ${c.to_date || "undated"}`,
        h("p", { class: "muted", style: "font-size:13px;margin:4px 0 0" }, c.explanation))));
  }
  add(wrap, h("h2", {}, "What cannot be determined"), h("ul", { class: "unknowns" }, rec.unknowns.map((u) => h("li", {}, u))));
  if (rec.documents.length) {
    add(wrap, h("h2", {}, "Documents involved"), h("table", {}, h("tbody", {}, rec.documents.map((d) => h("tr", {},
      h("td", {}, d.title, h("div", { class: "faint", style: "font-size:12.5px" }, d.filename)),
      h("td", { class: "num" }, d.date_range ? d.date_range.join(" → ") : "undated"),
      h("td", {}, d.labels.map((l) => h("button", { class: "cite", onclick: () => jump(l) }, l))))))));
  }
  add(wrap, h("h2", {}, "Evidence"), h("div", { class: "evidence" }, rec.evidence.map((ev) => {
    const g = ev.engram, p = g.provenance;
    return h("article", { id: `ev-${ev.label}` },
      h("div", {}, h("span", { class: "label" }, ev.label), kindChip(ev.kind), "  ", h("span", { class: "num" }, fmtDate(ev.date)), "  ",
        h("a", { href: `#/engram/${g.id}` }, p.document_title), h("span", { class: "faint" },
          `  ${p.filename}${p.page ? ` · page ${p.page}` : ""}${p.section ? ` · ${p.section}` : ""} · chars ${p.char_start}–${p.char_end}`)),
      h("div", { class: "body" }, g.content),
      ev.date_source ? h("div", { class: "faint", style: "font-size:12px;margin-top:4px" }, `date from ${ev.date_source}`) : null);
  })));
  out.replaceChildren(wrap);
}

// ------------------------------------------------------------------ timeline
async function viewTimeline(params) {
  const topic = params.get("q") || "";
  const kinds = new Set((params.get("kinds") || "").split(",").filter(Boolean));
  const input = h("input", { class: "input", type: "search", value: topic, placeholder: "Follow one topic over time (optional)", style: "max-width:380px" });
  const go = () => {
    const p = new URLSearchParams();
    if (input.value.trim()) p.set("q", input.value.trim());
    if (kinds.size) p.set("kinds", [...kinds].join(","));
    location.hash = `#/timeline?${p}`;
  };
  input.addEventListener("keydown", (e) => e.key === "Enter" && go());
  const out = h("div");
  setPage(h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Timeline"),
      h("p", {}, "Memories placed by the dates written in your sources. Each date shows where it came from; nothing is dated by import time."))),
    h("div", { class: "tl-controls" }, input, h("button", { class: "btn", onclick: go }, topic ? "Follow topic" : "Filter"),
      topic ? h("button", { class: "btn ghost", onclick: () => { input.value = ""; go(); } }, "Show everything") : null),
    topic ? null : h("div", { style: "margin:-10px 0 24px" }, kindFilter(kinds, go)), out));
  out.append(h("p", { class: "muted" }, "Loading…"));
  if (topic) {
    const ev = await api(`/evolution?q=${encodeURIComponent(topic)}`);
    out.replaceChildren();
    if (ev.first_appearance) {
      const f = ev.first_appearance;
      out.append(h("div", { class: "evo-head" }, h("h2", {}, `First appears ${f.date}`),
        h("p", { class: "muted" }, `In “${f.engram.provenance.document_title}” — date from ${f.date_source}. `,
          ev.latest && ev.latest.date !== f.date ? `Last mentioned ${ev.latest.date}.` : "",
          ` ${ev.decisions.length} decision(s), ${ev.changes.filter((c) => c.type !== "references").length} inferred change(s).`)));
    }
    ev.notes.forEach((n) => out.append(h("p", { class: "muted" }, n)));
    renderMonths(out, ev.chronology);
    if (ev.undated.length) out.append(h("section", { class: "block" }, h("h2", { style: "margin:20px 0 8px" }, "Related but undated"),
      ev.undated.map((g) => tlItem(null, null, g))));
  } else {
    const t = await api(`/timeline?${new URLSearchParams(kinds.size ? { kinds: [...kinds].join(",") } : {})}`);
    out.replaceChildren();
    if (!t.items.length) out.append(h("div", { class: "empty" }, h("h2", {}, "No dated memories"),
      h("p", {}, "Dates come from front-matter, headings, PDF metadata, file names or dates written in the text. Import notes that contain them.")));
    renderMonths(out, t.items);
    if (t.undated_count) out.append(h("p", { class: "faint", style: "margin-top:16px" }, `${t.undated_count} engram(s) have no recorded date and are not shown.`));
  }
}
function renderMonths(out, items) {
  const groups = new Map();
  for (const it of items) {
    const key = it.date.slice(0, 7);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }
  for (const [month, list] of groups) {
    const label = new Date(`${month}-01T00:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "long" });
    out.append(h("div", { class: "tl-month" }, h("h3", {}, label), h("div", {}, list.map((it) => tlItem(it.date, it.date_source, it.engram)))));
  }
}
function tlItem(date, src, g) {
  return h("div", { class: "tl-item", tabindex: 0, role: "link", onclick: () => (location.hash = `#/engram/${g.id}`),
    onkeydown: (e) => e.key === "Enter" && (location.hash = `#/engram/${g.id}`) },
    h("span", { class: "num faint" }, date || "—"),
    h("div", {}, h("div", {}, kindChip(g.kind), "  ", h("span", { class: "txt" }, short(g.content, 180))),
      h("div", { class: "meta" }, g.provenance.document_title, src ? ` · date from ${src}` : "")));
}

// ------------------------------------------------------------------ inspector
async function viewEngram(id) {
  if (!id) {
    const list = await api("/engrams?limit=1&order=recent");
    if (list.items.length) { location.hash = `#/engram/${list.items[0].id}`; return; }
    setPage(h("div", { class: "page" }, h("div", { class: "empty" }, h("h1", {}, "Inspector"),
      h("p", {}, "Import a document first; then open any engram from search, the timeline or the brain explorer."))));
    return;
  }
  setPage(loading("engram"));
  const d = await api(`/engrams/${id}`);
  const g = d.engram, p = g.provenance, doc = d.document;
  const kindSel = h("select", { class: "input", "aria-label": "Knowledge type", onchange: async (e) => {
    await api(`/engrams/${id}`, { method: "PATCH", body: { kind: e.target.value } }); toast(`Retyped as ${KIND_LABEL[e.target.value]}`); viewEngram(id);
  } }, KINDS.map((k) => h("option", { value: k, selected: k === g.kind }, KIND_LABEL[k])));
  const left = h("div", {},
    h("div", { class: "content-block", style: `--kc:${kc(g.kind)}` }, g.content),
    h("p", { class: "faint", style: "font-size:13px;margin-top:10px" },
      `Classified as ${KIND_LABEL[g.kind]} (${g.kind_reason || "—"}, confidence ${g.kind_confidence ?? "—"}). `,
      "Verbatim text from the source; ENGRAM never rewrites it."),
    g.concepts.length ? h("section", { class: "block", style: "margin-top:18px" }, h("h3", { style: "margin-bottom:8px" }, "Concepts and entities"),
      g.concepts.map((c) => h("button", { class: "chip " + c.kind, title: `${c.kind}, ${c.origin}`,
        onclick: () => (location.hash = `#/search?q=${encodeURIComponent(c.name)}`) }, c.name))) : null,
    relationsBlock(d.relations, id));
  const right = h("div", {},
    h("section", { class: "block" }, h("h2", { style: "margin-bottom:12px" }, "Provenance"),
      h("dl", { class: "prov" },
        h("dt", {}, "Document"), h("dd", {}, p.document_title),
        h("dt", {}, "File"), h("dd", {}, p.filename, "  ", h("a", { href: `/api/documents/${p.document_id}/original` }, "download original")),
        p.page ? [h("dt", {}, "Page"), h("dd", { class: "num" }, p.page)] : null,
        p.section ? [h("dt", {}, "Section"), h("dd", {}, p.section)] : null,
        h("dt", {}, "Characters"), h("dd", { class: "num" }, `${p.char_start}–${p.char_end}`),
        h("dt", {}, "Position"), h("dd", { class: "num" }, `fragment ${p.position + 1} of ${doc?.engram_count ?? "?"}`),
        h("dt", {}, "SHA-256"), h("dd", { class: "num" }, (p.sha256 || "").slice(0, 16) + "…"),
        h("dt", {}, "Event date"), h("dd", {}, g.occurred_at ? [h("span", { class: "num" }, g.occurred_at), h("div", { class: "faint" }, g.occurred_at_source)] : "none written"),
        h("dt", {}, "Source date"), h("dd", {}, g.created_at ? [h("span", { class: "num" }, g.created_at), h("div", { class: "faint" }, g.created_at_source)] : "unknown"),
        h("dt", {}, "Imported"), h("dd", { class: "num" }, g.ingested_at.replace("T", " ").slice(0, 16)),
        h("dt", {}, "Text fidelity"), h("dd", {}, g.confidence === 1 ? "verbatim" : `${g.confidence} (extracted from PDF)`),
        h("dt", {}, "Vector"), h("dd", {}, g.has_embedding ? "embedded" : "none"),
        h("dt", {}, "Opened"), h("dd", {}, `${g.access_count} time(s)`))),
    h("section", { class: "block" }, h("h2", { style: "margin-bottom:10px" }, "Type and status"),
      h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" }, kindSel,
        h("button", { class: "btn", onclick: () => (location.hash = `#/brain?focus=${id}`) }, "Show in brain"),
        h("button", { class: "btn ghost", onclick: async () => {
          const next = g.status === "active" ? "archived" : "active";
          await api(`/engrams/${id}`, { method: "PATCH", body: { status: next } });
          toast(next === "archived" ? "Archived: hidden from search, still stored" : "Restored"); viewEngram(id);
        } }, g.status === "active" ? "Archive" : "Restore")),
      g.status !== "active" ? h("p", { class: "muted", style: "margin-top:8px" }, "Archived. Hidden from search and the graph, but kept intact.") : null),
    d.document_versions.length > 1 ? h("section", { class: "block" }, h("h2", { style: "margin-bottom:8px" }, "Versions of this file"),
      h("ul", { class: "ledger" }, d.document_versions.map((v) => h("li", {}, h("time", {}, v.ingested_at.slice(0, 10)),
        h("span", {}, v.id === doc.id ? "this version" : h("a", { href: "#", onclick: (e) => { e.preventDefault(); openDocument(v.id); } }, `document ${v.id}`),
          h("span", { class: "faint num" }, `  ${v.sha256}`)))))) : null);
  setPage(h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("div", { style: "margin-bottom:8px" }, kindChip(g.kind), "  ", h("span", { class: "num faint" }, `engram ${g.id} · ${g.uid}`)),
      h("h1", { style: "font-size:26px" }, p.section || p.document_title))),
    h("div", { class: "insp" }, left, right)));
}
function relationsBlock(rels, id) {
  const groups = { explicit: [], inferred: [], structural: [] };
  rels.forEach((r) => groups[r.origin].push(r));
  const titles = { explicit: ["Documented links", "written in your sources"], inferred: ["Proposed by ENGRAM", "review: confirm or reject"],
    structural: ["Document structure", "neighbouring fragments"] };
  const wrap = h("section", { class: "block rel-group" }, h("h2", {}, "Relations"));
  if (!rels.length) add(wrap, h("p", { class: "muted" }, "No relations yet. Import related documents or run consolidation."));
  for (const origin of ["explicit", "inferred", "structural"]) {
    if (!groups[origin].length) continue;
    add(wrap, h("h3", {}, titles[origin][0], h("small", {}, titles[origin][1])));
    for (const r of groups[origin]) {
      const actions = origin === "inferred" ? h("span", { style: "margin-left:auto;display:flex;gap:6px" },
        r.status !== "confirmed" ? h("button", { class: "btn small", onclick: () => setRel(r.id, "confirmed", id) }, "Confirm") : null,
        r.status !== "rejected" ? h("button", { class: "btn small ghost", onclick: () => setRel(r.id, "rejected", id) }, "Reject") : null) : null;
      const dashed = r.origin === "inferred" ? " dashed" : "";
      add(wrap, h("div", { class: `rel ${r.status}` },
        h("div", { class: "rtype" }, h("i", { class: "sw" + dashed, style: `--rc:${REL_COLOR[r.type]}` }), REL_LABEL[r.type] || r.type,
          h("span", { class: "faint" }, r.direction === "out" ? "→" : "←"),
          origin === "inferred" ? h("span", { class: `status-pill ${r.status}` }, r.status) : null,
          h("span", { class: "num faint" }, r.confidence.toFixed(2)), actions),
        h("div", { class: "expl" }, r.explanation),
        h("a", { class: "other", href: `#/engram/${r.other.id}` }, r.other.kind ? kindChip(r.other.kind) : null, "  ",
          short(r.other.preview, 140), h("span", { class: "faint" }, `  — ${r.other.document || ""}${r.other.date ? `, ${r.other.date}` : ""}`))));
    }
  }
  return wrap;
}
async function setRel(relId, status, engramId) {
  try { await api(`/relations/${relId}`, { method: "PATCH", body: { status } }); toast(status === "confirmed" ? "Relation confirmed" : "Relation rejected — it won't be proposed again"); viewEngram(engramId); }
  catch (err) { toast(err.message, true); }
}

// ------------------------------------------------------------------ insights
const INSIGHT_LABEL = { bridge: ["Connection", "three documents linked through one memory"], contradiction: ["Contradiction", "possibly conflicting statements"],
  duplicate: ["Duplicate", "same content in two places"], forgotten: ["Forgotten link", "old memory related to recent work"],
  theme: ["Recurring theme", "appears across documents"], fragmented: ["Fragmented", "short notes on one topic"] };
async function viewInsights(params) {
  const kind = params.get("kind") || "";
  setPage(loading("insights"));
  const items = await api(`/insights${kind ? `?kind=${kind}` : ""}`);
  const order = ["contradiction", "bridge", "forgotten", "fragmented", "duplicate", "theme"];
  items.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind) || b.score - a.score);
  const seg = h("div", { class: "seg" }, [["", "All"], ...order.map((k) => [k, INSIGHT_LABEL[k][0]])].map(([k, l]) =>
    h("button", { "aria-pressed": String(k === kind), onclick: () => (location.hash = `#/insights${k ? `?kind=${k}` : ""}`) }, l)));
  const page = h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Insights"),
      h("p", {}, "Connections and conflicts ENGRAM noticed across your memories. They are suggestions with reasons — nothing is merged, rewritten or deleted.")),
      h("button", { class: "btn", onclick: runConsolidation }, "Run consolidation")),
    h("div", { style: "overflow-x:auto;margin-bottom:10px" }, seg));
  if (!items.length) page.append(h("div", { class: "empty" }, h("h2", {}, "Nothing to review"),
    h("p", {}, "Insights appear when several documents touch the same topics. Import more notes, then run consolidation.")));
  for (const it of items) {
    page.append(h("article", { class: "ins" },
      h("div", { class: "k" }, h("b", {}, INSIGHT_LABEL[it.kind]?.[0] || it.kind), INSIGHT_LABEL[it.kind]?.[1]),
      h("div", {}, h("h3", { style: "margin-bottom:6px" }, it.title), h("p", { class: "expl" }, it.explanation),
        h("div", { class: "members" }, it.engrams.slice(0, 6).map((g) => h("div", { class: "member", style: `--kc:${kc(g.kind)}`, tabindex: 0, role: "link",
          onclick: () => (location.hash = `#/engram/${g.id}`), onkeydown: (e) => e.key === "Enter" && (location.hash = `#/engram/${g.id}`) },
          kindChip(g.kind), h("span", { class: "num faint" }, `  ${fmtDate(g.date)}  `), h("span", { class: "faint" }, g.document), h("div", {}, short(g.preview, 160)))))),
      h("div", { class: "actions" },
        h("button", { class: "btn small", onclick: () => setInsight(it.id, "accepted") }, "Useful"),
        h("button", { class: "btn small ghost", onclick: () => setInsight(it.id, "dismissed") }, "Dismiss"),
        it.engrams.length ? h("button", { class: "btn small ghost", onclick: () => (location.hash = `#/brain?focus=${it.engrams[0].id}`) }, "Show in brain") : null)));
  }
  setPage(page);
}
async function setInsight(id, status) {
  await api(`/insights/${id}`, { method: "PATCH", body: { status } });
  toast(status === "accepted" ? "Marked as useful" : "Dismissed"); refreshBadge(); route();
}

// ------------------------------------------------------------------ brain
async function viewBrain(params) {
  const root = h("div", { class: "brain" });
  setPage(root);
  brain = new BrainView(root, { api, kindLabel: KIND_LABEL, relLabel: REL_LABEL, kinds: KINDS, h, kindChip, short,
    onOpen: (id) => (location.hash = `#/engram/${id}`) });
  try { await brain.load({ focus: params.get("focus") ? Number(params.get("focus")) : null, q: params.get("q") || "" }); }
  catch (err) { root.append(h("div", { class: "hud" }, h("p", { class: "err" }, err.message))); }
}

// ------------------------------------------------------------------ router
async function refreshBadge() {
  try {
    const o = await api("/overview");
    STATUS = o.status;
    const b = document.getElementById("insight-badge");
    b.hidden = !o.stats.insights_open; b.textContent = o.stats.insights_open;
    const foot = document.getElementById("rail-foot");
    const neural = STATUS.embedding_neural;
    foot.replaceChildren(h("div", {}, h("span", { class: "dot", style: `background:${neural ? "var(--k-project)" : "var(--k-idea)"}` }),
      neural ? "Neural embeddings, local" : "Lexical vectors (no model)"),
      h("div", {}, h("span", { class: "dot", style: `background:${STATUS.privacy.external_services_enabled ? "var(--k-decision)" : "var(--k-project)"}` }),
        STATUS.privacy.external_services_enabled ? "External LLM allowed" : "Nothing leaves this computer"));
  } catch {}
}
async function route() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/");
  const name = parts[0] || "overview";
  const params = new URLSearchParams(qs || "");
  document.querySelectorAll(".rail a").forEach((a) =>
    a.dataset.route === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  document.title = `${{ overview: "Overview", brain: "Brain explorer", search: "Search", timeline: "Timeline", engram: "Inspector", insights: "Insights" }[name] || "ENGRAM"} — ENGRAM`;
  try {
    if (name === "brain") await viewBrain(params);
    else if (name === "search") await viewSearch(params);
    else if (name === "timeline") await viewTimeline(params);
    else if (name === "engram") await viewEngram(parts[1] ? Number(parts[1]) : null);
    else if (name === "insights") await viewInsights(params);
    else await viewOverview();
  } catch (err) { setPage(errorView(err)); }
}
window.addEventListener("hashchange", route);
refreshBadge().then(route);
