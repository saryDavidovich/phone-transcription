/*
 * מחליף קול לקריינות - בלי בינה מלאכותית. עיבוד קבוע בדפדפן (Web Audio), אותה תוצאה בכל פעם:
 *   ניקוי (סינון זמזום) -> הורדת גובה הקול (pitch) -> שער רעשים -> אקולייזר "קריין" (חום, בהירות, נוכחות)
 *   -> קומפרסור (עוצמה אחידה) -> נרמול עוצמה.
 * מקליטים את הקול המקורי, והעיבוד נעשה עליו במחשב תוך שנייה - כך אפשר להחליף סגנון על אותה הקלטה
 * ולהשוות. אפשר גם לשמוע את הקול המעובד בזמן אמת באוזניות בזמן ההקלטה.
 *
 * שימוש:  VoiceFX.mount(element, { onFile: function (file) { ... } })   - file = WAV מוכן לשליחה (או null)
 */
(function () {
  if (window.VoiceFX) return;

  var PRESETS = {
    deep:     { label: 'קריין עמוק',        ratio: 0.87, low: 5, mud: -3, presence: 3.5, air: 2.5, gate: 0.012 },
    narrator: { label: 'קריין',             ratio: 0.93, low: 4, mud: -3, presence: 3,   air: 2,   gate: 0.012 },
    clean:    { label: 'ניקוי ושיפור בלבד', ratio: 1,    low: 2, mud: -2, presence: 2,   air: 1.5, gate: 0.010 },
    none:     { label: 'בלי עיבוד (מקורי)', ratio: 1,    none: true }
  };
  var STORE = 'voicefxPreset';

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

  // שרשרת העיבוד - אותה שרשרת בזמן אמת ובעיבוד הסופי
  function buildChain(ctx, src, p) {
    if (p.none) { var pass = ctx.createGain(); src.connect(pass); return pass; }
    var hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 85; hp.Q.value = 0.7;
    var sh = new AudioWorkletNode(ctx, 'vfx-shift', { outputChannelCount: [1] });
    sh.parameters.get('ratio').value = p.ratio; sh.parameters.get('gate').value = p.gate;
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

  // עיבוד סופי של ההקלטה (מהיר מזמן אמת) + נרמול ל- -1dB
  function render(raw, p) {
    var rate = 24000, len = Math.ceil(raw.duration * rate) + rate / 4;
    var ctx = new OfflineAudioContext(1, len, rate);
    return addWorklet(ctx).then(function () {
      var s = ctx.createBufferSource(); s.buffer = raw;
      buildChain(ctx, s, p).connect(ctx.destination);
      s.start();
      return ctx.startRendering();
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
    var preset = 'narrator';
    try { preset = localStorage.getItem(STORE) || 'narrator'; } catch (e) {}
    if (!PRESETS[preset]) preset = 'narrator';
    root.innerHTML =
      '<div class="vfx" style="display:flex;flex-direction:column;gap:10px">' +
      '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">' +
        '<label style="font-size:13px">קול: <select class="form-input vfx-preset" style="width:auto;display:inline-block;padding:4px 8px">' +
          Object.keys(PRESETS).map(function (k) { return '<option value="' + k + '"' + (k === preset ? ' selected' : '') + '>' + PRESETS[k].label + '</option>'; }).join('') +
        '</select></label>' +
        '<label style="font-size:13px;cursor:pointer" title="שומעים את הקול המעובד תוך כדי הקלטה. רק עם אוזניות - אחרת נוצרת שריקה"><input type="checkbox" class="vfx-monitor"> 🎧 לשמוע באוזניות תוך כדי</label>' +
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
      '</div>';
    var $ = function (s) { return root.querySelector(s); };
    var sel = $('.vfx-preset'), btn = $('.vfx-rec'), timeEl = $('.vfx-time'), meter = $('.vfx-meter'), bar = meter.firstChild;
    var raw = null, rawUrl = null, live = null, origAudio = new Audio();

    function process() {
      if (!raw) return;
      var p = PRESETS[sel.value];
      $('.vfx-status').textContent = 'מעבד...';
      render(raw, p).then(function (buf) {
        var wav = toWav(buf), pl = $('.vfx-play');
        if (pl.src) URL.revokeObjectURL(pl.src);
        pl.src = URL.createObjectURL(wav);
        $('.vfx-result').style.display = 'flex';
        $('.vfx-status').textContent = '✔ מוכן (' + p.label + ', ' + fmt(raw.duration) + ') - אפשר להחליף קול למעלה ולהשוות';
        if (opts.onFile) opts.onFile(new File([wav], 'recording.wav', { type: 'audio/wav' }));
      }).catch(function (e) {
        $('.vfx-status').textContent = '⚠ העיבוד נכשל: ' + e.message;
        if (opts.onFile) opts.onFile(null);
      });
    }

    sel.addEventListener('change', function () {
      try { localStorage.setItem(STORE, sel.value); } catch (e) {}
      if (live && live.out) {            // תוך כדי הקלטה - מחליפים את השרשרת של ההאזנה
        try { live.out.disconnect(); } catch (e) {}
        live.out = buildChain(live.ctx, live.src, PRESETS[sel.value]);
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
            live.out = buildChain(ctx, src, PRESETS[sel.value]);
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

  window.VoiceFX = { mount: mount, presets: PRESETS };
})();
