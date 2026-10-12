"""אבטחה מרכזית של המערכת.

עקרונות (נכשלים סגור - אם משהו חסר, חוסמים):
1. כל נתיב שמתחיל ב-/admin (כרטיסיות המנהל, ההקראה, בקשות השיוך וכו') עובר *בכל
   בקשה* בדיקה אחת מרכזית: האם זו התחברות של מנהל-העל אמיתי. בדיקה זו קודמת לכל
   route ולכל @login_required, כך שגם route חדש שמישהו ישכח לנעול - ננעל אוטומטית.
2. אם הוגדר ADMIN_GOOGLE_EMAILS - נדרש גם אימות Google, ורק מהכתובות ברשימה (סיסמה *וגם* Google).
   "מנהל-העל אמיתי" = משתמש מסוג AdminUser ששמו שווה ל-ADMIN_USERNAME, שיש
   ADMIN_PASSWORD מוגדר בשרת, והעוגייה נושאת חתימת-סשן (admin_fp) שנגזרת מהסיסמה
   ומהמפתח הסודי. החלפת הסיסמה במשתנה הסביבה מנתקת מיד את כל המנהלים המחוברים.
3. בקשות שמשנות מידע (POST וכו') ל-/admin חייבות להגיע מאותו אתר (Origin/Referer).
4. מפתח ההצפנה של העוגיות (SECRET_KEY) לא יכול להיות ברירת המחדל/קצר - אחרת
   אפשר לזייף עוגיית מנהל. אם הוא לא הוגדר, גוזרים מפתח חזק ממשתני סביבה סודיים.
5. ניסיונות כניסה שגויים מוגבלים לפי כתובת IP.
6. כותרות אבטחה לכל התשובות, ו-no-store לדפי המנהל.
7. אופציונלי: API_SHARED_SECRET - נעילת נקודות ה-/api של ה-IVR במפתח משותף.
"""
import os
import hmac
import time
import hashlib
import logging
from urllib.parse import urlparse

from flask import request, redirect, session, jsonify, current_app

log = logging.getLogger(__name__)

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
ADMIN_OPEN_PATHS = ('/admin/login', '/admin/logout', '/admin/auth/google/start', '/admin/auth/google/callback')

# --- הגבלת ניסיונות כניסה (בזיכרון, לכל תהליך) ---
_FAILS = {}
MAX_FAILS = 5
WINDOW_SECONDS = 15 * 60


def admin_credentials():
    return (os.environ.get('ADMIN_USERNAME') or 'admin').strip(), os.environ.get('ADMIN_PASSWORD') or ''


def allowed_admin_emails():
    """כתובות Google שמורשות להיכנס לממשק הניהול - משתנה הסביבה ADMIN_GOOGLE_EMAILS
    (מופרדות בפסיק/רווח/נקודה-פסיק). אם ריק - אין שלב Google (סיסמה בלבד)."""
    raw = os.environ.get('ADMIN_GOOGLE_EMAILS', '')
    parts = [p.strip().lower() for p in raw.replace(';', ',').replace(' ', ',').replace('\n', ',').split(',')]
    return sorted({p for p in parts if p})


def google_required():
    return bool(allowed_admin_emails())


def admin_session_fp(app=None):
    """חתימת סשן של מנהל: נגזרת מהמפתח הסודי + שם משתמש + סיסמת הסביבה."""
    user, pw = admin_credentials()
    key = ((app or current_app).secret_key or '').encode()
    return hmac.new(key, f'admin-session|{user}|{pw}|{",".join(allowed_admin_emails())}'.encode(), hashlib.sha256).hexdigest()[:40]


def is_valid_admin_session():
    """האם הבקשה הנוכחית מגיעה ממנהל-העל מחובר באמת."""
    from flask_login import current_user
    from models import AdminUser
    try:
        if not current_user.is_authenticated:
            return False
        user = current_user._get_current_object()
    except Exception:
        return False
    if not isinstance(user, AdminUser):
        return False
    env_user, env_pass = admin_credentials()
    if not env_pass or user.username != env_user:
        return False
    if not hmac.compare_digest(str(session.get('admin_fp', '')), admin_session_fp()):
        return False
    if google_required() and str(session.get('admin_google', '')).lower() not in allowed_admin_emails():
        return False
    return True


def _is_private_ip(ip):
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True   # ערך לא תקין - לא סומכים עליו
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or \
        (a.version == 4 and a in ipaddress.ip_network('100.64.0.0/10'))


def real_client_ip():
    """כתובת ה-IP האמיתית של הפונה, בלי אפשרות זיוף.

    הפרוקסי של Railway *מוסיף* את הכתובת האמיתית בסוף כותרת X-Forwarded-For,
    ואילו כל מה שנמצא משמאלה הוא מידע שהפונה עצמו שלח (ניתן לזיוף). לכן הולכים
    מימין לשמאל, מדלגים על כתובות פנימיות של הרשת, ולוקחים את הציבורית הראשונה.
    (הקוד הישן לקח את הערך השמאלי ביותר - ולכן אפשר היה לזייף "אני נדרים פלוס".)"""
    chain = [p.strip() for p in (request.headers.get('X-Forwarded-For') or '').split(',') if p.strip()]
    if request.remote_addr:
        chain.append(request.remote_addr)
    for ip in reversed(chain):
        if not _is_private_ip(ip):
            return ip
    return request.remote_addr or '?'


def client_ip():
    return real_client_ip()


# --- מונה ניסיונות כללי לפי מפתח (IP / חשבון), בזיכרון ---
def hits_blocked(key, max_hits=MAX_FAILS, window=WINDOW_SECONDS):
    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < window]
    _FAILS[key] = hits
    if not hits:
        _FAILS.pop(key, None)
    return len(hits) >= max_hits


def hits_add(key):
    _FAILS.setdefault(key, []).append(time.time())
    if len(_FAILS) > 20000:   # הגנה מניפוח זיכרון
        for k in list(_FAILS)[:5000]:
            _FAILS.pop(k, None)


def hits_clear(key):
    _FAILS.pop(key, None)


def login_blocked():
    return hits_blocked('ip:' + client_ip())


def register_failed_login():
    hits_add('ip:' + client_ip())


def clear_failed_logins():
    hits_clear('ip:' + client_ip())


def nedarim_key_ok():
    """אופציונלי: NEDARIM_WEBHOOK_KEY - סוד שמצורף ככתובת ה-Webhook (?key=...).
    אם הוגדר, בקשה בלעדיו נדחית גם אם הגיעה מכתובת IP "נכונה". אם לא הוגדר - אין שינוי."""
    secret = os.environ.get('NEDARIM_WEBHOOK_KEY', '')
    if not secret:
        return True
    return hmac.compare_digest((request.args.get('key') or '').encode(), secret.encode())


def new_login_code():
    """קוד כניסה זמני בן 6 ספרות, מחולל קריפטוגרפי (לא random רגיל שניתן לחיזוי)."""
    import secrets
    return ''.join(secrets.choice('0123456789') for _ in range(6))


def nedarim_request_ok(allowed_ips, ip=None):
    """אימות עדכון שמגיע מנדרים פלוס, לפי התיעוד שלהם:
    1. אם הוגדר NEDARIM_WEBHOOK_SECRET (אפשר כמה מפתחות מופרדים בפסיק, להחלפת מפתח) - חובה חתימת
       HMAC-SHA256 תקינה על timestamp + "." + גוף הבקשה הגולמי, וחותמת זמן בטווח של 5 דקות.
       החתימה היא ההוכחה הקריפטוגרפית; כתובת IP שונה רק נרשמת באזהרה (הכתובות שלהם עלולות להתחלף).
    2. אם לא הוגדר - בדיקת כתובת IP אמיתית (ואם הוגדר NEDARIM_WEBHOOK_KEY - גם המפתח ב-?key=)."""
    import re
    secrets_ = [x for x in re.split(r'[,\s]+', os.environ.get('NEDARIM_WEBHOOK_SECRET', '')) if x]
    ip = ip or real_client_ip()
    if secrets_:
        ts = (request.headers.get('X-Nedarim-Timestamp') or '').strip()
        sig = (request.headers.get('X-Nedarim-Signature') or '').strip()
        if sig.startswith('v1='):
            sig = sig[3:]
        try:
            fresh = abs(time.time() - int(ts)) <= 300
        except ValueError:
            fresh = False
        if not (ts and sig and fresh):
            log.warning(f'nedarim update rejected: missing/stale signature headers (ip={ip})')
            return False
        raw = request.get_data()
        for sec_ in secrets_:
            expected = hmac.new(sec_.encode(), ts.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, sig.lower()):
                if ip not in allowed_ips:
                    log.warning(f'nedarim update has valid signature but unexpected IP {ip} - Nedarim IPs may have changed')
                return True
        log.warning(f'nedarim update rejected: bad signature (ip={ip})')
        return False
    if ip not in allowed_ips:
        log.warning(f'nedarim update rejected: unexpected IP {ip}')
        return False
    return nedarim_key_ok()


SAFE_UPLOAD_EXTS = {'wav', 'mp3', 'm4a', 'mp4', 'ogg', 'oga', 'opus', 'webm', 'flac', 'aac', 'amr', 'wma', '3gp', 'mov', 'aiff', 'caf'}


def safe_audio_ext(filename, default='wav'):
    """סיומת בטוחה לקובץ שהמשתמש העלה ונשמר בתיקייה ציבורית. סיומות כמו .html/.svg
    היו מאפשרות להעלות דף שמריץ קוד באתר שלנו - לכן רק סיומות מדיה מוכרות."""
    ext = os.path.splitext(filename or '')[1].lstrip('.').lower()
    return '.' + (ext if ext in SAFE_UPLOAD_EXTS else default)


def resolve_secret_key():
    """מפתח חזק לעוגיות. אם SECRET_KEY לא הוגדר/חלש - גוזרים אחד קבוע וסודי."""
    key = os.environ.get('SECRET_KEY', '')
    if key and key != 'change-this' and len(key) >= 16:
        return key
    seed = '|'.join([os.environ.get('DATABASE_URL', ''), os.environ.get('ADMIN_PASSWORD', ''),
                     os.environ.get('SENDGRID_API_KEY', ''), os.environ.get('YEMOT_TOKEN', '')])
    if seed.replace('|', ''):
        log.warning('SECRET_KEY is missing or weak - using a derived key. Set a long random SECRET_KEY in the environment.')
        return hashlib.sha256(('derived-secret-key|' + seed).encode()).hexdigest()
    log.warning('SECRET_KEY is missing and nothing to derive from - generating a per-process key (sessions reset on restart).')
    return os.urandom(32).hex()


def _same_origin():
    """בקשה משנה-מידע חייבת להגיע מהאתר עצמו. אין כותרת בכלל = לא דפדפן (עוגיית SameSite חוסמת ממילא)."""
    for hdr in ('Origin', 'Referer'):
        val = request.headers.get(hdr)
        if val:
            return urlparse(val).netloc.lower() == request.host.lower()
    return True


# נקודות /api שיש להן אימות משלהן / ציבוריות בכוונה (לא נעולות במפתח ה-IVR)
API_KEY_EXEMPT_PREFIXES = (
    '/api/alefbot-webhook', '/api/fax-delivery-webhook', '/api/email-inbound',
    '/api/recordings-email/', '/api/email-choice/', '/api/nedarim/callback/',
)


def init_security(app):
    app.secret_key = app.config['SECRET_KEY'] = resolve_secret_key()
    secure = bool((os.environ.get('APP_URL') or os.environ.get('APP_BASE_URL') or '').startswith('https')
                  or os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('SESSION_COOKIE_SECURE') == '1')
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=secure,
        PERMANENT_SESSION_LIFETIME=12 * 3600,   # כניסה תקפה עד 12 שעות
    )

    @app.before_request
    def _admin_area_guard():
        path = request.path or ''
        if not (path == '/admin' or path.startswith('/admin/')):
            return None
        if path in ADMIN_OPEN_PATHS:
            return None
        if not is_valid_admin_session():
            from flask_login import current_user
            wants_page = request.method == 'GET' and 'application/json' not in (request.headers.get('Accept') or '')
            # מוסד מחובר שהגיע לכאן בטעות - חזרה לאזור שלו; כל השאר - לדף כניסת המנהל
            try:
                if current_user.is_authenticated and current_user.__class__.__name__ == 'Institution' and wants_page:
                    return redirect('/institution/dashboard')
            except Exception:
                pass
            if wants_page:
                return redirect('/admin/login')
            return jsonify({'error': 'unauthorized'}), 401
        if request.method not in SAFE_METHODS and not _same_origin():
            log.warning(f'admin request blocked: cross-origin {request.method} {path}')
            return jsonify({'error': 'forbidden'}), 403
        return None

    @app.before_request
    def _institution_csrf_guard():
        """בקשות שמשנות מידע באזור המוסדות חייבות להגיע מהאתר עצמו (הגנה מתקיפת CSRF)."""
        path = request.path or ''
        if request.method in SAFE_METHODS or not path.startswith('/institution'):
            return None
        if not _same_origin():
            log.warning(f'institution request blocked: cross-origin {request.method} {path}')
            return jsonify({'error': 'forbidden'}), 403
        return None

    @app.before_request
    def _api_key_guard():
        path = request.path or ''
        # אופציונלי: WEBHOOK_SECRET - מפתח ב-?key=... לשלוש ה-webhooks החיצוניים (מייל נכנס, אלף בוט, דוח פקס).
        # מגדירים אותו גם בכתובת ה-Webhook אצל הספק. לא מוגדר = אין שינוי.
        wsecret = os.environ.get('WEBHOOK_SECRET', '')
        if wsecret and path in ('/api/email-inbound', '/api/alefbot-webhook', '/api/fax-delivery-webhook'):
            if not hmac.compare_digest((request.args.get('key') or '').encode(), wsecret.encode()):
                log.warning(f'webhook rejected (bad/missing key): {path}')
                return jsonify({'error': 'unauthorized'}), 401
            return None
        secret = os.environ.get('API_SHARED_SECRET', '')
        if not secret or not path.startswith('/api/') or path.startswith(API_KEY_EXEMPT_PREFIXES):
            return None
        sent = request.headers.get('X-API-Key') or request.args.get('key') or ''
        if not hmac.compare_digest(sent.encode(), secret.encode()):
            log.warning(f'api request rejected (bad/missing key): {request.method} {path}')
            return jsonify({'error': 'unauthorized'}), 401
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        if secure:
            resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        path = request.path or ''
        if path.startswith('/static/fax_tmp/') or path.startswith('/api/recordings-email/'):
            # קבצים שהועלו ע"י משתמשים ומוגשים ציבורית: אסור שיפעילו קוד באתר גם אם הועלה HTML
            if (resp.mimetype or '').startswith(('text/html', 'image/svg', 'application/xhtml', 'text/xml', 'application/xml')):
                resp.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
                resp.headers['Content-Type'] = 'application/octet-stream'
                resp.headers['Content-Disposition'] = 'attachment'
        if path == '/admin' or path.startswith('/admin/') or path.startswith('/institution'):
            resp.headers['Cache-Control'] = 'no-store'
            resp.headers.setdefault('Pragma', 'no-cache')
        return resp
