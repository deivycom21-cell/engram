// Brain explorer: the real knowledge graph (engrams = nodes, typed relations = edges) in 3D.
// Layout is a force simulation computed here; positions carry no meaning beyond proximity
// (connected memories and fragments of the same document end up close together).
import * as THREE from "./vendor/three.module.min.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const KIND_HEX = { fact: 0x9aafc4, idea: 0xf5c451, event: 0x5fc4b8, decision: 0xf0875f, concept: 0xa98bf0,
  observation: 0x7db7f0, question: 0xe87ab8, project: 0x9bd16b };
const EDGE_STYLE = {
  structural: { color: 0x3a4a5e, opacity: 0.55, dashed: false, label: "Same document, consecutive", css: "var(--r-structural)" },
  explicit: { color: 0xe6eef5, opacity: 0.9, dashed: false, label: "Explicit link in source", css: "var(--r-explicit)" },
  inferred: { color: 0x7a8fb0, opacity: 0.75, dashed: true, label: "Inferred: similar / shared concepts", css: "var(--r-inferred)" },
  evolution: { color: 0x7db7f0, opacity: 0.9, dashed: true, label: "Inferred: revisited later", css: "var(--r-evolution)" },
  contradiction: { color: 0xff6161, opacity: 0.95, dashed: true, label: "Inferred: possible contradiction", css: "var(--r-contradiction)" },
};
const edgeClass = (e) => e.origin === "structural" ? "structural" : e.origin === "explicit" ? "explicit"
  : e.type === "possible_contradiction" ? "contradiction" : e.type === "later_version" ? "evolution" : "inferred";
const normTxt = (s) => (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export class BrainView {
  constructor(root, deps) {
    this.root = root; this.d = deps;
    this.kindsOn = new Set(deps.kinds);
    this.showStructural = true; this.showInferred = true;
    this.highlight = null; this.selected = null; this.hover = null;
    this.raf = 0; this.disposed = false;
  }

  async load({ focus = null, q = "" } = {}) {
    const { h } = this.d;
    const params = new URLSearchParams({ limit: 700, structural: "true" });
    if (q) params.set("q", q);
    const data = await this.d.api(`/graph?${params}`);
    this.data = data;
    this.buildDom(q);
    if (!data.nodes.length) {
      this.hud.append(h("p", { class: "muted", style: "margin-top:12px" }, q ? "No memories match this search." : "The graph is empty. Import documents from the Overview."));
      return;
    }
    this.initThree();
    this.initGraph();
    this.loop();
    if (focus && this.index.has(focus)) setTimeout(() => this.select(focus, true), reducedMotion ? 0 : 900);
  }

  // ---------------------------------------------------------------- DOM overlay
  buildDom(q) {
    const { h, kindLabel, kinds } = this.d;
    const n = this.data.nodes.length, m = this.data.edges.filter((e) => e.origin !== "structural").length;
    this.searchInput = h("input", { class: "input", type: "search", placeholder: "Find a concept or word", value: q || "", "aria-label": "Find in graph" });
    this.searchInfo = h("div", { class: "faint", style: "font-size:12.5px;margin-top:6px" });
    this.searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") this.find(this.searchInput.value); });
    const kf = h("div", { class: "kfilter" }, kinds.map((k) => {
      const inp = h("input", { type: "checkbox", checked: true });
      inp.addEventListener("change", () => { inp.checked ? this.kindsOn.add(k) : this.kindsOn.delete(k); this.applyVisibility(); });
      return h("label", { style: `--kc:var(--k-${k})` }, inp, kindLabel[k]);
    }));
    const toggle = (label, prop) => {
      const inp = h("input", { type: "checkbox", checked: true });
      inp.addEventListener("change", () => { this[prop] = inp.checked; this.applyVisibility(); });
      return h("label", { class: "check" }, inp, label);
    };
    this.hud = h("div", { class: "hud" },
      h("h2", {}, "Brain explorer"),
      h("div", { class: "sub" }, `${n} engrams · ${m} relations between memories. Drag to orbit, scroll to zoom, click a memory.`),
      this.searchInput, this.searchInfo,
      h("div", { class: "row" }, kf),
      h("div", { class: "row" }, toggle("Document structure edges", "showStructural"), toggle("Inferred relations", "showInferred")),
      h("div", { class: "edge-legend" }, Object.values(EDGE_STYLE).map((s) =>
        h("span", {}, h("i", { class: s.dashed ? "dashed" : "", style: `--rc:${s.css}` }), s.label))),
      h("div", { class: "row", style: "display:flex;gap:6px;flex-wrap:wrap" },
        h("button", { class: "btn small", onclick: () => this.resetView() }, "Reset view"),
        q ? h("button", { class: "btn small ghost", onclick: () => (location.hash = "#/brain") }, "Show whole memory") : null));
    this.panel = h("aside", { class: "panel", hidden: true, "aria-live": "polite" });
    this.tip = h("div", { class: "tip", hidden: true });
    this.stats = h("div", { class: "stats" }, "Positions come from a force layout: closeness means connection, not a coordinate system.");
    this.root.append(this.hud, this.panel, this.tip, this.stats);
  }

  // ---------------------------------------------------------------- three.js
  initThree() {
    const w = this.root.clientWidth, hgt = this.root.clientHeight;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, hgt);
    this.root.prepend(this.renderer.domElement);
    this.renderer.domElement.setAttribute("aria-label", "3D graph of memories");
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x0f141b, 0.0016);
    this.camera = new THREE.PerspectiveCamera(55, w / hgt, 1, 5000);
    this.camera.position.set(0, 60, 420);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.08;
    this.controls.autoRotate = !reducedMotion; this.controls.autoRotateSpeed = 0.35;
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const dl = new THREE.DirectionalLight(0xffffff, 1.1); dl.position.set(200, 300, 400); this.scene.add(dl);
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    const el = this.renderer.domElement;
    this.onMove = (e) => { const r = el.getBoundingClientRect(); this.pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); this.pointerPx = [e.clientX - r.left, e.clientY - r.top]; this.pick(); };
    this.onDown = (e) => { this.downAt = [e.clientX, e.clientY]; this.controls.autoRotate = false; };
    this.onUp = (e) => { if (this.downAt && Math.hypot(e.clientX - this.downAt[0], e.clientY - this.downAt[1]) < 5) { this.pick(); if (this.hover !== null) this.select(this.hover); } };
    el.addEventListener("pointermove", this.onMove);
    el.addEventListener("pointerdown", this.onDown);
    el.addEventListener("pointerup", this.onUp);
    this.ro = new ResizeObserver(() => {
      const W = this.root.clientWidth, H = this.root.clientHeight;
      if (!W || !H) return;
      this.camera.aspect = W / H; this.camera.updateProjectionMatrix(); this.renderer.setSize(W, H);
    });
    this.ro.observe(this.root);
  }

  initGraph() {
    const nodes = this.data.nodes;
    const N = nodes.length;
    this.index = new Map(nodes.map((n, i) => [n.id, i]));
    this.edges = this.data.edges.filter((e) => this.index.has(e.source) && this.index.has(e.target));
    this.adj = nodes.map(() => []);
    this.edges.forEach((e, k) => { this.adj[this.index.get(e.source)].push(k); this.adj[this.index.get(e.target)].push(k); });
    // initial positions: each document gets its own region of the sphere
    const docs = [...new Set(nodes.map((n) => n.document_id))];
    const anchor = new Map(docs.map((d, i) => {
      const phi = Math.acos(1 - (2 * (i + 0.5)) / docs.length), th = Math.PI * (1 + Math.sqrt(5)) * i;
      return [d, [Math.sin(phi) * Math.cos(th), Math.sin(phi) * Math.sin(th), Math.cos(phi)]];
    }));
    this.docAnchor = anchor;
    this.pos = new Float32Array(N * 3);
    this.vel = new Float32Array(N * 3);
    const R = 60 + Math.sqrt(N) * 14;
    nodes.forEach((n, i) => {
      const a = anchor.get(n.document_id);
      for (let c = 0; c < 3; c++) this.pos[i * 3 + c] = a[c] * R * 0.8 + (Math.random() - 0.5) * 40;
    });
    this.radius = nodes.map((n) => 3.2 + Math.min(6, Math.sqrt(n.degree) * 1.6));

    const geo = new THREE.SphereGeometry(1, 18, 14);
    const mat = new THREE.MeshLambertMaterial({ vertexColors: false });
    this.mesh = new THREE.InstancedMesh(geo, mat, N);
    this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.baseColor = nodes.map((n) => new THREE.Color(KIND_HEX[n.kind] || 0x9aafc4));
    nodes.forEach((_, i) => this.mesh.setColorAt(i, this.baseColor[i]));
    this.scene.add(this.mesh);

    const ringGeo = new THREE.SphereGeometry(1, 12, 8);
    this.marker = new THREE.Mesh(ringGeo, new THREE.MeshBasicMaterial({ color: 0xf5c451, wireframe: true, transparent: true, opacity: 0.3 }));
    this.marker.visible = false;
    this.scene.add(this.marker);

    this.edgeObjs = {};
    for (const [cls, st] of Object.entries(EDGE_STYLE)) {
      const g = new THREE.BufferGeometry();
      const m = st.dashed
        ? new THREE.LineDashedMaterial({ color: st.color, transparent: true, opacity: st.opacity, dashSize: 4, gapSize: 3 })
        : new THREE.LineBasicMaterial({ color: st.color, transparent: true, opacity: st.opacity });
      const obj = new THREE.LineSegments(g, m);
      obj.userData.cls = cls;
      this.edgeObjs[cls] = obj;
      this.scene.add(obj);
    }
    this.focusLines = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xf5c451, transparent: true, opacity: 0.9 }));
    this.scene.add(this.focusLines);
    this.visible = new Array(N).fill(true);
    this.applyVisibility();
    this.iter = 0;
    this.maxIter = 320;
    if (reducedMotion) { while (this.iter < this.maxIter) this.step(); }
    this.updateGeometry();
  }

  // ---------------------------------------------------------------- force layout
  step() {
    const N = this.data.nodes.length, p = this.pos, v = this.vel;
    const t = 1 - this.iter / this.maxIter;
    const k = 38;
    const f = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      if (!this.visible[i]) continue;
      for (let j = i + 1; j < N; j++) {
        if (!this.visible[j]) continue;
        let dx = p[i * 3] - p[j * 3], dy = p[i * 3 + 1] - p[j * 3 + 1], dz = p[i * 3 + 2] - p[j * 3 + 2];
        let d2 = dx * dx + dy * dy + dz * dz + 0.01;
        if (d2 > 90000) continue;
        const rep = (k * k) / d2;
        f[i * 3] += dx * rep; f[i * 3 + 1] += dy * rep; f[i * 3 + 2] += dz * rep;
        f[j * 3] -= dx * rep; f[j * 3 + 1] -= dy * rep; f[j * 3 + 2] -= dz * rep;
      }
    }
    for (const e of this.edges) {
      const a = this.index.get(e.source), b = this.index.get(e.target);
      if (!this.visible[a] || !this.visible[b]) continue;
      const cls = edgeClass(e);
      const w = cls === "structural" ? 0.9 : cls === "explicit" ? 0.7 : 0.45 * (e.confidence || 0.5);
      const dx = p[b * 3] - p[a * 3], dy = p[b * 3 + 1] - p[a * 3 + 1], dz = p[b * 3 + 2] - p[a * 3 + 2];
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.01;
      const s = (w * (d - k)) / d;
      f[a * 3] += dx * s; f[a * 3 + 1] += dy * s; f[a * 3 + 2] += dz * s;
      f[b * 3] -= dx * s; f[b * 3 + 1] -= dy * s; f[b * 3 + 2] -= dz * s;
    }
    this.data.nodes.forEach((n, i) => {
      const a = this.docAnchor.get(n.document_id);
      const R = 60 + Math.sqrt(N) * 12;
      for (let c = 0; c < 3; c++) {
        f[i * 3 + c] += (a[c] * R - p[i * 3 + c]) * 0.012 - p[i * 3 + c] * 0.004;
        v[i * 3 + c] = (v[i * 3 + c] + f[i * 3 + c] * 0.05) * 0.82;
        const lim = 12 * t + 0.4;
        v[i * 3 + c] = Math.max(-lim, Math.min(lim, v[i * 3 + c]));
        p[i * 3 + c] += v[i * 3 + c];
      }
    });
    this.iter++;
  }

  updateGeometry() {
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), pv = new THREE.Vector3();
    this.data.nodes.forEach((n, i) => {
      const r = this.visible[i] ? this.radius[i] : 0.0001;
      s.set(r, r, r); pv.set(this.pos[i * 3], this.pos[i * 3 + 1], this.pos[i * 3 + 2]);
      m.compose(pv, q, s); this.mesh.setMatrixAt(i, m);
    });
    this.mesh.instanceMatrix.needsUpdate = true;
    this.mesh.computeBoundingSphere();
    const buckets = Object.fromEntries(Object.keys(EDGE_STYLE).map((c) => [c, []]));
    for (const e of this.edges) {
      const cls = edgeClass(e);
      if (cls === "structural" && !this.showStructural) continue;
      if (cls !== "structural" && cls !== "explicit" && !this.showInferred) continue;
      const a = this.index.get(e.source), b = this.index.get(e.target);
      if (!this.visible[a] || !this.visible[b]) continue;
      buckets[cls].push(this.pos[a * 3], this.pos[a * 3 + 1], this.pos[a * 3 + 2], this.pos[b * 3], this.pos[b * 3 + 1], this.pos[b * 3 + 2]);
    }
    for (const [cls, arr] of Object.entries(buckets)) {
      const obj = this.edgeObjs[cls];
      obj.geometry.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
      obj.geometry.computeBoundingSphere();
      if (EDGE_STYLE[cls].dashed && arr.length) obj.computeLineDistances();
    }
    this.updateFocusLines();
    if (this.selected !== null) {
      const i = this.index.get(this.selected);
      this.marker.position.set(this.pos[i * 3], this.pos[i * 3 + 1], this.pos[i * 3 + 2]);
      this.marker.scale.setScalar(this.radius[i] * 1.9);
    }
  }

  updateFocusLines() {
    const arr = [];
    if (this.selected !== null) {
      const i = this.index.get(this.selected);
      for (const k of this.adj[i]) {
        const e = this.edges[k];
        if (e.origin === "structural" && !this.showStructural) continue;
        const a = this.index.get(e.source), b = this.index.get(e.target);
        if (!this.visible[a] || !this.visible[b]) continue;
        arr.push(this.pos[a * 3], this.pos[a * 3 + 1], this.pos[a * 3 + 2], this.pos[b * 3], this.pos[b * 3 + 1], this.pos[b * 3 + 2]);
      }
    }
    this.focusLines.geometry.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
    this.focusLines.geometry.computeBoundingSphere();
  }

  applyVisibility() {
    if (!this.mesh) return;
    this.data.nodes.forEach((n, i) => (this.visible[i] = this.kindsOn.has(n.kind)));
    this.recolor();
    this.updateGeometry();
  }

  recolor() {
    const dim = new THREE.Color(0x2a3442);
    const focusSet = new Set();
    if (this.selected !== null) {
      const i = this.index.get(this.selected);
      focusSet.add(i);
      this.adj[i].forEach((k) => { focusSet.add(this.index.get(this.edges[k].source)); focusSet.add(this.index.get(this.edges[k].target)); });
    }
    this.data.nodes.forEach((n, i) => {
      let c = this.baseColor[i];
      if (this.highlight && !this.highlight.has(i)) c = dim;
      else if (!this.highlight && focusSet.size && !focusSet.has(i)) c = this.baseColor[i].clone().lerp(dim, 0.65);
      this.mesh.setColorAt(i, c);
    });
    this.mesh.instanceColor.needsUpdate = true;
  }

  // ---------------------------------------------------------------- interaction
  pick() {
    if (!this.mesh) return;
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hit = this.raycaster.intersectObject(this.mesh, false).find((x) => this.visible[x.instanceId]);
    const id = hit ? this.data.nodes[hit.instanceId].id : null;
    this.hover = id;
    this.renderer.domElement.style.cursor = id !== null ? "pointer" : "grab";
    if (id === null) { this.tip.hidden = true; return; }
    const n = this.data.nodes[hit.instanceId];
    this.tip.hidden = false;
    this.tip.textContent = `${this.d.kindLabel[n.kind]} · ${n.document || ""}${n.date ? ` · ${n.date}` : ""}\n${this.d.short(n.preview, 110)}`;
    this.tip.style.whiteSpace = "pre-wrap";
    this.tip.style.left = `${this.pointerPx[0]}px`; this.tip.style.top = `${this.pointerPx[1]}px`;
  }

  async select(id, fly = true) {
    const { h, kindChip, relLabel, short } = this.d;
    this.selected = id;
    this.highlight = null;
    this.controls.autoRotate = false;
    const i = this.index.get(id);
    this.marker.visible = true;
    this.recolor(); this.updateGeometry();
    if (fly) this.flyTo(new THREE.Vector3(this.pos[i * 3], this.pos[i * 3 + 1], this.pos[i * 3 + 2]), 260);
    this.panel.hidden = false;
    this.panel.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    const d = await this.d.api(`/engrams/${id}`);
    if (this.selected !== id) return;
    const g = d.engram;
    const rels = d.relations.filter((r) => r.status !== "rejected").sort((a, b) => (a.origin === "structural") - (b.origin === "structural"));
    this.panel.replaceChildren(
      h("div", { style: "display:flex;justify-content:space-between;gap:8px;align-items:center" }, kindChip(g.kind),
        h("button", { class: "btn small ghost", "aria-label": "Close", onclick: () => this.deselect() }, "Close")),
      h("h3", { style: "margin-top:8px" }, g.provenance.document_title),
      h("div", { class: "faint", style: "font-size:12.5px" }, `${g.occurred_at || g.created_at || "undated"}${g.provenance.section ? " · " + g.provenance.section : ""}${g.provenance.page ? " · page " + g.provenance.page : ""}`),
      h("div", { class: "body" }, g.content),
      h("button", { class: "btn small", onclick: () => this.d.onOpen(id) }, "Open in inspector"),
      h("h3", { style: "margin:16px 0 4px" }, `Relations (${rels.length})`),
      ...rels.map((r) => {
        const inGraph = this.index.has(r.other.id) && this.visible[this.index.get(r.other.id)];
        const cls = edgeClass(r);
        return h("div", { class: "rel", tabindex: 0, role: "button",
          onclick: () => inGraph ? this.select(r.other.id) : this.d.onOpen(r.other.id),
          onkeydown: (e) => e.key === "Enter" && (inGraph ? this.select(r.other.id) : this.d.onOpen(r.other.id)) },
          h("div", { class: "rtype" }, h("i", { class: "sw" + (EDGE_STYLE[cls].dashed ? " dashed" : ""), style: `--rc:${EDGE_STYLE[cls].css}` }),
            relLabel[r.type] || r.type, r.origin === "inferred" ? h("span", { class: `status-pill ${r.status}` }, r.status) : null,
            inGraph ? null : h("span", { class: "faint" }, "(not in view: opens inspector)")),
          h("div", { class: "other" }, short(r.other.preview, 120)),
          h("div", { class: "expl" }, short(r.explanation, 200)));
      }));
  }

  deselect() {
    this.selected = null; this.marker.visible = false; this.panel.hidden = true;
    this.recolor(); this.updateFocusLines();
  }

  find(text) {
    const q = normTxt(text.trim());
    if (!q) { this.highlight = null; this.searchInfo.textContent = ""; this.recolor(); return; }
    const hits = new Set();
    this.data.nodes.forEach((n, i) => {
      const hay = normTxt([n.preview, n.document, n.title, ...(n.concepts || [])].join(" "));
      if (hay.includes(q)) hits.add(i);
    });
    this.highlight = hits;
    this.recolor();
    const { h } = this.d;
    this.searchInfo.replaceChildren(hits.size ? `${hits.size} engram(s) shown in colour.` : "No loaded engram contains this. ",
      h("button", { class: "btn small ghost", onclick: () => (location.hash = `#/brain?q=${encodeURIComponent(text.trim())}`) }, "Search the whole memory"));
    if (hits.size) {
      const c = new THREE.Vector3();
      hits.forEach((i) => c.add(new THREE.Vector3(this.pos[i * 3], this.pos[i * 3 + 1], this.pos[i * 3 + 2])));
      c.divideScalar(hits.size);
      this.flyTo(c, 260);
    }
  }

  flyTo(target, dist) {
    const fromT = this.controls.target.clone(), fromP = this.camera.position.clone();
    const dir = fromP.clone().sub(fromT).normalize();
    const toP = target.clone().add(dir.multiplyScalar(dist));
    if (reducedMotion) { this.controls.target.copy(target); this.camera.position.copy(toP); return; }
    const t0 = performance.now(), dur = 700;
    this.anim = (now) => {
      const t = Math.min(1, (now - t0) / dur), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      this.controls.target.lerpVectors(fromT, target, e);
      this.camera.position.lerpVectors(fromP, toP, e);
      if (t >= 1) this.anim = null;
    };
  }

  resetView() {
    this.highlight = null; this.searchInput.value = ""; this.searchInfo.textContent = "";
    this.deselect();
    this.flyTo(new THREE.Vector3(0, 0, 0), 420);
  }

  loop() {
    const tick = (now) => {
      if (this.disposed) return;
      if (this.iter < this.maxIter) { for (let s = 0; s < 3 && this.iter < this.maxIter; s++) this.step(); this.updateGeometry(); }
      if (this.anim) this.anim(now);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    if (this.ro) this.ro.disconnect();
    if (this.renderer) {
      const el = this.renderer.domElement;
      el.removeEventListener("pointermove", this.onMove);
      el.removeEventListener("pointerdown", this.onDown);
      el.removeEventListener("pointerup", this.onUp);
      this.controls.dispose();
      this.scene.traverse((o) => { o.geometry?.dispose(); o.material?.dispose?.(); });
      this.renderer.dispose();
    }
  }
}
