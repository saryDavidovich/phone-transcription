/*
 * מחליף קול לקריינות - בלי בינה מלאכותית. עיבוד קבוע בדפדפן, אותה תוצאה בכל פעם.
 * כדי שלא יזהו את הקול: מפרידים את הקול לשני חלקים ומשנים כל אחד בנפרד (כמו מחליפי קול גבר<->אישה):
 *   1. גובה הקול (pitch) - מיתרי הקול.
 *   2. "גוון" / גודל הגרון (פורמנטים) - מה שבעיקר מזהה אדם. גרון גדול = קול של גבר גדול יותר, וכו'.
 * שיטה: וֹוקוֹדר LPC - מנתחים כל 5 מ"ש את מסנן הגרון, מוציאים ממנו את "צליל המיתרים" (residual), מזיזים
 * את הגובה של צליל המיתרים, מותחים/מכווצים את מסנן הגרון, ומרכיבים מחדש. אחר כך: סינון זמזום,
 * שער רעשים, אקולייזר קריין, קומפרסור ונרמול עוצמה.
 * מקליטים את הקול המקורי, והעיבוד נעשה עליו במחשב תוך שנייה - כך אפשר להחליף סגנון על אותה הקלטה
 * ולהשוות. אפשר גם לשמוע את הקול המעובד בזמן אמת באוזניות בזמן ההקלטה.
 *
 * שימוש:  VoiceFX.mount(element, { onFile: function (file) { ... } })   - file = WAV מוכן לשליחה (או null)
 */
(function () {
  if (window.VoiceFX) return;

  // pitch = גובה הקול (1 = כמו המקור), formant = גודל הגרון (פחות מ-1 = גרון גדול יותר / קול "גדול")
  var EQ = { low: 4, mud: -3, presence: 3, air: 2, gate: 0.012 };
  var PRESETS = {
    man_deep:  { label: 'גבר אחר - קריין עמוק', pitch: 0.86, formant: 0.86 },
    man_mid:   { label: 'גבר אחר - קריין',      pitch: 0.95, formant: 0.88 },
    man_young: { label: 'גבר אחר - צעיר',       pitch: 1.10, formant: 1.07 },
    woman:     { label: 'קול אישה',              pitch: 1.70, formant: 1.18 },
    clean:     { label: 'רק ניקוי ושיפור (הקול שלי)', pitch: 1, formant: 1, low: 2, mud: -2, presence: 2, air: 1.5, gate: 0.010 },
    none:      { label: 'בלי עיבוד (מקורי)',     pitch: 1, formant: 1, none: true },
    custom:    { label: 'מותאם אישית',            pitch: 0.92, formant: 0.9 }
  };
  Object.keys(PRESETS).forEach(function (k) { var p = PRESETS[k]; for (var e in EQ) if (p[e] === undefined) p[e] = EQ[e]; p.ratio = 1; });
  var STORE = 'voicefxPreset2', STORE_CUSTOM = 'voicefxCustom';

  // מעבד הזזת גובה (granular, שני חלונות חופפים) + שער רעשים עדין - רץ בתוך AudioWorklet
  var WORKLET = [
    "class VfxShift extends AudioWorkletProcessor {",
    "  static get parameterDescriptors() { return [{ name: 'ratio', defaultValue: 1 }, { name: 'gate', defaultValue: 0 }]; }",
    "  constructor() { super(); this.W = Math.round(sampleRate * 0.045); this.N = this.W * 4; this.buf = new Float32Array(this.N);",
    "    this.w = 0; this.ph = 0; this.env = 0; this.g = 1; }",
    "  read(d) { var p = this.w - d - 1; while (p < 0) p += this.N; var i = Math.floor(p), f = p - i, a = this.buf[i % this.N], b = this.buf[(i + 1) % this.N]; return a + (b - a) * f; }",
    "  process(inputs, outputs, params) {",
    "    var inp = inputs[0] && inputs[0][0], out = outputs[0][0]; if (!out) return true;",
    "    var ratio = params.ratio[0], gate = params.gate[0], W = this.W;",
    "    var atk = Math.exp(-1 / (sampleRate * 0.005)), rel = Math.exp(-1 / (sampleRate * 0.12));",
    "    for (var i = 0; i < out.length; i++) {",
    "      var x = inp ? inp[i] : 0, ax = Math.abs(x);",
    "      this.env = ax > this.env ? atk * this.env + (1 - atk) * ax : rel * this.env + (1 - rel) * ax;",
    "      var tg = gate > 0 && this.env < gate ? 0.15 : 1;",
    "      this.g += (tg - this.g) * (tg < this.g ? 0.0008 : 0.01);",
    "      this.buf[this.w] = x * this.g;",
    "      var y;",
    "      if (ratio === 1) { y = this.buf[this.w]; }",
    "      else {",
    "        this.ph += (1 - ratio) / W; if (this.ph >= 1) this.ph -= 1; if (this.ph < 0) this.ph += 1;",
    "        var p2 = (this.ph + 0.5) % 1;",
    "        y = Math.sin(Math.PI * this.ph) * this.read(this.ph * W) + Math.sin(Math.PI * p2) * this.read(p2 * W);",
    "      }",
    "      this.w = (this.w + 1) % this.N;",
    "      for (var c = 0; c < outputs[0].length; c++) outputs[0][c][i] = y;",
    "    }",
    "    return true;",
    "  }",
    "}",
    "registerProcessor('vfx-shift', VfxShift);"
  ].join('\n');
  var workletUrl = null;
  function addWorklet(ctx) {
    if (!workletUrl) workletUrl = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
    return ctx.audioWorklet.addModule(workletUrl);
  }

  // ---------------------------------------------------------------------------
  // וֹוקוֹדר LPC: גובה וגוון בנפרד (על מערך דגימות, sr)
  // ---------------------------------------------------------------------------
  function levinson(r, order) {          // -> {k: מקדמי החזרה, a: מקדמי המסנן, e: שגיאת חיזוי}
    var a = new Float64Array(order + 1), k = new Float64Array(order + 1), tmp = new Float64Array(order + 1), e = r[0];
    a[0] = 1;
    for (var m = 1; m <= order; m++) {
      var acc = r[m];
      for (var i = 1; i < m; i++) acc += a[i] * r[m - i];
      var km = e > 1e-12 ? -acc / e : 0;
      if (km > 0.995) km = 0.995; else if (km < -0.995) km = -0.995;
      k[m] = km;
      for (i = 1; i < m; i++) tmp[i] = a[i] + km * a[m - i];
      for (i = 1; i < m; i++) a[i] = tmp[i];
      a[m] = km;
      e *= (1 - km * km);
    }
    return { k: k, a: a, e: e };
  }

  function granularShift(x, sr, ratio) {   // הזזת גובה בזמן (שני חלונות חופפים) - על צליל המיתרים
    if (ratio === 1) return x;
    var W = Math.round(sr * 0.03), N = x.length, y = new Float32Array(N), ph = 0;
    function rd(pos) { if (pos < 0) return 0; var i = Math.floor(pos), f = pos - i; return i + 1 < N ? x[i] + (x[i + 1] - x[i]) * f : 0; }
    for (var n = 0; n < N; n++) {
      ph += (1 - ratio) / W; if (ph >= 1) ph -= 1; if (ph < 0) ph += 1;
      var p2 = (ph + 0.5) % 1;
      y[n] = Math.sin(Math.PI * ph) * rd(n - ph * W) + Math.sin(Math.PI * p2) * rd(n - p2 * W);
    }
    return y;
  }

  function vocoder(x, sr, pitch, formant) {
    if (pitch === 1 && formant === 1) return x;
    var N = x.length, order = Math.min(30, Math.round(sr / 1000) + 4);
    var hop = Math.round(sr * 0.005), win = Math.round(sr * 0.032), K = 256;
    var pre = new Float32Array(N);                         // הדגשת גבוהים (סטנדרטי ב-LPC)
    pre[0] = x[0]; for (var n = 1; n < N; n++) pre[n] = x[n] - 0.94 * x[n - 1];
    var hann = new Float32Array(win); for (n = 0; n < win; n++) hann[n] = 0.5 - 0.5 * Math.cos(2 * Math.PI * n / (win - 1));
    var cosT = new Float32Array((order + 1) * K);           // טבלת קוסינוסים לחישוב ספקטרום/אוטוקורלציה
    for (var m = 0; m <= order; m++) for (var j = 0; j < K; j++) cosT[m * K + j] = Math.cos(m * Math.PI * (j + 0.5) / K);
    var nh = Math.ceil(N / hop), kA = [], kS = [], r = new Float64Array(order + 1), frame = new Float32Array(win);
    var P = new Float64Array(K), Pw = new Float64Array(K), rw = new Float64Array(order + 1);
    for (var h = 0; h < nh; h++) {
      var c = h * hop + (hop >> 1) - (win >> 1);
      for (n = 0; n < win; n++) { var idx = c + n; frame[n] = (idx >= 0 && idx < N ? pre[idx] : 0) * hann[n]; }
      for (m = 0; m <= order; m++) { var acc = 0; for (n = m; n < win; n++) acc += frame[n] * frame[n - m]; r[m] = acc; }
      r[0] = r[0] * 1.0001 + 1e-9;
      for (m = 1; m <= order; m++) r[m] *= Math.exp(-0.5 * Math.pow(2 * Math.PI * 60 * m / sr, 2));   // החלקה קלה
      var L = levinson(r, order);
      kA.push(L.k);
      // ספקטרום המעטפת (מסנן הגרון) -> מתיחה/כיווץ בתדר -> חזרה למקדמים
      for (j = 0; j < K; j++) {
        var re = 0, im = 0;
        for (m = 0; m <= order; m++) { var w = m * Math.PI * (j + 0.5) / K; re += L.a[m] * Math.cos(w); im -= L.a[m] * Math.sin(w); }
        P[j] = L.e / (re * re + im * im + 1e-12);
      }
      for (j = 0; j < K; j++) {
        var src = (j + 0.5) / formant - 0.5;
        if (src >= K - 1) Pw[j] = P[K - 1] * 0.25;
        else if (src <= 0) Pw[j] = P[0];
        else { var i0 = Math.floor(src), fr = src - i0; Pw[j] = P[i0] * (1 - fr) + P[i0 + 1] * fr; }
      }
      for (m = 0; m <= order; m++) { acc = 0; for (j = 0; j < K; j++) acc += Pw[j] * cosT[m * K + j]; rw[m] = acc; }
      rw[0] = rw[0] * 1.0001 + 1e-9;
      kS.push(levinson(rw, order).k);
    }
    // צליל המיתרים: מסנן הפוך (lattice) עם המקדמים של כל 5 מ"ש
    var res = new Float32Array(N), bA = new Float64Array(order + 1), bPrev = new Float64Array(order + 1), f;
    for (n = 0; n < N; n++) {
      var kk = kA[Math.min(nh - 1, (n / hop) | 0)];
      f = pre[n]; bA[0] = f;
      for (m = 1; m <= order; m++) { var fn = f + kk[m] * bPrev[m - 1]; bA[m] = bPrev[m - 1] + kk[m] * f; f = fn; }
      res[n] = f;
      var t = bPrev; bPrev = bA; bA = t;
    }
    res = granularShift(res, sr, pitch);
    // הרכבה מחדש עם הגרון החדש (lattice הפוך - יציב גם כשהמקדמים משתנים)
    var out = new Float32Array(N), b = new Float64Array(order + 1);
    for (n = 0; n < N; n++) {
      kk = kS[Math.min(nh - 1, (n / hop) | 0)];
      f = res[n];
      for (m = order; m >= 1; m--) { f = f - kk[m] * b[m - 1]; b[m] = b[m - 1] + kk[m] * f; }
      b[0] = f;
      out[n] = f;
    }
    // התאמת עוצמה לאורך הזמן (שהדיבור יישאר באותה עוצמה) + ביטול הדגשת הגבוהים
    var seg = hop * 4, gPrev = 1;
    for (var s0 = 0; s0 < N; s0 += hop) {
      var e1 = 1e-9, e2 = 1e-9, a0 = Math.max(0, s0 - seg), a1 = Math.min(N, s0 + hop + seg);
      for (n = a0; n < a1; n++) { e1 += pre[n] * pre[n]; e2 += out[n] * out[n]; }
      var g = Math.sqrt(e1 / e2); if (g > 8) g = 8; if (g < 0.05) g = 0.05;
      for (n = s0; n < Math.min(N, s0 + hop); n++) { var tt = (n - s0) / hop; out[n] *= gPrev + (g - gPrev) * tt; }
      gPrev = g;
    }
    var y = new Float32Array(N), prev = 0;
    for (n = 0; n < N; n++) { prev = out[n] + 0.94 * prev; y[n] = prev; }
    return y;
  }

  // שרשרת העיבוד - אותה שרשרת בזמן אמת ובעיבוד הסופי
  function buildChain(ctx, src, p) {
    if (p.none) { var pass = ctx.createGain(); src.connect(pass); return pass; }
    var hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 85; hp.Q.value = 0.7;
    var sh = new AudioWorkletNode(ctx, 'vfx-shift', { outputChannelCount: [1] });
    sh.parameters.get('ratio').value = p.live ? p.pitch : 1; sh.parameters.get('gate').value = p.gate;
    var low = ctx.createBiquadFilter(); low.type = 'lowshelf'; low.frequency.value = 160; low.gain.value = p.low;
    var mud = ctx.createBiquadFilter(); mud.type = 'peaking'; mud.frequency.value = 380; mud.Q.value = 1.1; mud.gain.value = p.mud;
    var pres = ctx.createBiquadFilter(); pres.type = 'peaking'; pres.frequency.value = 3200; pres.Q.value = 0.8; pres.gain.value = p.presence;
    var air = ctx.createBiquadFilter(); air.type = 'highshelf'; air.frequency.value = 9000; air.gain.value = p.air;
    var lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 12000;
    var comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -26; comp.knee.value = 8; comp.ratio.value = 4; comp.attack.value = 0.006; comp.release.value = 0.18;
    var makeup = ctx.createGain(); makeup.gain.value = 1.8;
    src.connect(hp); hp.connect(sh); sh.connect(low); low.connect(mud); mud.connect(pres); pres.connect(air);
    air.connect(lp); lp.connect(comp); comp.connect(makeup);
    return makeup;
  }

  // עיבוד סופי של ההקלטה: המרה ל-24kHz -> וֹוקוֹדר (גובה + גוון) -> ניקוי/אקולייזר/קומפרסור -> נרמול ל- -1dB
  function render(raw, p) {
    var rate = 24000, len = Math.ceil(raw.duration * rate) + rate / 4;
    var c1 = new OfflineAudioContext(1, len, rate), s1 = c1.createBufferSource();
    s1.buffer = raw; s1.connect(c1.destination); s1.start();
    return c1.startRendering().then(function (mono) {
      var src = mono;
      if (!p.none && (p.pitch !== 1 || p.formant !== 1)) {
        var y = vocoder(mono.getChannelData(0), rate, p.pitch, p.formant);
        src = new AudioBuffer({ length: y.length, numberOfChannels: 1, sampleRate: rate });
        src.copyToChannel(y, 0);
      }
      var ctx = new OfflineAudioContext(1, len, rate);
      return addWorklet(ctx).then(function () {
        var s = ctx.createBufferSource(); s.buffer = src;
        buildChain(ctx, s, p).connect(ctx.destination);
        s.start();
        return ctx.startRendering();
      });
    }).then(function (buf) {
      var d = buf.getChannelData(0), peak = 0;
      for (var i = 0; i < d.length; i++) { var a = Math.abs(d[i]); if (a > peak) peak = a; }
      var g = peak > 0 ? 0.89 / peak : 1;
      for (i = 0; i < d.length; i++) d[i] *= g;
      return buf;
    });
  }

  function toWav(buf) {
    var d = buf.getChannelData(0), rate = buf.sampleRate, n = d.length;
    var out = new DataView(new ArrayBuffer(44 + n * 2));
    function str(o, s) { for (var i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); }
    str(0, 'RIFF'); out.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
    out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
    str(36, 'data'); out.setUint32(40, n * 2, true);
    for (var i = 0; i < n; i++) { var v = Math.max(-1, Math.min(1, d[i])); out.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true); }
    return new Blob([out.buffer], { type: 'audio/wav' });
  }

  function fmt(s) { s = Math.floor(s); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }

  function mount(root, opts) {
    opts = opts || {};
    var preset = 'man_mid';
    try {
      preset = localStorage.getItem(STORE) || 'man_mid';
      var cu = JSON.parse(localStorage.getItem(STORE_CUSTOM) || 'null');
      if (cu && cu.pitch && cu.formant) { PRESETS.custom.pitch = cu.pitch; PRESETS.custom.formant = cu.formant; }
    } catch (e) {}
    if (!PRESETS[preset]) preset = 'man_mid';
    root.innerHTML =
      '<div class="vfx" style="display:flex;flex-direction:column;gap:10px">' +
      '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">' +
        '<label style="font-size:13px">קול: <select class="form-input vfx-preset" style="width:auto;display:inline-block;padding:4px 8px">' +
          Object.keys(PRESETS).map(function (k) { return '<option value="' + k + '"' + (k === preset ? ' selected' : '') + '>' + PRESETS[k].label + '</option>'; }).join('') +
        '</select></label>' +
        '<label style="font-size:13px;cursor:pointer" title="בזמן ההקלטה שומעים רק את שינוי הגובה; שינוי הגרון נעשה בסוף. רק עם אוזניות - אחרת נוצרת שריקה"><input type="checkbox" class="vfx-monitor"> 🎧 לשמוע באוזניות תוך כדי</label>' +
      '</div>' +
      '<div class="vfx-tune" style="display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:#8892b0">' +
        '<label style="display:flex;align-items:center;gap:6px">גובה הקול <input type="range" dir="ltr" class="vfx-pitch" min="-8" max="10" step="0.5" style="width:120px"> <b class="vfx-pitch-v" style="min-width:52px"></b></label>' +
        '<label style="display:flex;align-items:center;gap:6px" title="הכי חשוב כדי שלא יזהו: גרון גדול יותר = נשמע כמו אדם אחר, גדול יותר">גודל הגרון <input type="range" dir="ltr" class="vfx-formant" min="0.77" max="1.33" step="0.01" style="width:120px"> <b class="vfx-formant-v" style="min-width:64px"></b></label>' +
      '</div>' +
      '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">' +
        '<button type="button" class="btn btn-danger vfx-rec">⏺ התחל הקלטה</button>' +
        '<span class="vfx-time" style="font-variant-numeric:tabular-nums;color:#8892b0;min-width:60px"></span>' +
        '<span class="vfx-meter" style="display:none;width:120px;height:8px;border-radius:4px;background:rgba(127,127,127,.25);overflow:hidden"><span style="display:block;height:100%;width:0;background:#22c55e;transition:width .06s"></span></span>' +
      '</div>' +
      '<div class="vfx-result" style="display:none;gap:10px;align-items:center;flex-wrap:wrap">' +
        '<audio class="vfx-play" controls style="height:34px"></audio>' +
        '<button type="button" class="btn btn-ghost vfx-orig" style="font-size:12px">▶ המקורי להשוואה</button>' +
        '<span class="vfx-status" style="font-size:12px;color:#8892b0"></span>' +
      '</div>' +
      (opts.narrator ? '<div class="vfx-nr" style="display:none;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 12px;border:1px dashed rgba(127,127,127,.4);border-radius:10px">' +
        '<button type="button" class="btn btn-primary vfx-nr-btn">🗣️ להפוך לקול הקריין</button>' +
        '<span style="font-size:12px;color:#8892b0">קול אחר לגמרי, בלי שום קשר לקול שלך: המערכת מתמללת מה אמרת, והקריין המקצועי מקריא את זה.</span>' +
        '<span class="vfx-nr-status" style="font-size:12px"></span></div>' : '') +
      '</div>';
    var $ = function (s) { return root.querySelector(s); };
    var sel = $('.vfx-preset'), btn = $('.vfx-rec'), timeEl = $('.vfx-time'), meter = $('.vfx-meter'), bar = meter.firstChild;
    var raw = null, rawUrl = null, live = null, origAudio = new Audio();
    var pitchEl = $('.vfx-pitch'), formEl = $('.vfx-formant'), debounce = null;
    function semis(r) { return Math.round(12 * Math.log(r) / Math.LN2 * 2) / 2; }
    function showTune() {
      var p = PRESETS[sel.value], dis = !!p.none;
      pitchEl.value = semis(p.pitch); formEl.value = (1 / p.formant).toFixed(2);   // גודל הגרון = 1 / הזזת הפורמנטים
      pitchEl.disabled = formEl.disabled = dis;
      var st = semis(p.pitch);
      $('.vfx-pitch-v').textContent = st === 0 ? 'כמו שלי' : (st > 0 ? '+' : '') + st + ' טון';
      var fp = Math.round((1 / p.formant - 1) * 100);
      $('.vfx-formant-v').textContent = fp === 0 ? 'כמו שלי' : (fp > 0 ? 'גדול ב-' + fp + '%' : 'קטן ב-' + (-fp) + '%');
    }
    function liveP() { var p = PRESETS[sel.value], o = {}; for (var k in p) o[k] = p[k]; o.live = true; return o; }
    function onTune() {
      PRESETS.custom.pitch = Math.pow(2, parseFloat(pitchEl.value) / 12);
      PRESETS.custom.formant = 1 / parseFloat(formEl.value);
      sel.value = 'custom';
      try { localStorage.setItem(STORE, 'custom'); localStorage.setItem(STORE_CUSTOM, JSON.stringify({ pitch: PRESETS.custom.pitch, formant: PRESETS.custom.formant })); } catch (e) {}
      showTune();
      if (live && live.out) { try { live.out.disconnect(); } catch (e) {} live.out = buildChain(live.ctx, live.src, liveP()); if ($('.vfx-monitor').checked) live.out.connect(live.ctx.destination); }
      clearTimeout(debounce); debounce = setTimeout(process, 450);
    }
    pitchEl.addEventListener('input', onTune); formEl.addEventListener('input', onTune);
    if (opts.narrator) {
      $('.vfx-nr-btn').addEventListener('click', function () {
        if (!raw) return;
        var b = this, st = $('.vfx-nr-status');
        b.disabled = true; st.style.color = '#8892b0'; st.textContent = 'מתמלל את מה שאמרת...';
        render(raw, PRESETS.none).then(function (buf) {
          var fd = new FormData(); fd.append('audio', toWav(buf), 'recording.wav');
          return fetch(opts.narrator.url, { method: 'POST', body: fd }).then(function (r) { return r.json(); });
        }).then(function (d) {
          b.disabled = false;
          if (!d.ok) { st.style.color = '#ef4444'; st.textContent = '⚠ ' + (d.error || 'התמלול נכשל'); return; }
          st.style.color = '#22c55e'; st.textContent = '✔ הנוסח הועבר לקריין - בדוק אותו ולחץ להפקה';
          opts.narrator.onText(d.text);
        }).catch(function (e) { b.disabled = false; st.style.color = '#ef4444'; st.textContent = '⚠ ' + e.message; });
      });
    }
    showTune();

    function process() {
      if (!raw) return;
      var p = PRESETS[sel.value];
      $('.vfx-status').textContent = 'מעבד... (הקלטה ארוכה - כמה שניות)';
      $('.vfx-result').style.display = 'flex';
      render(raw, p).then(function (buf) {
        var wav = toWav(buf), pl = $('.vfx-play');
        if (pl.src) URL.revokeObjectURL(pl.src);
        pl.src = URL.createObjectURL(wav);
        $('.vfx-result').style.display = 'flex';
        if (opts.narrator) $('.vfx-nr').style.display = 'flex';
        $('.vfx-status').textContent = '✔ מוכן (' + p.label + ', ' + fmt(raw.duration) + ') - אפשר להחליף קול למעלה ולהשוות';
        if (opts.onFile) opts.onFile(new File([wav], 'recording.wav', { type: 'audio/wav' }));
      }).catch(function (e) {
        $('.vfx-status').textContent = '⚠ העיבוד נכשל: ' + e.message;
        if (opts.onFile) opts.onFile(null);
      });
    }

    sel.addEventListener('change', function () {
      try { localStorage.setItem(STORE, sel.value); } catch (e) {}
      showTune();
      if (live && live.out) {            // תוך כדי הקלטה - מחליפים את השרשרת של ההאזנה
        try { live.out.disconnect(); } catch (e) {}
        live.out = buildChain(live.ctx, live.src, liveP());
        if ($('.vfx-monitor').checked) live.out.connect(live.ctx.destination);
      }
      process();
    });
    $('.vfx-monitor').addEventListener('change', function () {
      if (!live || !live.out) return;
      try { live.out.disconnect(); } catch (e) {}
      if (this.checked) live.out.connect(live.ctx.destination);
    });
    $('.vfx-orig').addEventListener('click', function () {
      if (!rawUrl) return;
      $('.vfx-play').pause();
      if (!origAudio.paused) { origAudio.pause(); this.textContent = '▶ המקורי להשוואה'; return; }
      origAudio.src = rawUrl; origAudio.currentTime = 0; origAudio.play();
      var b = this; b.textContent = '⏸ עצור את המקורי';
      origAudio.onended = function () { b.textContent = '▶ המקורי להשוואה'; };
    });

    btn.addEventListener('click', function () {
      if (live) { live.rec.stop(); return; }
      if (!navigator.mediaDevices || !window.MediaRecorder || !window.AudioWorkletNode) { alert('הדפדפן לא תומך בהקלטה עם עיבוד קול - מומלץ כרום'); return; }
      navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: false, channelCount: 1 } })
        .then(function (stream) {
          var ctx = new (window.AudioContext || window.webkitAudioContext)();
          return addWorklet(ctx).then(function () {
            var src = ctx.createMediaStreamSource(stream), an = ctx.createAnalyser(); an.fftSize = 1024;
            src.connect(an);
            var chunks = [], rec = new MediaRecorder(stream), t0 = Date.now(), data = new Float32Array(an.fftSize);
            live = { ctx: ctx, src: src, rec: rec, stream: stream };
            live.out = buildChain(ctx, src, liveP());
            if ($('.vfx-monitor').checked) live.out.connect(ctx.destination);
            meter.style.display = 'inline-block';
            var tick = setInterval(function () {
              timeEl.textContent = '🔴 ' + fmt((Date.now() - t0) / 1000);
              an.getFloatTimeDomainData(data);
              var pk = 0; for (var i = 0; i < data.length; i++) pk = Math.max(pk, Math.abs(data[i]));
              bar.style.width = Math.min(100, pk * 140) + '%';
              bar.style.background = pk > 0.9 ? '#ef4444' : (pk > 0.6 ? '#eab308' : '#22c55e');
            }, 80);
            rec.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
            rec.onstop = function () {
              clearInterval(tick); meter.style.display = 'none';
              stream.getTracks().forEach(function (t) { t.stop(); });
              var blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
              live = null;
              btn.textContent = '⏺ הקלטה מחדש';
              timeEl.textContent = '';
              if (rawUrl) URL.revokeObjectURL(rawUrl);
              rawUrl = URL.createObjectURL(blob);
              blob.arrayBuffer().then(function (ab) { return ctx.decodeAudioData(ab); })
                .then(function (b) { raw = b; ctx.close(); process(); })
                .catch(function (e) { ctx.close(); $('.vfx-status').textContent = '⚠ ' + e.message; $('.vfx-result').style.display = 'flex'; });
            };
            rec.start();
            btn.textContent = '⏹ עצור הקלטה';
            if (opts.onFile) opts.onFile(null);
          });
        })
        .catch(function () { alert('אין גישה למיקרופון - יש לאשר בדפדפן'); });
    });
  }

  window.VoiceFX = { mount: mount, presets: PRESETS, _vocoder: vocoder };
})();
