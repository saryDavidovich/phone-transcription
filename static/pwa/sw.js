/* מערכת תמלול - אפליקציה מותקנת (PWA). מוגש מ-/admin/sw.js (תחום: /admin/).
 *
 * עמודי ניהול (HTML): קודם מהשרת, ועותק נשמר במחשב. אין חיבור -> מוצג העותק
 * האחרון, עם פס "אין חיבור - מוצג מידע שמור מ-...". פעולות (שמירה/שליחה) לא
 * מתבצעות בלי חיבור - מוצג הסבר. קבצים קבועים (/static) נשמרים ומתרעננים ברקע.
 * לא נשמרים: הורדות, שמע, נתונים חיים (JSON), התחברות/התנתקות.
 * בהתנתקות מהמערכת כל המידע השמור נמחק (templates/admin/login.html).
 */
const VERSION = 'v1';
const PAGES = 'admin-pages';             // השם קבוע - כדי שההתנתקות תמחק אותו
const STATIC = 'admin-static-' + VERSION;
const MAX_PAGES = 200;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) {
      if (k.startsWith('admin-static-') && k !== STATIC) await caches.delete(k);
    }
    await self.clients.claim();
  })());
});

const NO_CACHE = /\/(login|logout|listen|play|play-audio|download|download-word|download-audio|excel|cost-report|live|live-count|sw\.js|manifest\.webmanifest)(\/|\?|$)|\/dl\/|\.(docx|xlsx|wav|mp3|pdf|zip)(\?|$)/i;

function isPage(req) {
  const a = req.headers.get('accept') || '';
  return req.method === 'GET' && (req.mode === 'navigate' || a.includes('text/html'));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (req.method !== 'GET') {                        // שמירה/שליחה בלי חיבור
    e.respondWith(fetch(req).catch(() => offlineActionPage()));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(staleWhileRevalidate(req));
    return;
  }
  if (url.pathname.startsWith('/admin') && isPage(req) && !NO_CACHE.test(url.pathname)) {
    e.respondWith(networkFirstPage(req));
  }
});

async function networkFirstPage(req) {
  const cache = await caches.open(PAGES);
  const key = cacheKey(req);
  try {
    const res = await fetch(req);
    const type = res.headers.get('content-type') || '';
    if (res.ok && !res.redirected && type.includes('text/html')) {
      const copy = res.clone();
      const body = await copy.blob();
      const headers = new Headers(copy.headers);
      headers.set('x-sw-saved-at', String(Date.now()));
      await cache.put(key, new Response(body, { status: 200, headers }));
      trim(cache);
    }
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return withOfflineBanner(hit);
    return offlinePage('העמוד הזה עוד לא נשמר במחשב - הוא יהיה זמין בלי חיבור אחרי שתפתח אותו פעם אחת עם חיבור.');
  }
}

function cacheKey(req) {            // אותו עמוד גם מניווט רגיל וגם מ-Turbo/טעינה מוקדמת
  const u = new URL(req.url);
  u.hash = '';
  return new Request(u.toString(), { method: 'GET' });
}

async function trim(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MAX_PAGES; i++) await cache.delete(keys[i]);
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(STATIC);
  const hit = await cache.match(req);
  const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await net) || new Response('', { status: 504 });
}

async function withOfflineBanner(res) {
  const saved = Number(res.headers.get('x-sw-saved-at') || 0);
  const when = saved ? new Date(saved).toLocaleString('he-IL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
  const html = await res.text();
  const banner =
    '<div id="swOffline" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#b45309;color:#fff;' +
    'text-align:center;padding:8px 12px;font:600 14px Heebo,Arial,sans-serif;direction:rtl">' +
    '📴 אין חיבור לאינטרנט - מוצג מידע שמור' + (when ? ' מ-' + when : '') +
    '. אי אפשר לבצע פעולות עד שהחיבור יחזור.</div>';
  const out = html.replace(/<body([^>]*)>/i, (m) => m + banner);
  return new Response(out, { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } });
}

function offlinePage(text) {
  return new Response(
    '<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>אין חיבור</title></head><body style="font-family:Heebo,Arial,sans-serif;background:#0f1117;color:#e8eaf6;' +
    'display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px">' +
    '<div style="font-size:44px">📴</div><h2>אין חיבור לאינטרנט</h2><p style="color:#8892b0">' + text + '</p>' +
    '<button onclick="history.back()" style="margin-top:12px;padding:10px 22px;border:0;border-radius:8px;background:#4f6ef7;color:#fff;font-size:15px;cursor:pointer">חזרה</button>' +
    '</div></body></html>',
    { status: 503, headers: { 'content-type': 'text/html; charset=utf-8' } });
}

function offlineActionPage() {
  return offlinePage('הפעולה לא בוצעה, כי אין חיבור. כשהחיבור יחזור - יש לבצע אותה שוב.');
}

self.addEventListener('message', (e) => {
  if (e.data === 'clear') e.waitUntil(caches.delete(PAGES));
});
