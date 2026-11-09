/* מקלדת על המסך של האתר - למחשב מגע / טאבלט / טלפון.
   נפתחת רק כשנוגעים בשדה באצבע (לא בעכבר), ובמקומה המקלדת של המערכת (Windows) לא נפתחת.
   - שדה חיפוש לקוח (data-kb="num"): לוח מספרים.
   - שדות מייל (בכל המערכת): מקלדת מייל באנגלית עם שורת מספרים, @ ונקודה.
   בכל מקלדת יש כפתור "מקלדת רגילה" - חזרה למקלדת של המכשיר לשדה הזה. */
(function () {
  if (window.__siteKb) return;
  window.__siteKb = true;
  var EMAIL_SEL = 'input[type=email], input[name*="email" i], input[id*="email" i]';

  // ---------- הגדרות שדות (גם בלי מגע: מקלדת מייל/מספרים בטלפון, אם המקלדת שלנו כבויה) ----------
  function prep(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(EMAIL_SEL).forEach(function (el) {
      if (el.type === 'hidden' || el.dataset.kbPrep) return;
      el.dataset.kbPrep = '1';
      el.dataset.kbKind = 'email';
      el.setAttribute('autocapitalize', 'off');
      el.setAttribute('autocorrect', 'off');
      el.setAttribute('spellcheck', 'false');
      el.setAttribute('dir', 'ltr');
      if (!el.getAttribute('autocomplete')) el.setAttribute('autocomplete', 'email');
    });
    root.querySelectorAll('input[data-kb="num"]').forEach(function (el) {
      if (el.dataset.kbPrep) return;
      el.dataset.kbPrep = '1';
      el.dataset.kbKind = 'num';
    });
  }

  // ---------- המקלדת ----------
  var css = '' +
    '#siteKb{position:fixed;left:0;right:0;bottom:0;z-index:2147483000;display:none;direction:ltr;' +
    'background:#1f2433;border-top:1px solid #39405a;box-shadow:0 -6px 24px rgba(0,0,0,.35);padding:8px 6px 10px;' +
    'font-family:Arial,sans-serif;user-select:none;-webkit-user-select:none;touch-action:manipulation}' +
    '#siteKb .kb-in{max-width:560px;margin:0 auto}' +
    '#siteKb.num .kb-in{max-width:340px}' +
    '#siteKb .kb-row{display:flex;gap:5px;margin-bottom:6px}' +
    '#siteKb button{flex:1;min-width:0;border:0;border-radius:7px;background:#3a4157;color:#fff;font-size:19px;' +
    'padding:12px 0;cursor:pointer;font-family:inherit;box-shadow:0 2px 0 #12151f}' +
    '#siteKb button:active{background:#5b6b9a;transform:translateY(1px)}' +
    '#siteKb button.s{background:#2b3144;font-size:14px}' +
    '#siteKb button.go{background:#4f6bed}' +
    '#siteKb button.w2{flex:2}#siteKb button.w3{flex:3}' +
    '#siteKb.num button{font-size:24px;padding:15px 0}' +
    '#siteKb.num button.s{font-size:15px}' +
    '#siteKb .kb-top{display:flex;justify-content:space-between;align-items:center;color:#9aa3bd;font-size:12px;margin:0 2px 6px;direction:rtl}' +
    '#siteKb .kb-top a{color:#9fb2ff;cursor:pointer;text-decoration:underline}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var kb = document.createElement('div'); kb.id = 'siteKb';
  var target = null, shift = false, kind = null;

  var NUM = [['1', '2', '3'], ['4', '5', '6'], ['7', '8', '9'], ['{clr}', '0', '{bs}'], ['{native}', '{go}']];
  var EMAIL = [
    ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
    ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
    ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', '{bs}'],
    ['{shift}', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '-', '_'],
    ['{native}', '@', '.', '{gmail}', '{com}', '{go}']];
  var LABEL = { '{bs}': '⌫', '{clr}': 'נקה', '{go}': 'אישור', '{shift}': '⇧', '{native}': 'מקלדת רגילה',
                '{gmail}': '@gmail.com', '{com}': '.com' };
  var CLS = { '{bs}': 's', '{clr}': 's', '{go}': 'go w2', '{shift}': 's', '{native}': 's w2', '{gmail}': 's w3', '{com}': 's w2' };

  function render() {
    var rows = kind === 'num' ? NUM : EMAIL;
    var h = '<div class="kb-in"><div class="kb-top"><span>' + (kind === 'num' ? 'לוח מספרים' : 'מקלדת מייל') +
            '</span><a data-k="{close}">סגירה ✕</a></div>';
    rows.forEach(function (r) {
      h += '<div class="kb-row">';
      r.forEach(function (k) {
        var lab = LABEL[k] || (shift && k.length === 1 ? k.toUpperCase() : k);
        h += '<button type="button" tabindex="-1" class="' + (CLS[k] || '') + '" data-k="' + k + '">' + lab + '</button>';
      });
      h += '</div>';
    });
    kb.innerHTML = h + '</div>';
    kb.className = kind === 'num' ? 'num' : '';
  }

  function fire(el) {
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function insert(txt) {
    var el = target; if (!el) return;
    var v = el.value, a, b;
    try { a = el.selectionStart; b = el.selectionEnd; } catch (e) { a = b = null; }
    if (a === null || a === undefined) { a = b = v.length; }   // type=email לא תומך בסמן - מוסיפים בסוף
    el.value = v.slice(0, a) + txt + v.slice(b);
    try { el.setSelectionRange(a + txt.length, a + txt.length); } catch (e) {}
    fire(el);
  }
  function backspace() {
    var el = target; if (!el) return;
    var v = el.value, a, b;
    try { a = el.selectionStart; b = el.selectionEnd; } catch (e) { a = b = null; }
    if (a === null || a === undefined) { el.value = v.slice(0, -1); fire(el); return; }
    if (a !== b) { el.value = v.slice(0, a) + v.slice(b); a = a; }
    else if (a > 0) { el.value = v.slice(0, a - 1) + v.slice(a); a = a - 1; }
    try { el.setSelectionRange(a, a); } catch (e) {}
    fire(el);
  }
  function hide() {
    kb.style.display = 'none';
    document.body.style.paddingBottom = '';
    if (target) target.setAttribute('inputmode', target.dataset.kbOldMode || (target.dataset.kbKind === 'num' ? 'numeric' : 'email'));
    target = null;
  }
  function show(el) {
    target = el; kind = el.dataset.kbKind; shift = false;
    if (!el.dataset.kbOldMode) el.dataset.kbOldMode = el.getAttribute('inputmode') || '';
    el.setAttribute('inputmode', 'none');           // שהמקלדת של Windows / המכשיר לא תיפתח
    render();
    if (!kb.parentNode) document.body.appendChild(kb);
    kb.style.display = 'block';
    document.body.style.paddingBottom = kb.offsetHeight + 'px';
    setTimeout(function () {
      var r = el.getBoundingClientRect();
      if (r.bottom > window.innerHeight - kb.offsetHeight - 10) el.scrollIntoView({ block: 'center' });
    }, 30);
  }

  kb.addEventListener('pointerdown', function (e) {
    e.preventDefault();                              // השדה נשאר בפוקוס
    var t = e.target.closest('[data-k]'); if (!t || !target) return;
    var k = t.getAttribute('data-k'), el = target;
    if (k === '{close}') { hide(); el.blur(); return; }
    if (k === '{bs}') return backspace();
    if (k === '{clr}') { el.value = ''; fire(el); return; }
    if (k === '{shift}') { shift = !shift; render(); return; }
    if (k === '{gmail}') return insert('@gmail.com');
    if (k === '{com}') return insert('.com');
    if (k === '{native}') {                          // חזרה למקלדת של המכשיר לשדה הזה
      el.dataset.kbNative = '1'; hide(); el.blur(); setTimeout(function () { el.focus(); }, 60); return;
    }
    if (k === '{go}') {
      hide();
      if (el.form && (el.dataset.kbKind === 'num' || el.form.method.toLowerCase() === 'get')) {
        if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
      } else { el.blur(); }
      return;
    }
    insert(shift ? k.toUpperCase() : k);
    if (shift) { shift = false; render(); }
  });

  // נגיעה באצבע בשדה -> המקלדת שלנו; עכבר/עט -> כרגיל
  document.addEventListener('pointerdown', function (e) {
    if (e.target.closest && e.target.closest('#siteKb')) return;
    var el = e.target.closest && e.target.closest('input[data-kb-kind]');
    if (el && e.pointerType === 'touch' && !el.disabled && !el.readOnly && !el.dataset.kbNative) {
      el.setAttribute('inputmode', 'none');          // לפני שהפוקוס מגיע - שהמקלדת של Windows לא תקפוץ
      setTimeout(function () { el.focus(); show(el); }, 0);
      return;
    }
    if (target && kb.style.display === 'block' && e.target !== target) hide();
  }, true);
  document.addEventListener('focusout', function (e) {
    if (e.target === target) setTimeout(function () { if (document.activeElement !== target) hide(); }, 120);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && target) hide(); });

  function run() { prep(document); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
  document.addEventListener('turbo:load', run);
  document.addEventListener('turbo:before-render', function () { if (target) hide(); });
  try {
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1 && n.id !== 'siteKb') prep(n.parentNode || n); }); });
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
})();
