(() => {
  "use strict";
  const cfg = window.VIEWER_CONFIG || {};
  const q = (s) => document.querySelector(s);
  const sig = cfg.sig ? `?s=${encodeURIComponent(cfg.sig)}` : "";
  const COLOURS = { extracted_verified: "#1a7f37", extracted_unverified: "#b26a00", computed: "#0b5cad", quoted: "#6f42c1", judgement: "#6e7781" };
  const ICONS = { extracted_verified: "✔", extracted_unverified: "⚠", computed: "∑", quoted: "❝", judgement: "◌" };
  const LABELS = { extracted_verified: "Verified", extracted_unverified: "Unverified - check the source", computed: "Computed", quoted: "Quoted verbatim", judgement: "Analysis, not from the filing" };
  const STMT = { income_statement: "Income statement", balance_sheet: "Balance sheet", cash_flow: "Cash flow", equity_statement: "Changes in equity", note: "Note" };

  let doc = null, cits = [], byId = {}, page = 1, current = null, filter = "all";
  const storeKey = `ch-review:${cfg.sha}`;
  const state = JSON.parse(localStorage.getItem(storeKey) || '{"reviewed":{},"flagged":{}}');
  const save = () => localStorage.setItem(storeKey, JSON.stringify(state));

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`);
    return r.json();
  }

  function el(tag, attrs = {}, text) {
    const e = document.createElementNS(tag === "svg" || attrs.__svg ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml", tag);
    for (const [k, v] of Object.entries(attrs)) if (k !== "__svg") e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const svg = (tag, attrs) => el(tag, Object.assign({ __svg: true }, attrs));

  function banner() {
    q("#company").textContent = doc.company_name || "Unknown company";
    q("#number").textContent = doc.company_number ? `No. ${doc.company_number}` : "";
    q("#madeup").textContent = doc.made_up_date ? `Made up to ${doc.made_up_date}` : "";
    q("#atype").textContent = doc.accounts_type ? `${doc.accounts_type} accounts` : "";
    const a = q("#chlink");
    if (doc.source_url) a.href = doc.source_url; else { a.textContent = "No Companies House link (local file)"; a.removeAttribute("href"); }
    q("#sha").textContent = `sha256 ${doc.doc_id.slice(0, 12)}…`;
    document.title = `${doc.company_name || "Filing"} – viewer`;
    q("#legend").innerHTML = Object.keys(LABELS).filter((k) => k !== "judgement").map((k) => `<span style="color:${COLOURS[k]}">${ICONS[k]} ${LABELS[k].split(" - ")[0]}</span>`).join("");
  }

  function visible() {
    return cits.filter((c) => {
      if (c.type === "computed") return false;
      if (filter === "unverified") return c.status === "extracted_unverified";
      if (filter === "flagged") return state.flagged[c.citation_id];
      if (filter === "page") return c.pdf_page === page;
      return true;
    });
  }

  function renderList() {
    const ul = q("#items");
    ul.innerHTML = "";
    let lastGroup = null;
    for (const c of visible()) {
      const g = `${STMT[c.statement] || c.section || c.type} ${c.scope && c.scope !== "unknown" ? "(" + c.scope + ")" : ""} · p.${c.printed_page || "–"} (PDF ${c.pdf_page})`;
      if (g !== lastGroup) { ul.appendChild(el("li", { class: "group" }, g)); lastGroup = g; }
      const li = el("li", { "data-id": c.citation_id });
      li.className = [current && current.citation_id === c.citation_id ? "active" : "", state.reviewed[c.citation_id] ? "reviewed" : "", state.flagged[c.citation_id] ? "flagged" : ""].join(" ");
      li.appendChild(el("span", { class: `ico ${c.status}`, title: LABELS[c.status] }, ICONS[c.status] || "?"));
      const name = c.label || (c.type === "figure" ? "(unlabelled subtotal)" : (c.verbatim || "").slice(0, 60));
      li.appendChild(el("span", { class: "lbl", title: c.label || c.verbatim }, name));
      li.appendChild(el("span", { class: "val" }, c.raw || ""));
      if (c.column) li.appendChild(el("span", { class: "sub" }, `${c.column}${c.unit ? " · " + c.unit : ""}`));
      li.onclick = () => select(c.citation_id);
      ul.appendChild(li);
    }
    const all = cits.filter((c) => c.type !== "computed");
    const done = all.filter((c) => state.reviewed[c.citation_id]).length;
    q("#bar").style.width = all.length ? `${(100 * done) / all.length}%` : "0";
    q("#progress-text").textContent = `${done} / ${all.length} reviewed`;
    const act = ul.querySelector("li.active");
    if (act) act.scrollIntoView({ block: "nearest" });
  }

  function showPage(p) {
    page = Math.max(1, Math.min(doc.page_count, p));
    const [w, h] = doc.page_sizes[page - 1];
    const stage = q("#stage");
    const maxW = Math.min(q("#doc").clientWidth - 40, 1000);
    stage.style.width = `${maxW}px`;
    q("#overlay").setAttribute("viewBox", `0 0 ${w} ${h}`);
    q("#overlay").setAttribute("preserveAspectRatio", "none");
    const img = q("#pageimg");
    const src = `/tile/${cfg.sha}/${page}.png${sig}`;
    if (img.getAttribute("src") !== src) img.src = src;
    const info = doc.pages[page - 1] || {};
    q("#pageinfo").textContent = `PDF page ${page} of ${doc.page_count}` + (info.printed_page ? ` · printed page ${info.printed_page}` : "") + (info.kind ? ` · ${info.kind.replace(/_/g, " ")}${info.scope && info.scope !== "unknown" ? " (" + info.scope + ")" : ""}` : "");
    draw();
    if (filter === "page") renderList();
  }

  function box(b, attrs) {
    const pad = 1.2;
    return svg("rect", Object.assign({ x: b[0] - pad, y: b[1] - pad, width: b[2] - b[0] + 2 * pad, height: b[3] - b[1] + 2 * pad, rx: 1.5 }, attrs));
  }

  function draw() {
    const o = q("#overlay");
    o.innerHTML = "";
    if (!current || current.pdf_page !== page) return;
    const col = COLOURS[current.status] || "#d68c00";
    const spans = current.spans || [];
    const value = spans.find((s) => s.role === "value" || s.role === "quote");
    if (q("#trail").checked) {
      for (const ch of current.checks || []) {
        for (const id of ch.citation_ids || []) {
          const r = byId[id];
          if (!r || r.pdf_page !== page) continue;
          const v = (r.spans || []).find((s) => s.role === "value");
          if (!v) continue;
          o.appendChild(box(v.bbox, { fill: "rgba(11,92,173,.16)", stroke: "#0b5cad", "stroke-width": 0.8, "stroke-dasharray": "2 1.5" }));
          if (value) {
            o.appendChild(svg("line", { x1: (value.bbox[0] + value.bbox[2]) / 2, y1: (value.bbox[1] + value.bbox[3]) / 2, x2: (v.bbox[0] + v.bbox[2]) / 2, y2: (v.bbox[1] + v.bbox[3]) / 2, stroke: "#0b5cad", "stroke-width": 0.5, opacity: 0.6 }));
          }
        }
      }
    }
    for (const s of spans) {
      if (s.role === "value" || s.role === "quote") continue;
      o.appendChild(box(s.bbox, { fill: col, "fill-opacity": 0.13, stroke: col, "stroke-opacity": 0.45, "stroke-width": 0.5 }));
    }
    if (value) {
      const vb = value.bbox;
      for (const s of spans) {
        if (s === value) continue;
        const b = s.bbox;
        let line = null;
        if (s.role === "label" || s.role === "note") {
          if (Math.abs((b[1] + b[3]) / 2 - (vb[1] + vb[3]) / 2) < 14) line = { x1: b[2] + 1, y1: (b[1] + b[3]) / 2, x2: vb[0] - 1, y2: (vb[1] + vb[3]) / 2 };
        } else if (s.role === "header" || s.role === "unit") {
          line = { x1: (b[0] + b[2]) / 2, y1: b[3] + 1, x2: (vb[0] + vb[2]) / 2, y2: vb[1] - 1 };
        }
        if (line) o.appendChild(svg("line", Object.assign(line, { stroke: col, "stroke-width": 0.45, "stroke-dasharray": "1.5 1.5", opacity: 0.8 })));
      }
      for (const s of spans.filter((s) => s.role === "value" || s.role === "quote")) {
        o.appendChild(box(s.bbox, { fill: col, "fill-opacity": 0.32, stroke: col, "stroke-width": 1.1 }));
      }
      const badge = svg("text", { x: vb[2] + 3, y: vb[1] + 6.5, "font-size": 7, fill: col, "font-weight": 700 });
      badge.textContent = ICONS[current.status] || "";
      o.appendChild(badge);
    }
    const target = value || spans[0];
    if (target) {
      const [w, h] = doc.page_sizes[page - 1];
      const stage = q("#stage");
      const y = (target.bbox[1] / h) * stage.clientHeight;
      const docEl = q("#doc");
      const top = stage.offsetTop + y - docEl.clientHeight / 2;
      docEl.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    }
  }

  function select(id) {
    const c = byId[id];
    if (!c) return;
    current = c;
    const u = new URL(location.href);
    u.searchParams.set("c", id);
    u.searchParams.delete("p");
    history.replaceState(null, "", u);
    showPage(c.pdf_page);
    renderDetail();
    renderList();
  }

  function renderDetail() {
    const c = current;
    q("#detail-empty").hidden = !!c;
    q("#detail-body").hidden = !c;
    if (!c) return;
    const p = q("#d-prov");
    p.className = `prov ${c.status}`;
    p.textContent = `${ICONS[c.status] || ""} ${LABELS[c.status] || c.status}`;
    q("#d-label").textContent = c.label || (c.type === "quote" ? "Quoted passage" : "(unlabelled subtotal)");
    q("#d-raw").textContent = c.raw || "";
    q("#d-unit").textContent = c.unit || "";
    q("#d-col").textContent = c.column_header || c.column || "–";
    q("#d-scope").textContent = [c.scope, c.period, c.year, c.restated ? "restated" : ""].filter(Boolean).join(" · ") || "–";
    q("#d-page").textContent = `printed page ${c.printed_page || "–"} · PDF page ${c.pdf_page}`;
    q("#d-verbatim").textContent = c.verbatim || "";
    q("#d-vstatus").textContent = c.verification_status || c.status;
    q("#d-conf").textContent = c.ocr_confidence != null ? `${c.ocr_confidence}${c.ocr_low_confidence_substitution ? " (OCR character substitution applied)" : ""}` : "–";
    q("#d-cid").textContent = c.citation_id;
    const ul = q("#d-checks");
    ul.innerHTML = "";
    if (!(c.checks || []).length) ul.appendChild(el("li", {}, c.type === "quote" ? "Verbatim text: no arithmetic check applies." : "No check passed. Treat this figure as uncertain and confirm it on the page."));
    for (const ch of c.checks || []) {
      const li = el("li", { class: ch.passed === false ? "fail" : "" });
      li.appendChild(document.createTextNode(`${ch.passed === false ? "✖ " : ""}${ch.rule.replace(/_/g, " ")}: ${ch.detail} `));
      for (const id of ch.citation_ids || []) {
        if (!byId[id]) continue;
        const a = el("a", {}, `[${byId[id].raw || id}]`);
        a.onclick = () => select(id);
        li.appendChild(a);
        li.appendChild(document.createTextNode(" "));
      }
      ul.appendChild(li);
    }
    q("#d-crop").src = `/crop/${cfg.sha}/${c.citation_id}.png${sig}`;
  }

  function step(d) {
    const v = visible();
    if (!v.length) return;
    let i = current ? v.findIndex((c) => c.citation_id === current.citation_id) : -1;
    i = (i + d + v.length) % v.length;
    select(v[i].citation_id);
  }

  function toggle(kind) {
    if (!current) return;
    const id = current.citation_id;
    if (state[kind][id]) delete state[kind][id]; else state[kind][id] = new Date().toISOString();
    save();
    renderList();
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" && e.target.type === "text") return;
    if (e.key === "n") step(1);
    else if (e.key === "p") step(-1);
    else if (e.key === " ") { e.preventDefault(); toggle("reviewed"); if (filter !== "flagged") step(1); }
    else if (e.key === "f") toggle("flagged");
    else if (e.key === "t") { q("#trail").checked = !q("#trail").checked; draw(); }
    else if (e.key === "ArrowRight") showPage(page + 1);
    else if (e.key === "ArrowLeft") showPage(page - 1);
  });
  q("#prev").onclick = () => showPage(page - 1);
  q("#next").onclick = () => showPage(page + 1);
  q("#trail").onchange = draw;
  q("#btn-review").onclick = () => toggle("reviewed");
  q("#btn-flag").onclick = () => toggle("flagged");
  for (const r of document.querySelectorAll('input[name="flt"]')) r.onchange = () => { filter = r.value; renderList(); };
  window.addEventListener("resize", () => showPage(page));
  q("#pageimg").addEventListener("load", draw);

  (async () => {
    try {
      doc = await getJSON(`/api/doc/${cfg.sha}${sig}`);
      banner();
      const data = await getJSON(`/api/citations/${cfg.sha}${sig}`);
      cits = data.citations.filter((c) => (c.spans || []).length).sort((a, b) => (a.pdf_page - b.pdf_page) || ((a.row_bbox || a.spans[0].bbox)[1] - (b.row_bbox || b.spans[0].bbox)[1]) || ((a.spans.find((s) => s.role === "value") || a.spans[0]).bbox[0] - (b.spans.find((s) => s.role === "value") || b.spans[0]).bbox[0]));
      for (const c of data.citations) byId[c.citation_id] = c;
      if (cfg.trail) q("#trail").checked = true;
      if (cfg.citation && byId[cfg.citation]) select(cfg.citation);
      else { showPage(parseInt(cfg.page || "1", 10) || 1); renderList(); }
    } catch (err) {
      q("#company").textContent = `Could not load the filing: ${err.message}`;
    }
  })();
})();
