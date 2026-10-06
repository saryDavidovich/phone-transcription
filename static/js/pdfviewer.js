/* צופה PDF פנימי (מבוסס pdf.js) עם סרגל כלים: סיבוב, זום, התאמה לרוחב/לעמוד,
   ניווט עמודים, הורדה, הדפסה ומסך מלא - במקום הצופה המובנה של הדפדפן, שבתוך
   iframe עם data URI מגיע בלי סרגל כלים ונכשל בקבצים כבדים.
   שימוש: PdfViewer.mount(element, {uri: 'data:application/pdf;base64,...', fit: 'width'|'page', filename})
   מחזיר אובייקט עם load(uri) להחלפת המסמך (למשל מעבר בין "חלקים"). */
(function () {
  'use strict';

  var WORKER_SRC = '/static/vendor/pdfjs/pdf.worker.min.js';
  var MAX_CANVAS_SIDE = 4096;
  var STYLE_ID = 'pdfv-style';

  var CSS = [
    '.pdfv{display:flex;flex-direction:column;width:100%;height:100%;min-height:260px;background:#525659;direction:rtl;position:relative}',
    '.pdfv:fullscreen{background:#525659}',
    '.pdfv-bar{direction:ltr;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:4px;padding:5px 8px;background:#323639;color:#fff;font:13px Arial,sans-serif;flex:0 0 auto;user-select:none}',
    '.pdfv-bar button{background:#4a4e51;color:#fff;border:1px solid #5f6368;border-radius:6px;padding:4px 9px;font:13px Arial,sans-serif;cursor:pointer;line-height:1.2;min-width:30px}',
    '.pdfv-bar button:hover{background:#5f6368}',
    '.pdfv-bar button.on{background:#1a73e8;border-color:#1a73e8}',
    '.pdfv-bar .sep{width:1px;align-self:stretch;background:#5f6368;margin:0 3px}',
    '.pdfv-bar .lbl{min-width:46px;text-align:center;direction:ltr;unicode-bidi:isolate}',
    '.pdfv-bar input.pg{width:42px;text-align:center;background:#202124;color:#fff;border:1px solid #5f6368;border-radius:5px;padding:3px;font:13px Arial,sans-serif}',
    '.pdfv-scroll{flex:1 1 auto;overflow:auto;padding:10px 0;text-align:center;-webkit-overflow-scrolling:touch}',
    '.pdfv-page{position:relative;display:block;margin:0 auto 10px;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.5)}',
    '.pdfv-page canvas{display:block;width:100%;height:100%}',
    '.pdfv-msg{color:#fff;font:14px Arial,sans-serif;padding:30px 16px;text-align:center}',
    '.pdfv-msg a{color:#8ab4f8}'
  ].join('\n');

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID; s.textContent = CSS;
    document.head.appendChild(s);
  }

  function dataUriToBytes(uri) {
    var i = uri.indexOf(',');
    var bin = atob(uri.slice(i + 1));
    var out = new Uint8Array(bin.length);
    for (var k = 0; k < bin.length; k++) out[k] = bin.charCodeAt(k);
    return out;
  }

  function mkBtn(label, title, onClick, cls) {
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = label; b.title = title;
    if (cls) b.className = cls;
    b.addEventListener('click', onClick);
    return b;
  }

  function mount(el, opts) {
    opts = opts || {};
    injectStyle();
    el.classList.add('pdfv');
    el.innerHTML = '';

    var hasLib = (typeof window.pdfjsLib !== 'undefined');
    if (hasLib) {
      try { window.pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER_SRC; } catch (e) { hasLib = false; }
    }

    var state = {
      doc: null, bytes: null, uri: null,
      rotation: 0,          // סיבוב ידני של המשתמש (0/90/180/270)
      fit: opts.fit || 'width',   // 'width' | 'page' | 'custom'
      zoom: 1,              // יחסי להתאמה לרוחב (בשימוש כש-fit == 'custom')
      pages: [],            // [{wrap, canvas, w, h, task, rendered}]
      current: 1,
      loadId: 0
    };

    var bar = document.createElement('div'); bar.className = 'pdfv-bar';
    var scroll = document.createElement('div'); scroll.className = 'pdfv-scroll';
    el.appendChild(bar); el.appendChild(scroll);

    var zoomLbl = document.createElement('span'); zoomLbl.className = 'lbl';
    var pgInput = document.createElement('input'); pgInput.className = 'pg'; pgInput.type = 'text'; pgInput.value = '1';
    var pgTotal = document.createElement('span'); pgTotal.className = 'lbl'; pgTotal.textContent = '/ 1';
    var btnWidth, btnPage;

    function sep() { var s = document.createElement('span'); s.className = 'sep'; return s; }

    bar.appendChild(mkBtn('⟲', 'סיבוב שמאלה', function () { rotate(-90); }));
    bar.appendChild(mkBtn('⟳', 'סיבוב ימינה', function () { rotate(90); }));
    bar.appendChild(sep());
    bar.appendChild(mkBtn('−', 'הקטנה', function () { zoomBy(1 / 1.2); }));
    bar.appendChild(zoomLbl);
    bar.appendChild(mkBtn('+', 'הגדלה', function () { zoomBy(1.2); }));
    btnWidth = mkBtn('↔', 'התאמה לרוחב', function () { setFit('width'); });
    btnPage = mkBtn('▢', 'התאמה לעמוד שלם', function () { setFit('page'); });
    bar.appendChild(btnWidth); bar.appendChild(btnPage);
    bar.appendChild(sep());
    bar.appendChild(mkBtn('▲', 'עמוד קודם', function () { goPage(state.current - 1); }));
    bar.appendChild(pgInput); bar.appendChild(pgTotal);
    bar.appendChild(mkBtn('▼', 'עמוד הבא', function () { goPage(state.current + 1); }));
    bar.appendChild(sep());
    bar.appendChild(mkBtn('⬇', 'הורדת הקובץ', download));
    bar.appendChild(mkBtn('🖨', 'הדפסה', printDoc));
    bar.appendChild(mkBtn('⤢', 'מסך מלא', fullscreen));

    pgInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { goPage(parseInt(pgInput.value, 10) || 1); e.preventDefault(); }
    });

    function message(html) {
      scroll.innerHTML = '<div class="pdfv-msg">' + html + '</div>';
    }

    // ---------- פריסה ----------
    function viewportFor(info, scale) {
      var rot = ((info.baseRotate + state.rotation) % 360 + 360) % 360;
      return { rot: rot, w: (rot % 180 === 0 ? info.w0 : info.h0) * scale, h: (rot % 180 === 0 ? info.h0 : info.w0) * scale };
    }

    function currentScale(info) {
      var availW = Math.max(120, scroll.clientWidth - 24);
      var availH = Math.max(120, scroll.clientHeight - 20);
      var rotated = (((info.baseRotate + state.rotation) % 360 + 360) % 360) % 180 !== 0;
      var pw = rotated ? info.h0 : info.w0, ph = rotated ? info.w0 : info.h0;
      var fitW = availW / pw, fitH = availH / ph;
      if (state.fit === 'width') return fitW;
      if (state.fit === 'page') return Math.min(fitW, fitH);
      return fitW * state.zoom;
    }

    function updateButtons() {
      btnWidth.classList.toggle('on', state.fit === 'width');
      btnPage.classList.toggle('on', state.fit === 'page');
      if (state.pages.length) {
        var sc = currentScale(state.pages[0].info);
        var pct = Math.round(sc * 100);
        zoomLbl.textContent = pct + '%';
      }
    }

    function layout() {
      if (!state.pages.length) return;
      state.pages.forEach(function (p) {
        var sc = currentScale(p.info);
        var vp = viewportFor(p.info, sc);
        p.scale = sc; p.rot = vp.rot;
        p.wrap.style.width = Math.round(vp.w) + 'px';
        p.wrap.style.height = Math.round(vp.h) + 'px';
        p.rendered = false;
        if (p.task) { try { p.task.cancel(); } catch (e) {} p.task = null; }
      });
      updateButtons();
      renderVisible();
    }

    // ---------- רינדור עצל ----------
    function renderVisible() {
      var top = scroll.scrollTop, bottom = top + scroll.clientHeight;
      var margin = scroll.clientHeight; // מרנדרים קצת מעבר למסך
      state.pages.forEach(function (p) {
        var t = p.wrap.offsetTop - scroll.offsetTop, b = t + p.wrap.offsetHeight;
        if (b >= top - margin && t <= bottom + margin) renderPage(p);
      });
      updateCurrent();
    }

    function renderPage(p) {
      if (p.rendered || p.task || !state.doc) return;
      var myLoad = state.loadId, scale = p.scale, rot = p.rot;
      state.doc.getPage(p.index).then(function (page) {
        if (myLoad !== state.loadId) return;
        var dpr = Math.min(window.devicePixelRatio || 1, 2);
        var vp = page.getViewport({ scale: scale * dpr, rotation: rot });
        var maxSide = Math.max(vp.width, vp.height);
        if (maxSide > MAX_CANVAS_SIDE) {
          vp = page.getViewport({ scale: scale * dpr * (MAX_CANVAS_SIDE / maxSide), rotation: rot });
        }
        var canvas = document.createElement('canvas');
        canvas.width = Math.floor(vp.width); canvas.height = Math.floor(vp.height);
        var task = page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
        p.task = task;
        task.promise.then(function () {
          if (myLoad !== state.loadId) return;
          p.task = null; p.rendered = true;
          p.wrap.innerHTML = ''; p.wrap.appendChild(canvas);
        }).catch(function () { p.task = null; });
      }).catch(function () { p.task = null; });
    }

    var scrollTimer = null;
    scroll.addEventListener('scroll', function () {
      if (scrollTimer) return;
      scrollTimer = setTimeout(function () { scrollTimer = null; renderVisible(); }, 60);
    });

    function updateCurrent() {
      if (!state.pages.length) return;
      var mid = scroll.scrollTop + scroll.clientHeight / 3, cur = 1;
      state.pages.forEach(function (p, i) {
        if (p.wrap.offsetTop - scroll.offsetTop <= mid) cur = i + 1;
      });
      state.current = cur;
      if (document.activeElement !== pgInput) pgInput.value = String(cur);
    }

    // ---------- פעולות ----------
    function rotate(delta) {
      state.rotation = ((state.rotation + delta) % 360 + 360) % 360;
      layout();
    }
    function setFit(mode) { state.fit = mode; state.zoom = 1; layout(); }
    function zoomBy(f) {
      if (!state.pages.length) return;
      var cur = currentScale(state.pages[0].info), info = state.pages[0].info;
      var rotated = (((info.baseRotate + state.rotation) % 360 + 360) % 360) % 180 !== 0;
      var pw = rotated ? info.h0 : info.w0;
      var fitW = Math.max(120, scroll.clientWidth - 24) / pw;
      var next = Math.min(Math.max(cur * f, 0.1), 8);
      state.fit = 'custom'; state.zoom = next / fitW;
      layout();
    }
    function goPage(n) {
      if (!state.pages.length) return;
      n = Math.min(Math.max(n, 1), state.pages.length);
      var p = state.pages[n - 1];
      scroll.scrollTop = p.wrap.offsetTop - scroll.offsetTop - 6;
      state.current = n; pgInput.value = String(n);
      renderVisible();
    }
    function download() {
      if (!state.bytes) return;
      var blob = new Blob([state.bytes], { type: 'application/pdf' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = opts.filename || 'document.pdf';
      document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    }
    function printDoc() {
      if (!state.bytes) return;
      var blob = new Blob([state.bytes], { type: 'application/pdf' });
      var url = URL.createObjectURL(blob);
      var f = document.createElement('iframe');
      f.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0';
      f.src = url;
      f.onload = function () {
        try { f.contentWindow.focus(); f.contentWindow.print(); } catch (e) { window.open(url, '_blank'); }
        setTimeout(function () { URL.revokeObjectURL(url); f.remove(); }, 60000);
      };
      document.body.appendChild(f);
    }
    function fullscreen() {
      if (document.fullscreenElement) { document.exitFullscreen(); return; }
      if (el.requestFullscreen) el.requestFullscreen();
    }

    // ctrl + גלגלת = זום
    scroll.addEventListener('wheel', function (e) {
      if (!e.ctrlKey) return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1);
    }, { passive: false });

    if (window.ResizeObserver) {
      var rt = null;
      new ResizeObserver(function () {
        if (rt) clearTimeout(rt);
        rt = setTimeout(function () { if (state.fit !== 'custom') layout(); else renderVisible(); }, 120);
      }).observe(scroll);
    }

    // ---------- טעינה ----------
    function fallbackIframe(uri) {
      el.classList.remove('pdfv');
      el.innerHTML = '';
      var f = document.createElement('iframe');
      f.src = uri; f.style.cssText = 'width:100%;height:100%;border:0;display:block'; f.title = 'מסמך';
      el.appendChild(f);
    }

    function load(uri) {
      state.loadId++;
      var myLoad = state.loadId;
      state.uri = uri; state.pages = []; state.doc = null; state.rotation = 0; state.current = 1;
      if (!uri) { message('⚠ הקובץ אינו זמין'); return; }
      if (!hasLib) { fallbackIframe(uri); return; }
      message('טוען...');
      var bytes;
      try { bytes = dataUriToBytes(uri); } catch (e) { fallbackIframe(uri); return; }
      state.bytes = bytes;
      window.pdfjsLib.getDocument({ data: bytes.slice(0) }).promise.then(function (doc) {
        if (myLoad !== state.loadId) return;
        state.doc = doc;
        var tasks = [];
        for (var i = 1; i <= doc.numPages; i++) tasks.push(doc.getPage(i));
        return Promise.all(tasks).then(function (pgs) {
          if (myLoad !== state.loadId) return;
          scroll.innerHTML = '';
          state.pages = pgs.map(function (pg, idx) {
            var vp = pg.getViewport({ scale: 1, rotation: 0 });
            var wrap = document.createElement('div'); wrap.className = 'pdfv-page';
            scroll.appendChild(wrap);
            return { index: idx + 1, wrap: wrap, task: null, rendered: false,
                     info: { w0: vp.width, h0: vp.height, baseRotate: pg.rotate || 0 } };
          });
          pgInput.value = '1'; pgTotal.textContent = '/ ' + doc.numPages;
          scroll.scrollTop = 0;
          layout();
        });
      }).catch(function (err) {
        if (myLoad !== state.loadId) return;
        if (window.console) console.error('pdfviewer: failed, falling back to iframe', err);
        fallbackIframe(uri);
      });
    }

    var api = { load: load, rotate: rotate, el: el };
    el.__pdfv = api;
    if (opts.uri) load(opts.uri); else message('⚠ הקובץ אינו זמין');
    return api;
  }

  window.PdfViewer = { mount: mount };
})();
