/**
 * ocd-regeltekst.js — gedeelde STOP-XML weergave.  (KLASSIEK script, geen module)
 *
 * Eén zelfstandig bestand, nul afhankelijkheden. Laad het met een gewoon
 * script-tag — werkt óók bij dubbelklikken (file://), geen server/bundler nodig:
 *
 *     <script src="ocd-regeltekst.js"></script>
 *     <ocd-regeltekst id="rt"></ocd-regeltekst>
 *     <script>
 *       const el = document.getElementById('rt');
 *       el.tekst = rauweStopXml;         // property (niet als attribuut — te groot)
 *       el.begrijpelijk = "…";           // optioneel; ontgrendelt de begrijpelijk-modus
 *       el.weergave = "begrijpelijk";    // of attribuut weergave="…"
 *       el.addEventListener('intref-klik', e => nav(e.detail.eid));
 *     </script>
 *
 * De parser is 1-op-1 geport uit OCDviewer's stop-xml-parser.ts (logica identiek,
 * alleen types eruit). Enige runtime-afhankelijkheid = DOMParser (in elke browser).
 * Programmatische API staat op window.ocdRegeltekst (parseTekst, renderTekst, …).
 *
 * GEEN netwerk, GEEN API-calls: puur STOP-XML (string) → HTML, client-side.
 */
(function (global) {
  'use strict';

  // ==========================================================================
  // 1. PARSER  (STOP-XML → Block[])   — geport uit stop-xml-parser.ts, logica 1:1
  // ==========================================================================

  const blokkenCache = new Map();
  const inlineCache = new Map();
  let cachedNamenRef = null;
  let cachedNamenRegex = null;
  let cachedNaamLookup = null;
  let domParser = null;

  function buildNamenRegex(namen) {
    const gesorteerd = [...namen].filter(n => n).sort((a, b) => b.length - a.length);
    if (gesorteerd.length === 0) { cachedNamenRegex = null; cachedNaamLookup = null; return; }
    const escaped = gesorteerd.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    cachedNamenRegex = new RegExp(`\\b(?:${escaped.join('|')})\\b`, 'gi');
    cachedNaamLookup = new Map();
    for (const n of gesorteerd) {
      const k = n.toLowerCase();
      if (!cachedNaamLookup.has(k)) cachedNaamLookup.set(k, n);
    }
  }

  function ensureCacheForNamen(namen) {
    if (cachedNamenRef === namen) return;
    blokkenCache.clear();
    buildNamenRegex(namen);
    cachedNamenRef = namen;
  }

  function blokkenVanCached(tekst, namen) {
    ensureCacheForNamen(namen);
    let result = blokkenCache.get(tekst);
    if (!result) { result = parseTekst(tekst, namen); blokkenCache.set(tekst, result); }
    return result;
  }

  function resetParserCache() {
    blokkenCache.clear(); inlineCache.clear();
    cachedNamenRef = null; cachedNamenRegex = null; cachedNaamLookup = null;
  }

  function inlineFragmentCached(opschrift) {
    let result = inlineCache.get(opschrift);
    if (!result) { result = parseInlineFragment(opschrift); inlineCache.set(opschrift, result); }
    return result;
  }

  function parseInlineFragment(opschrift) {
    const trimmed = (opschrift || '').trim();
    if (!trimmed) return [];
    if (!trimmed.includes('<')) return [{ type: 'plain', text: trimmed }];
    if (!domParser) domParser = new DOMParser();
    let doc;
    try { doc = domParser.parseFromString(`<root>${trimmed}</root>`, 'text/xml'); }
    catch { return [{ type: 'plain', text: stripTags(trimmed) }]; }
    if (doc.getElementsByTagName('parsererror').length > 0) return [{ type: 'plain', text: stripTags(trimmed) }];
    const root = doc.documentElement;
    if (!root) return [{ type: 'plain', text: stripTags(trimmed) }];
    return parseInline(root, []);
  }

  /** Parse STOP-XML inhoud naar Blocks. Bij fouten/plain-text → één Al-block. */
  function parseTekst(tekst, namen) {
    namen = namen || [];
    const trimmed = (tekst || '').trim();
    if (!trimmed) return [];
    if (!trimmed.includes('<')) return [{ type: 'al', segments: splitOpTermen(trimmed, namen) }];
    if (!domParser) domParser = new DOMParser();
    let doc;
    try { doc = domParser.parseFromString(trimmed, 'text/xml'); }
    catch { return [{ type: 'al', segments: splitOpTermen(trimmed, namen) }]; }
    if (doc.getElementsByTagName('parsererror').length > 0) return [{ type: 'al', segments: splitOpTermen(stripTags(trimmed), namen) }];
    const root = doc.documentElement;
    if (!root) return [{ type: 'al', segments: splitOpTermen(trimmed, namen) }];

    const blocks = [];
    const children = root.localName === 'Inhoud' ? Array.from(root.children) : [root];
    for (const child of children) {
      const block = parseBlock(child, namen);
      if (block) blocks.push(block);
    }
    return blocks.length > 0 ? blocks : [{ type: 'al', segments: splitOpTermen(stripTags(trimmed), namen) }];
  }

  function parseBlock(el, namen) {
    switch (el.localName) {
      case 'Lijst': {
        const items = [];
        for (const li of Array.from(el.children)) { if (li.localName === 'Li') items.push(parseLi(li, namen)); }
        return { type: 'lijst', lijstType: el.getAttribute('type'), items };
      }
      case 'Begrippenlijst': {
        const items = [];
        for (const begrip of Array.from(el.children)) { if (begrip.localName === 'Begrip') items.push(parseBegrip(begrip, namen)); }
        return { type: 'definitielijst', items };
      }
      case 'table':
        return parseTabel(el, namen);
      case 'Al':
      default:
        return { type: 'al', segments: parseInline(el, namen) };
    }
  }

  function parseTabel(el, namen) {
    let titel = null;
    const hoofding = [], rijen = [];
    for (const child of Array.from(el.children)) {
      if (child.localName === 'title') { const t = (child.textContent || '').trim(); if (t) titel = t; continue; }
      if (child.localName === 'tgroup') {
        for (const tg of Array.from(child.children)) {
          if (tg.localName === 'thead') collectTabelRows(tg, namen, hoofding);
          else if (tg.localName === 'tbody') collectTabelRows(tg, namen, rijen);
        }
      }
    }
    return { type: 'tabel', titel, hoofding, rijen };
  }

  function collectTabelRows(parent, namen, out) {
    for (const row of Array.from(parent.children)) {
      if (row.localName !== 'row') continue;
      const cellen = [];
      for (const entry of Array.from(row.children)) { if (entry.localName === 'entry') cellen.push(parseTabelEntry(entry, namen)); }
      out.push({ cellen });
    }
  }

  function parseTabelEntry(entry, namen) {
    const segments = [];
    let foundChild = false;
    for (const child of Array.from(entry.children)) {
      if (child.localName === 'Al') { segments.push(...parseInline(child, namen)); foundChild = true; }
      else { const txt = child.textContent || ''; if (txt) segments.push(...splitOpTermen(txt, namen)); foundChild = true; }
    }
    if (!foundChild) { const txt = entry.textContent || ''; if (txt) segments.push(...splitOpTermen(txt, namen)); }
    return { segments: mergeInline(segments) };
  }

  function parseBegrip(el, namen) {
    let term = '';
    const definitieSegments = [];
    for (const child of Array.from(el.children)) {
      if (child.localName === 'Term') { term = (child.textContent || '').trim(); continue; }
      if (child.localName === 'Definitie') {
        let foundAl = false;
        for (const dchild of Array.from(child.children)) {
          if (dchild.localName === 'Al') { definitieSegments.push(...parseInline(dchild, namen)); foundAl = true; }
        }
        if (!foundAl) { const txt = child.textContent || ''; if (txt) definitieSegments.push(...splitOpTermen(txt, namen)); }
      }
    }
    return { term, segments: mergeInline(definitieSegments) };
  }

  function parseLi(li, namen) {
    let marker = null;
    const inlineParts = [];
    for (const child of Array.from(li.childNodes)) {
      if (isElement(child)) {
        if (child.localName === 'LiNummer') { marker = (child.textContent || '').trim() || null; continue; }
        inlineParts.push(...parseInline(child, namen));
      } else if (child.nodeType === 3) {
        const txt = child.textContent || ''; if (txt) inlineParts.push(...splitOpTermen(txt, namen));
      }
    }
    return { marker, segments: mergeInline(inlineParts) };
  }

  function parseInline(el, namen) {
    const out = [];
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === 3) { const txt = child.textContent || ''; if (txt) out.push(...splitOpTermen(txt, namen)); continue; }
      if (!isElement(child)) continue;
      if (child.localName === 'IntRef' || child.localName === 'IntIoRef') {
        const ref = child.getAttribute('ref') || '', text = child.textContent || '';
        if (ref) out.push({ type: 'intref', text, eid: ref });
        else if (text) out.push(...splitOpTermen(text, namen));
        continue;
      }
      if (child.localName === 'NieuweTekst' || child.localName === 'VerwijderdeTekst') {
        const inner = parseRenvooiInner(child, namen);
        if (inner.length === 0) continue;
        out.push(child.localName === 'NieuweTekst' ? { type: 'ins', segments: inner } : { type: 'del', segments: inner });
        continue;
      }
      const text = child.textContent || '';
      if (text) out.push(...splitOpTermen(text, namen));
    }
    return mergeInline(out);
  }

  function parseRenvooiInner(el, namen) {
    const out = [];
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === 3) { const txt = child.textContent || ''; if (txt) out.push(...splitOpTermen(txt, namen)); continue; }
      if (!isElement(child)) continue;
      if (child.localName === 'NieuweTekst' || child.localName === 'VerwijderdeTekst') {
        const txt = child.textContent || ''; if (txt) out.push(...splitOpTermen(txt, namen)); continue;
      }
      if (child.localName === 'IntRef' || child.localName === 'IntIoRef') {
        const ref = child.getAttribute('ref') || '', text = child.textContent || '';
        if (ref) out.push({ type: 'intref', text, eid: ref });
        else if (text) out.push(...splitOpTermen(text, namen));
        continue;
      }
      const text = child.textContent || '';
      if (text) out.push(...splitOpTermen(text, namen));
    }
    return mergeInline(out);
  }

  function mergeInline(segments) {
    const out = [];
    for (const seg of segments) {
      const prev = out[out.length - 1];
      if (seg.type === 'plain' && prev && prev.type === 'plain') prev.text += seg.text;
      else out.push(seg);
    }
    return out;
  }

  function stripTags(s) { return s.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(); }
  function isElement(node) { return node.nodeType === 1; }

  function splitOpTermen(tekst, namen) {
    if (!namen || namen.length === 0 || !tekst) return tekst ? [{ type: 'plain', text: tekst }] : [];
    ensureCacheForNamen(namen);
    const regex = cachedNamenRegex, lookup = cachedNaamLookup;
    if (!regex || !lookup) return [{ type: 'plain', text: tekst }];
    regex.lastIndex = 0;
    const segments = [];
    let pos = 0, m;
    while ((m = regex.exec(tekst)) !== null) {
      const start = m.index, end = start + m[0].length;
      if (pos < start) segments.push({ type: 'plain', text: tekst.slice(pos, start) });
      const naam = lookup.get(m[0].toLowerCase()) || m[0];
      segments.push({ type: 'term', text: tekst.slice(start, end), naam });
      pos = end;
    }
    if (segments.length === 0) return [{ type: 'plain', text: tekst }];
    if (pos < tekst.length) segments.push({ type: 'plain', text: tekst.slice(pos) });
    return segments;
  }

  // ==========================================================================
  // 2. RENDER  (Block[] → veilige DOM)
  // ==========================================================================

  function inlineToNode(seg, emit) {
    if (seg.type === 'plain') return document.createTextNode(seg.text);
    if (seg.type === 'term') {
      const s = document.createElement('span');
      s.className = 'ocd-term'; s.dataset.naam = seg.naam; s.textContent = seg.text;
      s.addEventListener('click', () => emit('term-klik', { naam: seg.naam, text: seg.text }));
      return s;
    }
    if (seg.type === 'intref') {
      const a = document.createElement('a');
      a.className = 'ocd-intref'; a.href = '#'; a.dataset.eid = seg.eid; a.textContent = seg.text || seg.eid;
      a.addEventListener('click', (e) => { e.preventDefault(); emit('intref-klik', { eid: seg.eid, text: seg.text }); });
      return a;
    }
    if (seg.type === 'ins' || seg.type === 'del') {
      const el = document.createElement(seg.type === 'ins' ? 'ins' : 'del');
      el.className = seg.type === 'ins' ? 'ocd-ins' : 'ocd-del';
      appendInline(el, seg.segments, emit);
      return el;
    }
    return document.createTextNode('');
  }

  function appendInline(parent, segments, emit) {
    for (const seg of segments) parent.appendChild(inlineToNode(seg, emit));
  }

  function blockToNode(block, emit) {
    switch (block.type) {
      case 'al': {
        const p = document.createElement('p'); p.className = 'ocd-al';
        appendInline(p, block.segments, emit); return p;
      }
      case 'lijst': {
        const ul = document.createElement('ul'); ul.className = 'ocd-lijst';
        if (block.lijstType) ul.dataset.lijstType = block.lijstType;
        for (const item of block.items) {
          const li = document.createElement('li'); li.className = 'ocd-li';
          if (item.marker) {
            const m = document.createElement('span'); m.className = 'ocd-li-marker'; m.textContent = item.marker; li.appendChild(m);
          }
          const body = document.createElement('span'); body.className = 'ocd-li-body';
          appendInline(body, item.segments, emit); li.appendChild(body); ul.appendChild(li);
        }
        return ul;
      }
      case 'definitielijst': {
        const dl = document.createElement('dl'); dl.className = 'ocd-begrippen';
        for (const item of block.items) {
          const dt = document.createElement('dt'); dt.textContent = item.term;
          const dd = document.createElement('dd'); appendInline(dd, item.segments, emit);
          dl.appendChild(dt); dl.appendChild(dd);
        }
        return dl;
      }
      case 'tabel': {
        const table = document.createElement('table'); table.className = 'ocd-tabel';
        if (block.titel) { const cap = document.createElement('caption'); cap.textContent = block.titel; table.appendChild(cap); }
        if (block.hoofding.length) {
          const thead = document.createElement('thead');
          for (const rij of block.hoofding) thead.appendChild(tabelRij(rij, 'th', emit));
          table.appendChild(thead);
        }
        const tbody = document.createElement('tbody');
        for (const rij of block.rijen) tbody.appendChild(tabelRij(rij, 'td', emit));
        table.appendChild(tbody);
        return table;
      }
      default:
        return document.createTextNode('');
    }
  }

  function tabelRij(rij, cellTag, emit) {
    const tr = document.createElement('tr');
    for (const cel of rij.cellen) {
      const c = document.createElement(cellTag);
      appendInline(c, cel.segments, emit);
      tr.appendChild(c);
    }
    return tr;
  }

  /** Render Block[] → DocumentFragment. opts.emit optioneel. */
  function renderBlocks(blocks, opts = {}) {
    const emit = opts.emit || (() => {});
    const frag = document.createDocumentFragment();
    for (const block of blocks) frag.appendChild(blockToNode(block, emit));
    return frag;
  }

  /** Gemaks-helper: rauwe STOP-XML → DocumentFragment in één stap. */
  function renderTekst(tekst, opts = {}) {
    return renderBlocks(parseTekst(tekst, opts.namen || []), opts);
  }

  // ==========================================================================
  // 3. WEB COMPONENT  <ocd-regeltekst>
  // ==========================================================================

  const STYLE = `
    :host { display: block; color: inherit; font: inherit; line-height: 1.55; }
    .ocd-al { margin: 0 0 .6em; }
    .ocd-lijst { margin: 0 0 .6em; padding-left: 0; list-style: none; }
    .ocd-li { display: flex; gap: .5em; margin: .2em 0; }
    .ocd-li-marker { flex: 0 0 auto; min-width: 1.4em; font-weight: 600; }
    .ocd-begrippen { margin: 0 0 .6em; }
    .ocd-begrippen dt { font-weight: 600; margin-top: .4em; }
    .ocd-begrippen dd { margin: 0 0 .3em; padding-left: 1em; }
    table.ocd-tabel { border-collapse: collapse; margin: .4em 0 .8em; width: 100%; font-size: .95em; }
    table.ocd-tabel caption { text-align: left; font-weight: 600; padding: .2em 0; }
    table.ocd-tabel th, table.ocd-tabel td { border: 1px solid var(--ocd-border, #d5d5d5); padding: .3em .5em; text-align: left; vertical-align: top; }
    table.ocd-tabel thead th { background: var(--ocd-th-bg, #f2f4f6); }
    .ocd-term { border-bottom: 1px dotted var(--ocd-term, #39870c); cursor: help; }
    .ocd-intref { color: var(--ocd-link, #275937); text-decoration: underline; cursor: pointer; }
    .ocd-ins { text-decoration: none; background: #e7f6e7; }
    .ocd-del { color: #999; }
    .ocd-beg { margin: 0; }
    .ocd-badge { display: inline-block; margin-bottom: .5em; font-size: .72em; font-weight: 600;
      color: var(--ocd-badge-fg, #7a5b00); background: var(--ocd-badge-bg, #fdf3d7);
      border: 1px solid var(--ocd-badge-bd, #e8d9a8); border-radius: 9px; padding: .05em .6em; }
    .ocd-fallback-note { margin: 0 0 .4em; font-size: .8em; font-style: italic; color: #888; }
  `;

  // Class-syntax mag in een klassiek script (alleen import/export maakt het een module).
  let OcdRegeltekst = null;
  if (typeof HTMLElement !== 'undefined') {
    OcdRegeltekst = class extends HTMLElement {
      static get observedAttributes() { return ['weergave']; }

      constructor() {
        super();
        this._tekst = ''; this._begrijpelijk = null; this._namen = [];
        this.attachShadow({ mode: 'open' });
      }

      get tekst() { return this._tekst; }
      set tekst(v) { this._tekst = v || ''; this._render(); }
      get begrijpelijk() { return this._begrijpelijk; }
      set begrijpelijk(v) { this._begrijpelijk = v || null; this._render(); }
      get namen() { return this._namen; }
      set namen(v) { this._namen = Array.isArray(v) ? v : []; this._render(); }
      get weergave() { return this.getAttribute('weergave') || 'juridisch'; }
      set weergave(v) { this.setAttribute('weergave', v || 'juridisch'); }

      connectedCallback() {
        for (const p of ['tekst', 'begrijpelijk', 'namen']) {
          if (Object.prototype.hasOwnProperty.call(this, p)) { const v = this[p]; delete this[p]; this[p] = v; }
        }
        this._render();
      }
      attributeChangedCallback() { this._render(); }

      _emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
      }

      _render() {
        if (!this.shadowRoot) return;
        const root = this.shadowRoot;
        root.textContent = '';
        const style = document.createElement('style'); style.textContent = STYLE; root.appendChild(style);

        const toonBegrijpelijk = this.weergave === 'begrijpelijk' && this._begrijpelijk;
        if (toonBegrijpelijk) {
          const badge = document.createElement('span'); badge.className = 'ocd-badge'; badge.textContent = 'geen juridische status'; root.appendChild(badge);
          const p = document.createElement('p'); p.className = 'ocd-beg'; p.textContent = this._begrijpelijk; root.appendChild(p);
          return;
        }
        if (this.weergave === 'begrijpelijk' && !this._begrijpelijk) {
          const note = document.createElement('p'); note.className = 'ocd-fallback-note';
          note.textContent = 'nog geen begrijpelijke uitleg — juridische tekst getoond'; root.appendChild(note);
        }
        const emit = (t, d) => this._emit(t, d);
        root.appendChild(renderBlocks(parseTekst(this._tekst, this._namen), { emit }));
      }
    };

    if (typeof customElements !== 'undefined' && !customElements.get('ocd-regeltekst')) {
      customElements.define('ocd-regeltekst', OcdRegeltekst);
    }
  }

  // ── Publieke API (voor programmatisch gebruik zonder het element) ──
  global.ocdRegeltekst = {
    parseTekst, renderTekst, renderBlocks, blokkenVanCached,
    inlineFragmentCached, splitOpTermen, resetParserCache, OcdRegeltekst
  };

})(typeof window !== 'undefined' ? window : this);
