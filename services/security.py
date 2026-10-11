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


def client_ip():
    return request.remote_addr or '?'


def login_blocked():
    now = time.time()
    hits = [t for t in _FAILS.get(client_ip(), []) if now - t < WINDOW_SECONDS]
    _FAILS[client_ip()] = hits
    return len(hits) >= MAX_FAILS


def register_failed_login():
    _FAILS.setdefault(client_ip(), []).append(time.time())


def clear_failed_logins():
    _FAILS.pop(client_ip(), None)


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
    def _api_key_guard():
        secret = os.environ.get('API_SHARED_SECRET', '')
        path = request.path or ''
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
        if path == '/admin' or path.startswith('/admin/') or path.startswith('/institution'):
            resp.headers['Cache-Control'] = 'no-store'
            resp.headers.setdefault('Pragma', 'no-cache')
        return resp
