/*
 * כלי כיווץ הקלטות - רץ בדפדפן של המשתמש (ffmpeg.wasm מתוך /static/vendor/ffmpeg), לא בשרת.
 * כל קובץ שמע/וידאו -> MP3 מונו 16kHz 48kbps: שעתיים ~ 43MB, בלי פגיעה בתמלול
 * (השרת ממילא ממיר הכל ל-16kHz מונו). הקובץ המקורי לא נטען כולו לזיכרון (WORKERFS).
 *
 * AudioCompress.supported()           - האם הדפדפן מסוגל
 * AudioCompress.needs(file)           - קובץ שכדאי לכווץ (מעל THRESH_MB)
 * AudioCompress.compress(file, onP, maxBytes) - Promise<File>; onP(0..1, 'second' בכיווץ השני)
 *   קובץ שיוצא גדול מ-maxBytes מכווץ שוב ב-24kbps (עדיין מספיק לדיבור בתמלול)
 */
(function () {
  var BASE = '/static/vendor/ffmpeg/';
  // נטפרי חוסמים קבצי wasm (מחזירים דף HTML במקום). לכן המנוע מגיע גם כ-7 קבצי טקסט (gzip+base64,
  // core-wasm-N.js, ~13MB בסך הכל - פחות מחצי מה-32MB של הקובץ המקורי) ונבנה מחדש כאן בדפדפן.
  var WASM_PARTS = 7;
  var THRESH_MB = 40;
  var ff = null, loading = null;

  function supported() {
    return typeof WebAssembly === 'object' && typeof Worker === 'function' && typeof File === 'function';
  }

  function loadScript(src) {
    return new Promise(function (ok, bad) {
      if (window.FFmpegWASM) return ok();
      var s = document.createElement('script');
      s.src = src; s.onload = function () { ok(); };
      s.onerror = function () { bad(new Error('טעינת כלי הכיווץ נכשלה')); };
      document.head.appendChild(s);
    });
  }

  function isWasm(buf) {
    var b = new Uint8Array(buf, 0, 4);
    return b.length === 4 && b[0] === 0 && b[1] === 0x61 && b[2] === 0x73 && b[3] === 0x6d;
  }

  // כתובת blob: לקובץ המנוע (application/wasm), שנבנה מקבצי הטקסט; אם אי אפשר - הכתובת הרגילה
  async function wasmURL() {
    if (typeof DecompressionStream === 'function' && typeof Blob === 'function' && window.URL && URL.createObjectURL) {
      try {
        var idx = []; for (var i = 0; i < WASM_PARTS; i++) idx.push(i);
        var texts = await Promise.all(idx.map(function (i) {
          return fetch(BASE + 'core-wasm-' + i + '.js', { credentials: 'same-origin' }).then(function (r) {
            if (!r.ok) throw new Error('חלק ' + i + ': ' + r.status);
            return r.text();
          });
        }));
        var b64 = texts.join('').replace(/\s+/g, '');
        if (!/^[A-Za-z0-9+/=]+$/.test(b64.slice(0, 200))) throw new Error('חלקי המנוע הגיעו משובשים');
        var bin = atob(b64), gz = new Uint8Array(bin.length);
        for (var k = 0; k < bin.length; k++) gz[k] = bin.charCodeAt(k);
        var wasm = await new Response(new Blob([gz]).stream().pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
        if (!isWasm(wasm)) throw new Error('המנוע לא נבנה כראוי');
        return URL.createObjectURL(new Blob([wasm], { type: 'application/wasm' }));
      } catch (e) {
        try { console.warn('[compress] text parts failed, trying the .wasm file', e); } catch (x) {}
      }
    }
    return BASE + 'ffmpeg-core.wasm';
  }

  function get() {
    if (ff) return Promise.resolve(ff);
    if (!loading) {
      loading = loadScript(BASE + 'ffmpeg.js').then(wasmURL).then(function (wurl) {
        var f = new window.FFmpegWASM.FFmpeg();
        return f.load({ coreURL: BASE + 'ffmpeg-core.js', wasmURL: wurl }).then(function () {
          ff = f; return f;
        });
      }).catch(function (e) { loading = null; throw (e && e.stage) ? e : why('load', e); });
    }
    return loading;
  }

  function secs(h, m, s) { return (+h) * 3600 + (+m) * 60 + parseFloat(s); }

  // שגיאה ברורה עם השלב שבו נכשל (טעינה / פתיחת הקובץ / כיווץ) - כדי שאפשר יהיה להבין מה קרה
  function why(stage, e, tail) {
    var m = '';
    if (e && typeof e === 'object') m = e.message || (e.type ? 'אירוע ' + e.type : '') || (e.target && e.target.src ? 'נחסם ' + e.target.src : '');
    else if (e) m = String(e);
    var label = { load: 'טעינת כלי הכיווץ נכשלה (ייתכן שהסינון חוסם את קבצי הכלי)',
                  mount: 'פתיחת הקובץ בכלי הכיווץ נכשלה',
                  exec: 'הכיווץ נכשל' }[stage] || 'הכיווץ נכשל';
    var err = new Error(label + (m ? ' [' + m + ']' : '') + (tail ? ' · ' + tail : ''));
    err.stage = stage;
    try { console.error('[compress]', stage, e, tail || ''); } catch (x) {}
    return err;
  }

  // כיווץ, ואם הקובץ עדיין גדול מ-maxBytes (הקלטה של יותר מ-~7 שעות) - שוב, חזק יותר (24kbps, ~14 שעות)
  async function compress(file, onProgress, maxBytes) {
    var out = await encode(file, '48k', onProgress);
    if (maxBytes && out.size > maxBytes) {
      out = await encode(file, '24k', onProgress ? function (p) { onProgress(p, 'second'); } : null);
    }
    return out;
  }

  async function encode(file, bitrate, onProgress) {
    var f = await get();
    var ext = (file.name.match(/\.[A-Za-z0-9]{1,5}$/) || ['.bin'])[0].toLowerCase();
    var inName = 'input' + ext, dir = '/in' + Date.now();
    var total = 0;
    var onLog = function (e) {                     // ההתקדמות לפי "Duration" ו-"time=" בלוג של ffmpeg
      var msg = (e && e.message) || '', m;
      if (!total && (m = msg.match(/Duration:\s*(\d+):(\d+):([\d.]+)/))) total = secs(m[1], m[2], m[3]);
      if (total && (m = msg.match(/time=\s*(\d+):(\d+):([\d.]+)/)) && onProgress) {
        onProgress(Math.max(0, Math.min(1, secs(m[1], m[2], m[3]) / total)));
      }
    };
    var tail = [];
    var keepTail = function (e) { var t = (e && e.message) || ''; if (t) { tail.push(t); if (tail.length > 4) tail.shift(); } };
    f.on('log', onLog);
    f.on('log', keepTail);
    try {
      await f.createDir(dir);
      await f.mount('WORKERFS', { files: [new File([file], inName)] }, dir);
    } catch (e) { f.off('log', onLog); f.off('log', keepTail); throw why('mount', e); }
    try {
      var rc;
      try {
        rc = await f.exec(['-hide_banner', '-i', dir + '/' + inName, '-vn', '-ac', '1', '-ar', '16000',
                           '-c:a', 'libmp3lame', '-b:a', bitrate, 'out.mp3']);
      } catch (e) { throw why('exec', e, tail.join(' | ').slice(-300)); }
      if (rc !== 0) throw why('exec', 'קוד ' + rc + ' - ייתכן שהקובץ אינו קובץ שמע/וידאו תקין', tail.join(' | ').slice(-300));
      var data = await f.readFile('out.mp3');
      try { await f.deleteFile('out.mp3'); } catch (x) {}
      if (!data || data.length < 1000) throw new Error('הכיווץ לא הצליח - לא נמצא שמע בקובץ');
      if (onProgress) onProgress(1);
      return new File([data], file.name.replace(/\.[^.]+$/, '') + '.mp3', { type: 'audio/mpeg' });
    } finally {
      f.off('log', onLog);
      f.off('log', keepTail);
      try { await f.unmount(dir); await f.deleteDir(dir); } catch (x) {}
    }
  }

  window.AudioCompress = {
    THRESH_MB: THRESH_MB,
    supported: supported,
    needs: function (file) { return !!file && file.size > THRESH_MB * 1048576; },
    compress: compress,
    preload: function () { if (supported()) get().catch(function () {}); }
  };
})();
