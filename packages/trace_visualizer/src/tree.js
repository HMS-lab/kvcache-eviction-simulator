export function heatColor(count, max, logarithmic = true) {
  const t = max > 1 ? (logarithmic ? Math.log1p(count) / Math.log1p(max) : count / max) : 0;
  const a = t < .6 ? [216, 234, 245] : [116, 196, 181];
  const b = t < .6 ? [116, 196, 181] : [244, 188, 78];
  const u = t < .6 ? t / .6 : (t - .6) / .4;
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * u)).join(',')})`;
}

export class TreeView {
  constructor(canvas, onSelect) {
    this.canvas = canvas; this.ctx = canvas.getContext('2d'); this.onSelect = onSelect;
    this.scale = 1; this.tx = 0; this.ty = 0; this.positions = []; this.hitboxes = [];
    this.observer = new ResizeObserver(() => { this.resize(); if (this.autoFit) this.fit(); });
    this.autoFit = true; this.observer.observe(canvas.parentElement);
    canvas.addEventListener('wheel', event => {
      event.preventDefault(); this.autoFit = false;
      const rect = canvas.getBoundingClientRect();
      this.zoom(Math.exp(-event.deltaY * .0015), event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    canvas.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      this.drag = { x: event.clientX, y: event.clientY, tx: this.tx, ty: this.ty, moved: false };
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', event => {
      if (!this.drag) return;
      const dx = event.clientX - this.drag.x, dy = event.clientY - this.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) this.drag.moved = true;
      if (this.drag.moved) { this.autoFit = false; this.tx = this.drag.tx + dx; this.ty = this.drag.ty + dy; this.draw(); }
    });
    canvas.addEventListener('pointerup', event => {
      if (this.drag && !this.drag.moved) {
        const rect = canvas.getBoundingClientRect();
        const x = (event.clientX - rect.left - this.tx) / this.scale, y = (event.clientY - rect.top - this.ty) / this.scale;
        const hit = this.hitboxes.find(b => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
        if (hit) this.onSelect(hit.id);
      }
      this.drag = null;
    });
    canvas.addEventListener('pointercancel', () => { this.drag = null; });
    canvas.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '-', '0'].includes(event.key)) event.preventDefault();
      if (event.key === '0') this.fit();
      else if (event.key === '+') this.zoom(1.2);
      else if (event.key === '-') this.zoom(1 / 1.2);
      else if (event.key.startsWith('Arrow')) {
        this.autoFit = false;
        this.tx += event.key === 'ArrowLeft' ? 40 : event.key === 'ArrowRight' ? -40 : 0;
        this.ty += event.key === 'ArrowUp' ? 40 : event.key === 'ArrowDown' ? -40 : 0;
        this.draw();
      }
    });
  }
  resize() {
    const rect = this.canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    this.width = rect.width; this.height = rect.height;
    this.canvas.width = Math.round(rect.width * dpr); this.canvas.height = Math.round(rect.height * dpr);
    this.draw();
  }
  layout(data, { model = '*', focus = null, levels = 5, layout = 'compact', priorityCounts = null, rootCounts = null } = {}) {
    this.data = data; this.positions = []; this.positionMap = new Map();
    const roots = focus === null ? data.roots.filter(id => (model === '*' || data.nodes[id].model === model) && (!rootCounts || rootCounts[id])) : [focus];
    // Spend a viewport-sized leaf budget on the most-used branches. Hidden
    // branches remain selectable in the inspector; no counts are discarded.
    const dense = levels >= 100000;
    let extraLeaves = Math.max(0, Math.floor((this.width || 900) / 190) - roots.length);
    const candidates = roots.map(id => ({ id, tier: 0, children: [], hidden: false, omitted: 0, x: 0, y: 0 }));
    for (const p of candidates) this.positionMap.set(p.id, p);
    const pending = candidates.slice();
    const priority = id => priorityCounts ? priorityCounts[id] : data.nodes[id].visits.length;
    while (pending.length) {
      pending.sort((a, b) => priority(a.id) - priority(b.id));
      const p = pending.pop(), node = data.nodes[p.id];
      const children = p.tier < levels ? node.children : [];
      const take = dense ? children.length : Math.min(children.length, extraLeaves + 1);
      const chosen = new Set(children.slice().sort((a, b) => priority(b) - priority(a)).slice(0, take));
      p.children = children.filter(id => chosen.has(id));
      p.omitted = node.children.length - p.children.length;
      p.hidden = p.omitted > 0;
      extraLeaves -= Math.max(0, p.children.length - 1);
      for (const id of p.children) {
        const child = { id, tier: p.tier + 1, children: [], hidden: false, omitted: 0, x: 0, y: 0 };
        this.positionMap.set(id, child); pending.push(child);
      }
    }
    // Explicit stack avoids recursion limits in sessions with thousands of turns.
    let leaf = 0, maxY = 0;
    for (const root of roots) {
      const stack = [[root, false]];
      while (stack.length) {
        const [id, done] = stack.pop(), p = this.positionMap.get(id), children = p.children;
        if (!done) {
          this.positions.push(p);
          stack.push([id, true]);
          for (let i = children.length - 1; i >= 0; i--) stack.push([children[i], false]);
        } else {
          p.x = children.length ? (this.positionMap.get(children[0]).x + this.positionMap.get(children.at(-1)).x) / 2 : 110 + leaf++ * 210;
        }
      }
      leaf += .5;
    }
    for (const p of this.positions) {
      const n = data.nodes[p.id], parent = this.positionMap.get(n.parent);
      p.y = layout === 'tokens' ? Math.max((n.depth - data.nodes[focus ?? data.roots.find(id => data.nodes[id].model === n.model && data.nodes[id].session === n.session)].depth) * .025 + 45, parent ? parent.y + 78 : 45) : 45 + p.tier * 105;
      maxY = Math.max(maxY, p.y);
    }
    this.worldWidth = Math.max(300, (leaf - .5) * 210 + 15); this.worldHeight = maxY + 90;
    this.autoFit = true; this.fit();
  }
  update(state) { Object.assign(this, state); this.draw(); }
  fit() {
    if (!this.width || !this.height) return;
    this.scale = Math.min((this.width - 40) / (this.worldWidth || 400), (this.height - 40) / (this.worldHeight || 400), 1.25);
    this.tx = (this.width - (this.worldWidth || 400) * this.scale) / 2;
    this.ty = Math.max(15, (this.height - (this.worldHeight || 400) * this.scale) / 2);
    this.draw();
  }
  zoom(factor, x = this.width / 2, y = this.height / 2) {
    const old = this.scale;
    this.scale = Math.max(.005, Math.min(5, old * factor));
    this.tx = x - (x - this.tx) * this.scale / old; this.ty = y - (y - this.ty) * this.scale / old;
    this.autoFit = false; this.draw();
  }
  draw() {
    const c = this.ctx, dpr = devicePixelRatio || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, this.width || 0, this.height || 0);
    this.hitboxes = [];
    if (!this.data || !this.counts) return;
    const highlighted = new Set();
    if (this.agentCounts) for (const p of this.positions) if (this.agentCounts[p.id]) highlighted.add(p.id);
    let cursor = this.agentCounts ? null : this.selected;
    while (cursor !== null && cursor !== undefined && cursor >= 0) { highlighted.add(cursor); cursor = this.data.nodes[cursor].parent; }
    if (!this.agentCounts && this.request !== null && this.request !== undefined) {
      for (const p of this.positions) {
        const list = this.data.nodes[p.id].visits;
        let lo = 0, hi = list.length;
        while (lo < hi) { const mid = (lo + hi) >>> 1; if (list[mid] < this.request) lo = mid + 1; else hi = mid; }
        if (list[lo] === this.request) highlighted.add(p.id);
      }
    }
    let max = 1;
    for (const count of this.counts) max = Math.max(max, count);
    c.translate(this.tx, this.ty); c.scale(this.scale, this.scale);
    // Edges are drawn first so node labels stay legible.
    for (const p of this.positions) {
      const n = this.data.nodes[p.id], parent = this.positionMap.get(n.parent);
      if (!parent || !this.counts[p.id]) continue;
      if (Math.max(parent.y, p.y) * this.scale + this.ty < -50 || Math.min(parent.y, p.y) * this.scale + this.ty > this.height + 50) continue;
      const active = highlighted.has(p.id);
      c.globalAlpha = this.agentCounts && !active ? .2 : 1;
      c.strokeStyle = active ? '#0a9c8e' : '#b4c6bd'; c.lineWidth = active ? 2.8 : 1.2;
      c.beginPath(); c.moveTo(parent.x, parent.y + (parent.hidden ? 52 : 36)); c.lineTo(parent.x, (parent.y + p.y) / 2 + 26); c.lineTo(p.x, (parent.y + p.y) / 2 + 26); c.lineTo(p.x, p.y); c.stroke();
    }
    for (const p of this.positions) {
      const node = this.data.nodes[p.id], count = this.counts[p.id];
      if (!count) continue;
      const root = node.parent < 0, w = root ? 210 : 190, h = p.hidden ? 52 : 36, x = p.x - w / 2, y = p.y;
      if ((x + w) * this.scale + this.tx < 0 || x * this.scale + this.tx > this.width || (y + h) * this.scale + this.ty < 0 || y * this.scale + this.ty > this.height) continue;
      c.fillStyle = root ? '#edf3ef' : heatColor(count, max, this.logarithmic);
      c.globalAlpha = this.agentCounts && !highlighted.has(p.id) ? .25 : 1;
      c.strokeStyle = this.selected === p.id ? '#087f76' : highlighted.has(p.id) ? '#17a693' : root ? '#b4c5bb' : '#7cafa3';
      c.lineWidth = this.selected === p.id ? 3 : this.agentCounts && highlighted.has(p.id) ? 2.5 : 1;
      c.beginPath(); c.roundRect(x, y, w, h, 8); c.fill(); c.stroke();
      c.fillStyle = '#173d33'; c.font = `${root ? '600' : '500'} 11px system-ui`; c.textAlign = 'center';
      const label = root ? `${node.session === undefined ? '' : `S${node.session + 1} · `}${node.model}` : `${node.blocks.toLocaleString()} blocks · ${count.toLocaleString()} requests`;
      c.fillText(label.length > 31 ? label.slice(0, 29) + '…' : label, p.x, y + 22);
      if (p.hidden) { c.font = '10px system-ui'; c.fillStyle = '#346c5e'; c.fillText(`＋ ${p.omitted} other branches · select`, p.x, y + 41); }
      if (this.endpoints?.[p.id]) {
        c.fillStyle = '#fff'; c.beginPath(); c.arc(x + w - 7, y + h - 2, 4, 0, Math.PI * 2); c.fill(); c.stroke();
      }
      this.hitboxes.push({ id: p.id, x, y, w, h });
    }
    c.globalAlpha = 1;
  }
}
