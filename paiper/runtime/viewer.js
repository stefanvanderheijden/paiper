/* paiper viewer: lays the report out as sheets (A4 or Letter), zoom (buttons, + - 0, pinch / ctrl+wheel),
   a lightbox for photos and charts (wheel/pinch to zoom, drag to pan), and the contents highlight.
   Blocks are moved, not cloned, so comment anchors keep working. WeasyPrint does not run this. */
(function () {
  'use strict';
  var CFG = {};
  try { CFG = JSON.parse(document.getElementById('paiper-config').textContent); } catch (e) {}
  window.PAIPER = CFG;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  /* ------------------------------------------------------------ pages */
  function paginate() {
    var doc = $('main.doc');
    if (!doc) return;
    var toc = $('.tocpage', doc);
    var sections = $$(':scope > section', doc);
    var blocks = $$(':scope > section, :scope > .tocpage', doc);     /* in document order */
    var units = [], pend = [];
    function push(el, after) {
      var els = pend.concat([el]); pend = [];
      units.push({ els: els, pb: els[0].classList.contains('pb'), after: !!after });
    }
    blocks.forEach(function (s) {
      if (s === toc) { if (pend.length) push(pend.pop()); push(toc, true); units[units.length - 1].pb = true; return; }
      $$(':scope > *', s).forEach(function (c) {
        if (c.tagName === 'H1' || c.tagName === 'H2' || c.tagName === 'H3' || c.classList.contains('kwn')) pend.push(c); else push(c);
      });
    });
    if (pend.length) push(pend.pop());

    var sheets = [], f = CFG.footer || {};
    var head = CFG.logo ? '<img src="' + esc(CFG.logo) + '" alt="">' : esc(CFG.title || '');
    function newSheet() {
      var s = document.createElement('div'); s.className = 'sheet';
      s.innerHTML = '<div class="sh-head">' + head + '</div><div class="sh-body"></div>' +
        '<div class="sh-foot"><span>' + esc(f.left) + '</span><span>' + esc(f.center) + '</span><span class="pn"></span></div>';
      doc.appendChild(s);
      var o = { el: s, body: $('.sh-body', s), n: 0 }; sheets.push(o); return o;
    }
    function fits(sh, u) {
      return u.els[u.els.length - 1].getBoundingClientRect().bottom <= sh.body.getBoundingClientRect().bottom + 0.5;
    }
    /* a heading does not start in the bottom part of a page: it needs room below it (the same rule as in print) */
    var ROOM = { H1: 0.26, H2: 0.26, H3: 0.16 };
    function low(sh, u) {
      var h = u.els[0], need = ROOM[h.tagName]; if (!need) return false;
      var b = sh.body.getBoundingClientRect();
      return b.bottom - h.getBoundingClientRect().top < need * b.height;
    }
    var cur = null;
    units.forEach(function (u) {
      if (!cur || (u.pb && cur.n > 0)) cur = newSheet();
      u.els.forEach(function (e) { cur.body.appendChild(e); });
      if ((!fits(cur, u) || low(cur, u)) && cur.n > 0) {
        u.els.forEach(function (e) { e.parentNode.removeChild(e); });
        cur = newSheet();
        u.els.forEach(function (e) { cur.body.appendChild(e); });
      }
      cur.n++;
      if (u.after) cur = null;
    });
    sections.forEach(function (s) { if (s.parentNode) s.parentNode.removeChild(s); });
    var right = (f.right || 'page {page}');
    sheets.forEach(function (s, i) { $('.pn', s.el).textContent = right.replace('{pages}', sheets.length + 1).replace('{page}', i + 2); });
    if (toc) {
      $$('a', toc).forEach(function (a) {
        var t = document.getElementById(a.getAttribute('href').slice(1));
        var sh = t && t.closest('.sheet'); if (!sh) return;
        var n = sheets.map(function (x) { return x.el; }).indexOf(sh) + 2;
        a.insertAdjacentHTML('beforeend', '<span class="lead"></span><span class="pg">' + n + '</span>');
      });
    }
  }

  /* ------------------------------------------------------------ zoom */
  var LEVELS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  var ZKEY = 'paiper-zoom:' + (CFG.doc || location.pathname);
  var z = 1;
  function setZoom(nz, anchorY) {
    nz = Math.max(0.4, Math.min(3, nz));
    var doc = $('main.doc'); if (!doc) return;
    var y = anchorY == null ? window.innerHeight / 2 : anchorY;
    var docTop = doc.getBoundingClientRect().top + window.scrollY;
    var rel = (window.scrollY + y - docTop) / z;          /* document point under the anchor, unzoomed */
    z = nz;
    document.documentElement.style.setProperty('--z', z);
    window.scrollTo(window.scrollX, docTop + rel * z - y);
    var l = $('#zlabel'); if (l) l.textContent = Math.round(z * 100) + '%';
    try { localStorage.setItem(ZKEY, String(z)); } catch (e) {}
  }
  function step(dir) {
    var i = dir > 0 ? LEVELS.findIndex(function (v) { return v > z + 0.001; }) : -1;
    if (dir < 0) { for (var k = LEVELS.length - 1; k >= 0; k--) if (LEVELS[k] < z - 0.001) { i = k; break; } }
    if (i >= 0) setZoom(LEVELS[i]);
  }
  function fit() {
    var doc = $('main.doc'); if (!doc) return;
    var cols = getComputedStyle($('.layout')).gridTemplateColumns.split(' ');
    var side = 0;
    if ($('nav.toc') && getComputedStyle($('nav.toc')).display !== 'none') side += 190 + 28;
    if ($('aside.panel') && getComputedStyle($('aside.panel')).display !== 'none' && cols.length > 1) side += 340 + 28;
    var avail = document.documentElement.clientWidth - side - 40;
    var base = doc.getBoundingClientRect().width / z;
    setZoom(Math.round((avail / base) * 100) / 100);
  }
  function initZoom() {
    var saved = 1; try { saved = parseFloat(localStorage.getItem(ZKEY)) || 1; } catch (e) {}
    if (saved !== 1) setZoom(saved);
    var b;
    if ((b = $('#zin'))) b.onclick = function () { step(1); };
    if ((b = $('#zout'))) b.onclick = function () { step(-1); };
    if ((b = $('#zlabel'))) b.onclick = function () { setZoom(1); };
    if ((b = $('#zfit'))) b.onclick = fit;
    document.addEventListener('keydown', function (e) {
      if (e.target.closest && e.target.closest('input, textarea, [contenteditable="true"]')) return;
      if (lb.classList.contains('on')) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === '+' || e.key === '=') { step(1); e.preventDefault(); }
      else if (e.key === '-') { step(-1); e.preventDefault(); }
      else if (e.key === '0') { setZoom(1); e.preventDefault(); }
    });
    /* trackpad pinch arrives as ctrl+wheel: zoom the document, not the browser */
    window.addEventListener('wheel', function (e) {
      if (!e.ctrlKey || lb.classList.contains('on')) return;
      e.preventDefault();
      setZoom(z * Math.exp(-e.deltaY * 0.01), e.clientY);
    }, { passive: false });
  }

  /* ------------------------------------------------------------ lightbox */
  var lb = document.createElement('div'); lb.className = 'lightbox';
  lb.innerHTML = '<div class="stage"></div><div class="lbbar"><button data-a="out">&minus;</button><button data-a="in">+</button>' +
    '<button data-a="fit">Fit</button><button data-a="one">100%</button><button data-a="close">Close (Esc)</button></div><div class="lbcap"></div>';
  document.body.appendChild(lb);
  var stage = $('.stage', lb), S = { s: 1, x: 0, y: 0, w: 0, h: 0 };
  function apply() { stage.style.transform = 'translate(' + S.x + 'px,' + S.y + 'px) scale(' + S.s + ')'; }
  function fitLb() {
    var vw = window.innerWidth, vh = window.innerHeight - 40;
    S.s = Math.min(vw * 0.94 / S.w, vh * 0.92 / S.h);
    S.x = (vw - S.w * S.s) / 2; S.y = (vh - S.h * S.s) / 2 + 10; apply();
  }
  function zoomAt(f, cx, cy) {
    var ns = Math.max(0.05, Math.min(40, S.s * f));
    S.x = cx - (cx - S.x) * ns / S.s; S.y = cy - (cy - S.y) * ns / S.s; S.s = ns; apply();
  }
  function openLb(el) {
    stage.innerHTML = '';
    var fig = el.closest('figure'), cap = fig && $('figcaption', fig);
    $('.lbcap', lb).textContent = cap ? cap.textContent : '';
    if (el.tagName === 'IMG') {
      var im = new Image();
      im.onload = function () { S.w = im.naturalWidth; S.h = im.naturalHeight; fitLb(); };
      im.src = el.currentSrc || el.src; stage.appendChild(im);
    } else {
      var c = el.cloneNode(true), vb = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
      var r = el.getBoundingClientRect();
      S.w = vb.length === 4 && vb[2] ? vb[2] * 1.5 : r.width; S.h = vb.length === 4 && vb[3] ? vb[3] * 1.5 : r.height;
      c.setAttribute('width', S.w); c.setAttribute('height', S.h); c.style.width = S.w + 'px'; c.style.height = S.h + 'px';
      stage.appendChild(c); fitLb();
    }
    lb.classList.add('on');
  }
  function closeLb() { lb.classList.remove('on'); stage.innerHTML = ''; }
  lb.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0025)), e.clientX, e.clientY);
  }, { passive: false });
  var ptrs = {}, last = null, pinch = null;
  lb.addEventListener('pointerdown', function (e) {
    if (e.target.closest('.lbbar')) return;
    lb.setPointerCapture(e.pointerId); ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
    var ids = Object.keys(ptrs);
    if (ids.length === 1) { last = { x: e.clientX, y: e.clientY, moved: false }; lb.classList.add('drag'); }
    if (ids.length === 2) { var a = ptrs[ids[0]], b = ptrs[ids[1]]; pinch = Math.hypot(a.x - b.x, a.y - b.y); }
  });
  lb.addEventListener('pointermove', function (e) {
    if (!ptrs[e.pointerId]) return;
    ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
    var ids = Object.keys(ptrs);
    if (ids.length === 2 && pinch) {
      var a = ptrs[ids[0]], b = ptrs[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt(d / pinch, (a.x + b.x) / 2, (a.y + b.y) / 2); pinch = d; return;
    }
    if (last) { S.x += e.clientX - last.x; S.y += e.clientY - last.y; if (Math.abs(e.clientX - last.x) + Math.abs(e.clientY - last.y) > 2) last.moved = true; last.x = e.clientX; last.y = e.clientY; apply(); }
  });
  function up(e) {
    var wasClick = last && !last.moved && Object.keys(ptrs).length === 1;
    delete ptrs[e.pointerId]; if (Object.keys(ptrs).length < 2) pinch = null;
    if (!Object.keys(ptrs).length) { lb.classList.remove('drag'); if (wasClick && e.target === lb) closeLb(); last = null; }
  }
  lb.addEventListener('pointerup', up); lb.addEventListener('pointercancel', up);
  lb.addEventListener('dblclick', function (e) { if (S.s < 1.5) zoomAt(2.5, e.clientX, e.clientY); else fitLb(); });
  $('.lbbar', lb).addEventListener('click', function (e) {
    var a = e.target.getAttribute('data-a'), cx = window.innerWidth / 2, cy = window.innerHeight / 2;
    if (a === 'close') closeLb(); else if (a === 'fit') fitLb(); else if (a === 'in') zoomAt(1.4, cx, cy); else if (a === 'out') zoomAt(1 / 1.4, cx, cy);
    else if (a === 'one') zoomAt(1 / S.s * (stage.firstChild && stage.firstChild.tagName === 'IMG' ? 1 : 1), cx, cy);
  });
  document.addEventListener('keydown', function (e) {
    if (!lb.classList.contains('on')) return;
    if (e.key === 'Escape') closeLb();
    else if (e.key === '+' || e.key === '=') zoomAt(1.4, window.innerWidth / 2, window.innerHeight / 2);
    else if (e.key === '-') zoomAt(1 / 1.4, window.innerWidth / 2, window.innerHeight / 2);
    else if (e.key === '0') fitLb();
  });
  window.addEventListener('resize', function () { if (lb.classList.contains('on')) fitLb(); });
  document.addEventListener('click', function (e) {
    if (document.body.classList.contains('editing')) return;
    var sel = window.getSelection && window.getSelection().toString(); if (sel) return;
    var t = e.target.closest && e.target.closest('main.doc img, main.doc svg');
    if (!t || t.closest('.cover, .sh-head, .cbtn')) return;
    if (t.tagName !== 'IMG' && t.tagName.toLowerCase() === 'svg' && t.parentNode.closest && t.parentNode.closest('svg')) t = t.parentNode.closest('svg');
    e.preventDefault(); openLb(t);
  });

  /* ------------------------------------------------------------ contents highlight */
  function spy() {
    var cur = null;
    $$('main.doc h1[id], main.doc h2[id]').forEach(function (s) { if (s.getBoundingClientRect().top < 140) cur = s.id; });
    $$('nav.toc a').forEach(function (a) { a.classList.toggle('on', a.getAttribute('href') === '#' + cur); });
  }

  function done() {
    document.documentElement.setAttribute('data-paged', '1');
    initZoom();
    window.addEventListener('scroll', spy, { passive: true }); spy();
    /* the browser jumped to the anchor before the blocks moved onto sheets: undo any scroll inside the
       sheets (overflow hidden) and scroll the window itself to the anchor's new place */
    $$('.sheet, .sh-body, .cover, .layout, main.doc').forEach(function (el) { el.scrollTop = 0; el.scrollLeft = 0; });
    function jump(id, smooth) {
      var t = id && document.getElementById(id);
      if (!t) return false;
      $$('.sheet, .sh-body').forEach(function (el) { el.scrollTop = 0; el.scrollLeft = 0; });
      window.scrollTo({ left: 0, top: t.getBoundingClientRect().top + window.scrollY - 70, behavior: smooth ? 'smooth' : 'auto' });
      return true;
    }
    /* in-page links (contents, cross references) scroll the window, never the clipped sheets */
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a || document.body.classList.contains('editing')) return;
      var id = decodeURIComponent(a.getAttribute('href').slice(1));
      if (jump(id, true)) { e.preventDefault(); history.replaceState(null, '', '#' + id); }
    });
    if (location.hash) setTimeout(function () { jump(decodeURIComponent(location.hash.slice(1))); }, 0);
    window.dispatchEvent(new Event('paged'));
  }
  function go() { try { paginate(); } catch (e) { console.error('pagination failed', e); } done(); }
  var waits = $$('img').filter(function (i) { return !i.complete; }).map(function (i) { return new Promise(function (r) { i.onload = i.onerror = r; }); });
  waits.push(document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve());
  Promise.all(waits).then(go, go);
})();
