/* אזור מוסד - תוכנה מותקנת (PWA). מוגש מ-/institution/sw.js (תחום: /institution/).
 * לא שומר עמודים או נתונים במחשב (מידע של מוסד ותלמידים) - כל הבקשות עוברות ישירות לשרת.
 * בלי חיבור לאינטרנט מוצגת הודעה ברורה במקום שגיאת דפדפן. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.mode !== 'navigate') return;
  e.respondWith(fetch(req).catch(() => new Response(
    '<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>אין חיבור</title><body style="font-family:Heebo,Arial,sans-serif;background:#f3f5fa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">' +
    '<div style="background:#fff;border:1px solid #e3e7f1;border-radius:14px;padding:28px;max-width:360px;text-align:center">' +
    '<h2 style="margin:0 0 10px;font-size:18px">אין חיבור לאינטרנט</h2><p style="color:#64708a;font-size:14px;margin:0 0 16px">בדקו את החיבור ונסו שוב.</p>' +
    '<button onclick="location.reload()" style="background:#10b981;color:#fff;border:0;border-radius:10px;padding:10px 22px;font-size:14px;cursor:pointer">נסו שוב</button></div></body></html>',
    { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});

/* לחיצה על התראה (תמלול מוכן / הודעה מהצוות) - פותחת את אזור המוסד בעמוד המתאים */
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/institution/dashboard';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (c.url.indexOf('/institution/') >= 0 && 'focus' in c) { c.focus(); if ('navigate' in c) c.navigate(url); return; }
    }
    return self.clients.openWindow(url);
  }));
});
