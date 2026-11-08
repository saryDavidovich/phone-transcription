/* מקלדות בטלפון / טאבלט (במגע):
   - שדות מייל: מקלדת מייל (@ ונקודה), בלי אות גדולה אוטומטית ובלי תיקון אוטומטי, כתיבה משמאל לימין.
   - שדות שמסומנים data-kb="num" (חיפוש לקוח): מקלדת מספרים.
   במחשב עם מקלדת רגילה אין לזה שום השפעה. */
(function () {
  var EMAIL_SEL = 'input[type=email], input[name*="email" i], input[id*="email" i]';
  function apply(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(EMAIL_SEL).forEach(function (el) {
      if (el.type === 'hidden' || el.dataset.kbDone) return;
      el.dataset.kbDone = '1';
      el.setAttribute('inputmode', 'email');
      el.setAttribute('autocapitalize', 'off');
      el.setAttribute('autocorrect', 'off');
      el.setAttribute('spellcheck', 'false');
      el.setAttribute('lang', 'en');
      el.setAttribute('dir', 'ltr');
      if (!el.getAttribute('autocomplete')) el.setAttribute('autocomplete', 'email');
      // מחשב מגע עם Windows: מקלדת המגע בוחרת פריסה לפי סוג השדה (type) ולא לפי inputmode.
      // שדה טקסט הופך לשדה מייל - בלי לחסום שמירה של ערך ישן לא תקין (noValidate על הטופס).
      if (el.type === 'text' || !el.getAttribute('type')) {
        try { el.type = 'email'; el.multiple = true; if (el.form) el.form.noValidate = true; } catch (e) {}
      }
    });
    root.querySelectorAll('input[data-kb="num"]').forEach(function (el) {
      el.setAttribute('inputmode', 'numeric');
      // גם כאן - ב-Windows מקלדת המספרים נפתחת לפי type=tel (אפשר עדיין להקליד שם במקלדת רגילה)
      if (el.type !== 'tel') { try { el.type = 'tel'; } catch (e) {} }
    });
  }
  function run() { apply(document); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
  document.addEventListener('turbo:load', run);
  document.addEventListener('turbo:frame-load', run);
  try {
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1) { apply(n.parentNode || n); } }); });
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
})();
