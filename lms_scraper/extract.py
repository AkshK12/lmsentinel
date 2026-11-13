"""JavaScript snippets evaluated inside each page frame.

PeopleSoft renders grids as <table> elements with <th> header rows; we extract
every visible table that looks like a data grid into {headers, rows:[{header: value}]}.
"""

GRIDS_JS = r"""
() => {
  const clean = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  for (const t of document.querySelectorAll('table')) {
    if (!visible(t)) continue;
    const rows = [...t.rows];
    const hi = rows.findIndex(r => [...r.cells].some(c => c.tagName === 'TH'));
    if (hi < 0) continue;
    let headers = [...rows[hi].cells].map(c => clean(c.innerText));
    if (headers.filter(Boolean).length < 2) continue;
    const seen = {};
    headers = headers.map((h, i) => {
      h = h || `col${i}`;
      if (seen[h] !== undefined) { seen[h]++; return `${h} (${seen[h]})`; }
      seen[h] = 0; return h;
    });
    const data = [];
    for (const r of rows.slice(hi + 1)) {
      const cells = [...r.cells];
      if (cells.length !== headers.length) continue;
      if (cells.some(c => c.querySelector('table'))) continue;   // layout row, not data
      const obj = {};
      cells.forEach((c, i) => { obj[headers[i]] = clean(c.innerText); });
      if (Object.values(obj).some(Boolean)) data.push(obj);
    }
    if (!data.length) continue;
    let title = clean(t.caption && t.caption.innerText) || clean(t.getAttribute('summary'));
    if (!title) {
      const box = t.closest('[id^="win0div"]');
      const lbl = box && box.querySelector('.PSLEVEL1GRIDLABEL, .PSGRIDLABEL, .PSLEVEL1GRIDLABELNBO, .ps_grid-title');
      title = clean(lbl && lbl.innerText);
    }
    out.push({ id: t.id || '', title, headers, rows: data });
  }
  return out;
}
"""

LINKS_JS = r"""
() => [...document.querySelectorAll('table a[id]')]
  .filter(a => a.offsetWidth || a.offsetHeight)
  .map(a => ({ id: a.id, text: (a.innerText || '').replace(/\s+/g, ' ').trim() }))
  .filter(l => l.text)
"""

TERMS_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('input[type=radio]').forEach((r, i) => {
    const row = r.closest('tr');
    out.push({ kind: 'radio', id: r.id, index: i,
               label: clean(row ? row.innerText : (r.labels && r.labels[0] ? r.labels[0].innerText : r.value)) });
  });
  document.querySelectorAll('select').forEach(s => {
    if (!/term|strm/i.test((s.id || '') + (s.name || ''))) return;
    [...s.options].forEach(o => { if (o.value) out.push({ kind: 'select', id: s.id, value: o.value, label: clean(o.text) }); });
  });
  return out;
}
"""
