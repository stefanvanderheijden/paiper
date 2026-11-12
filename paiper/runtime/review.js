/* paiper review layer: comments (bubble per block, or on selected text) and direct text edits
   (Edit text, then click a paragraph, list item or heading). Talks to the paiper server (/api). */
(function () {
  'use strict';
  var CFG = window.PAIPER || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function txt(h) { var d = document.createElement('div'); d.innerHTML = h || ''; return (d.textContent || '').replace(/\s+/g, ' ').trim(); }
  var Q = '?doc=' + encodeURIComponent(CFG.doc || '');
  var items = [], target = null, editing = null;

  function api(method, path, body) {
    return fetch(CFG.api + path + Q, { method: method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.status); return d; }); });
  }
  function toast(m) { var t = $('.toast') || document.body.appendChild(el('div', 'toast')); t.textContent = m; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove('on'); }, 2600); }

  var panel = $('aside.panel'); if (!panel) return;
  panel.innerHTML =
    '<div class="ph"><div><h3>Review</h3><div class="sub" id="pstat"></div></div>' +
    '<button class="ph-copy" id="copybtn" title="Copy the open items as text"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/></svg>Copy</button></div>' +
    '<div class="composer" id="composer"><div class="q" id="cq"></div><textarea id="ctx" placeholder="Your comment. Short or detailed, both fine."></textarea>' +
    '<div class="row"><button class="btn primary" id="csave">Add</button><button class="btn" id="ccancel">Cancel</button><span class="hint">&#8984;&#8629; to add</span></div></div>' +
    '<div id="plist"></div>';

  function load() { return api('GET', '/comments').then(function (d) { items = d; render(); }, function () {}); }

  function blockOf(n) { while (n && n !== document.body) { if (n.nodeType === 1 && n.hasAttribute('data-cid')) return n; n = n.parentNode; } return null; }
  function section(b) { var h = null, sh = b.closest('.sheet') || b.parentNode; $$('h1[id], h2[id]').some(function (x) { if (x.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING || x === b) { h = x; return false; } return true; }); return h ? h.id : ''; }
  function excerpt(b) { var c = b.cloneNode(true); $$('.cbtn', c).forEach(function (x) { x.remove(); }); return (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160); }

  /* ------------------------------------------------------------ comments */
  function openComposer(b, quote) {
    if (document.body.classList.contains('panel-hidden')) { var pbt = $('#panelbtn'); if (pbt) pbt.click(); }
    target = { cid: b ? b.getAttribute('data-cid') : '', section: b ? section(b) : '', quote: quote || (b ? excerpt(b) : '') };
    $('#composer').classList.add('on'); $('#cq').textContent = target.quote || 'General';
    $('#ctx').value = ''; $('#ctx').focus(); panel.scrollTop = 0;
  }
  function closeComposer() { target = null; $('#composer').classList.remove('on'); }
  $('#ccancel').onclick = closeComposer;
  $('#csave').onclick = function () {
    var t = $('#ctx').value.trim(); if (!t || !target) return;
    api('POST', '/comments', { cid: target.cid, section: target.section, quote: target.quote, text: t, author: CFG.author })
      .then(function (c) { items.push(c); render(); closeComposer(); }, function (e) { toast('Not saved: ' + e.message); });
  };
  $('#ctx').addEventListener('keydown', function (e) { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') $('#csave').click(); if (e.key === 'Escape') closeComposer(); });

  function item(c) {
    var done = c.status !== 'open';
    var d = el('div', 'cm' + (done ? ' done' : ''));
    var who = c.author ? esc(c.author) + ' · ' : '';
    if (c.kind === 'edit') {
      d.innerHTML = '<div class="kind">Edit' + (c.status === 'open' ? ' · to carry into the build' : '') + '</div>' +
        '<div class="tx"><del>' + esc(txt(c.before)) + '</del><br><ins>' + esc(txt(c.after)) + '</ins></div>';
    } else {
      d.innerHTML = '<div class="qt">' + (c.quote ? esc(c.quote) : 'General') + '</div><div class="tx">' + esc(c.text) + '</div>';
    }
    if (c.reply) d.innerHTML += '<div class="reply"><b>Reply:</b> ' + esc(c.reply) + '</div>';
    var acts = '';
    if (c.kind === 'edit' && c.status !== 'processed') acts = '<a data-undo="' + c.id + '">undo</a>';
    else if (c.kind !== 'edit' && c.status === 'open') acts = '<a data-del="' + c.id + '">delete</a>';
    d.innerHTML += '<div class="meta"><span>' + who + esc(c.created) + '</span>' + acts + '</div>';
    d.onclick = function (e) {
      var del = e.target.getAttribute('data-del'), undo = e.target.getAttribute('data-undo');
      if (del) { api('DELETE', '/comments/' + del).then(load); return; }
      if (undo) { api('DELETE', '/edit/' + undo).then(function () { toast('Edit undone, reloading'); setTimeout(function () { location.reload(); }, 600); }, function (er) { toast('Could not undo: ' + er.message); }); return; }
      var b = c.cid && $('[data-cid="' + c.cid + '"]');
      if (b) { b.scrollIntoView({ behavior: 'smooth', block: 'center' }); b.classList.remove('flash'); void b.offsetWidth; b.classList.add('flash'); }
    };
    return d;
  }

  function render() {
    var open = items.filter(function (c) { return c.status === 'open'; });
    var done = items.filter(function (c) { return c.status !== 'open'; });
    $('#pstat').textContent = open.length ? open.length + ' open, for your agent to process' : 'Nothing open';
    var l = $('#plist'); l.innerHTML = '';
    l.appendChild(el('div', 'sec', 'Open'));
    if (!open.length) l.appendChild(el('div', 'empty', 'Hover a block and press the bubble, select text to comment on it, or switch to Edit (E) to change wording directly.'));
    open.slice().reverse().forEach(function (c) { l.appendChild(item(c)); });
    if (done.length) {
      var dt = el('details', 'done'); dt.open = !open.length; dt.appendChild(el('summary', null, 'Done (' + done.length + ')'));
      done.slice().reverse().forEach(function (c) { dt.appendChild(item(c)); }); l.appendChild(dt);
    }
    $$('[data-cid]').forEach(function (b) {
      var n = items.filter(function (c) { return c.cid === b.getAttribute('data-cid') && c.status === 'open' && c.kind !== 'edit'; }).length;
      var btn = $(':scope > .cbtn', b); if (btn) { btn.classList.toggle('has', n > 0); btn.textContent = n > 0 ? n : '+'; }
      b.classList.toggle('edited', items.some(function (c) { return c.kind === 'edit' && c.cid === b.getAttribute('data-cid') && c.status === 'open'; }));
    });
    var oc = $('#opencount'); if (oc) { oc.textContent = open.length; oc.hidden = !open.length; }
    var cb = $('#copybtn'); if (cb) cb.disabled = !open.length;
  }

  /* ------------------------------------------------------------ edits */
  var bar = el('div', 'edbar', '<button class="btn primary" data-e="save">Save</button><button class="btn" data-e="cancel">Cancel</button><span class="hint">&#8629; save · &#8679;&#8629; new line · Esc cancel</span>');
  bar.style.display = 'none'; document.body.appendChild(bar);
  function innerOf(b) { var c = b.cloneNode(true); $$('.cbtn', c).forEach(function (x) { x.remove(); }); return c.innerHTML; }
  function placeBar(b) { var r = b.getBoundingClientRect(); bar.style.display = 'flex'; bar.style.left = (window.scrollX + r.left) + 'px'; bar.style.top = (window.scrollY + r.bottom + 8) + 'px'; }
  function startEdit(b) {
    if (editing) finishEdit(false);
    editing = { b: b, before: innerOf(b), btn: $(':scope > .cbtn', b) };
    if (editing.btn) editing.btn.remove();
    b.setAttribute('contenteditable', 'true'); b.focus();
    placeBar(b);
  }
  function finishEdit(save) {
    if (!editing) return;
    var e = editing; editing = null; bar.style.display = 'none';
    e.b.removeAttribute('contenteditable');
    var after = innerOf(e.b);
    function restoreBtn() { if (e.btn) e.b.insertBefore(e.btn, e.b.firstChild); }
    if (!save || txt(after) === txt(e.before) && after === e.before) { e.b.innerHTML = e.before; restoreBtn(); return; }
    api('POST', '/edit', { cid: e.b.getAttribute('data-cid'), before: e.before, after: after, author: CFG.author }).then(function (r) {
      e.b.innerHTML = r.after; e.b.setAttribute('data-cid', r.cid); restoreBtn();
      items.push(r.item); render();
      toast(CFG.built ? 'Saved. Your agent carries it into the build script.' : 'Saved to the document.');
    }, function (er) { e.b.innerHTML = e.before; restoreBtn(); toast('Not saved: ' + er.message); });
  }
  bar.addEventListener('mousedown', function (e) { e.preventDefault(); });
  bar.addEventListener('click', function (e) { var a = e.target.getAttribute('data-e'); if (a) finishEdit(a === 'save'); });
  document.addEventListener('keydown', function (e) {
    if (editing && document.activeElement === editing.b) {
      if (e.key === 'Escape') { e.preventDefault(); finishEdit(false); }
      else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finishEdit(true); }
      else if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); document.execCommand('insertLineBreak'); }
      return;
    }
    if (e.target.closest && e.target.closest('input, textarea, [contenteditable="true"]')) return;
    if ((e.key === 'e' || e.key === 'E') && !e.metaKey && !e.ctrlKey && CFG.editable) toggleEdit();
    if (e.key === 'Escape' && document.body.classList.contains('editing')) toggleEdit(false);
  });
  document.addEventListener('paste', function (e) {
    if (!editing) return;
    e.preventDefault();
    document.execCommand('insertText', false, (e.clipboardData || window.clipboardData).getData('text/plain').replace(/\s+/g, ' '));
  });
  document.addEventListener('click', function (e) {
    if (!document.body.classList.contains('editing')) return;
    if (e.target.closest('.edbar')) return;
    var b = e.target.closest('[data-editable]');
    if (e.target.closest('a')) e.preventDefault();
    if (b && (!editing || editing.b !== b)) { e.preventDefault(); startEdit(b); }
    else if (!b && editing) finishEdit(true);
  }, true);
  function toggleEdit(on) {
    on = on === undefined ? !document.body.classList.contains('editing') : on;
    if (!on && editing) finishEdit(true);
    document.body.classList.toggle('editing', on);
    var b = $('#editbtn'), m = $('#modecomment');
    if (b) { b.classList.toggle('on', on); b.setAttribute('aria-checked', on); }
    if (m) { m.classList.toggle('on', !on); m.setAttribute('aria-checked', !on); }
    if (on) toast(CFG.built ? 'Click a paragraph, item or heading to edit. Generated document: your agent carries edits into the build.' : 'Click a paragraph, item or heading to edit it.');
  }

  /* ------------------------------------------------------------ start */
  function start() {
    $$('[data-cid]').forEach(function (b) {
      var btn = el('button', 'cbtn', '+'); btn.title = 'Comment on this';
      btn.onclick = function (e) { e.stopPropagation(); e.preventDefault(); openComposer(b); };
      b.insertBefore(btn, b.firstChild);
    });
    var sb = el('button', null, 'Comment on selection'); sb.id = 'selbtn'; document.body.appendChild(sb);
    var selState = null;
    document.addEventListener('mouseup', function (e) {
      if (e.target === sb || document.body.classList.contains('editing')) return;
      setTimeout(function () {
        var s = window.getSelection(), t = s ? s.toString().trim() : '';
        if (!t || !s.rangeCount) { sb.style.display = 'none'; return; }
        var b = blockOf(s.anchorNode); if (!b || !$('main.doc').contains(b)) { sb.style.display = 'none'; return; }
        var r = s.getRangeAt(0).getBoundingClientRect();
        sb.style.display = 'block'; sb.style.left = (window.scrollX + r.left) + 'px'; sb.style.top = (window.scrollY + r.top - 38) + 'px';
        selState = { b: b, q: t.slice(0, 300) };
      }, 0);
    });
    sb.onmousedown = function (e) { e.preventDefault(); };
    sb.onclick = function () { if (selState) openComposer(selState.b, selState.q); sb.style.display = 'none'; window.getSelection().removeAllRanges(); };

    var eb = $('#editbtn'), mb = $('#modecomment');
    if (eb) { if (CFG.editable) eb.onclick = function () { toggleEdit(true); }; else eb.remove(); }
    if (mb) mb.onclick = function () { toggleEdit(false); };
    /* review panel: show / hide, remembered per browser */
    var pb = $('#panelbtn'), PK = 'paiper-panel-hidden';
    function setPanel(hidden) {
      document.body.classList.toggle('panel-hidden', hidden);
      if (pb) pb.classList.toggle('on', !hidden);
      try { localStorage.setItem(PK, hidden ? '1' : ''); } catch (e) {}
    }
    var hid = false; try { hid = !!localStorage.getItem(PK); } catch (e) {}
    setPanel(hid);
    if (pb) pb.onclick = function () { setPanel(!document.body.classList.contains('panel-hidden')); };
    var copy = $('#copybtn');
    if (copy) copy.onclick = function () {
      var t = items.filter(function (c) { return c.status === 'open'; }).map(function (c, i) {
        return (i + 1) + '. ' + (c.kind === 'edit' ? 'EDIT: "' + txt(c.before) + '" -> "' + txt(c.after) + '"' : '[' + (c.section || 'general') + '] ' + (c.quote ? '"' + c.quote + '"\n   ' : '') + c.text);
      }).join('\n\n');
      if (!t) return;
      navigator.clipboard.writeText(t).then(function () { toast('Open items copied'); });
    };
    load();
    setInterval(function () { if (!target && !editing && !document.hidden) load(); }, 15000);
  }
  if (document.documentElement.getAttribute('data-paged')) start(); else window.addEventListener('paged', start);
})();
