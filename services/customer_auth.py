"""
אבטחת אזור הלקוח (/my) - לקוחות פרטיים.

עקרונות:
* מי נכנס: רק לקוח פרטי (לא תלמיד של מוסד, לא "לקוח-דמה" של מוסד), לא חסום, עם מייל רשום,
  ורק אם המנהל פתח את האזור (כבוי / רק מסומנים / כולם - Settings.customer_portal_mode).
  הטלפון של המנהל (0527134491) תמיד מורשה, לבדיקות.
* סיסמה נשמרת רק כגיבוב (scrypt). קודים חד-פעמיים - HMAC עם המפתח הסודי, 10 דקות, 5 ניסיונות.
* מכשיר חדש: גם עם סיסמה נכונה - קוד למייל. מכשיר שאומת נשמר 90 יום (בדפדפן רק אסימון
  אקראי, אצלנו רק הגיבוב שלו). אפשר לבטל מכשירים, ו"ניתוק מכל המכשירים".
* סשן חתום: נגזר מהסיסמה ומ"גרסת הסשן". החלפת סיסמה / ניתוק הכל / חסימה = ניתוק מיידי.
* נעילה: 8 ניסיונות שגויים = 15 דקות (בבסיס הנתונים), ובנוסף הגבלה לפי IP.
* CSRF: כל בקשה משנה-מידע ב-/my חייבת להגיע מהאתר וגם לשאת אסימון סשן (X-CSRF-Token / csrf_token).
* מה הלקוח יכול לשנות: רק רשימה סגורה של שדות (ברירות מחדל לתמלול, אופן קבלה, פקס, מייל
  באימות קוד, סיסמה). יתרה, מחירון, חסימה, מבצעים, מוסד - לעולם לא מגיעים מהטופס.
"""
import hmac
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from flask import current_app, request, session

log = logging.getLogger(__name__)

ADMIN_PHONE = '0527134491'
MAX_FAILS = 8
LOCK_MINUTES = 15
CODE_MINUTES = 10
CODE_TRIES = 5
DEVICE_DAYS = 90
DEVICE_COOKIE = 'tp_dev'


# ------------------------------------------------------------------ מי מורשה
def portal_mode():
    from models import Settings
    row = Settings.query.filter_by(key='customer_portal_mode').first()
    v = (row.value if row else '') or 'off'
    return v if v in ('off', 'marked', 'all') else 'off'


def is_private_customer(c):
    return bool(c) and not c.institution_id and not getattr(c, 'is_institution_self', False)


def _phones(c):
    from services.customer_phones import all_phones
    try:
        return set(all_phones(c))
    except Exception:
        return {c.phone} if c.phone else set()


def portal_allowed(c):
    if not is_private_customer(c) or c.is_blocked or not (c.email or '').strip():
        return False
    if ADMIN_PHONE in _phones(c):
        return True
    mode = portal_mode()
    if mode == 'all':
        return True
    if mode == 'marked':
        return bool(c.account and c.account.enabled)
    return False


def account_for(c, create=True):
    from app import db
    from models import CustomerAccount
    acc = c.account
    if not acc and create:
        acc = CustomerAccount(customer=c, session_ver=0, failed=0, code_tries=0)
        db.session.add(acc)
        db.session.flush()
    return acc


def find_customer(identifier):
    """מייל או טלפון (גם מספר נוסף בחשבון). מייל שמשותף לכמה לקוחות - לא מזהה אף אחד."""
    from models import Customer
    from sqlalchemy import func
    ident = (identifier or '').strip()
    if not ident or len(ident) > 200:
        return None
    if '@' in ident:
        rows = Customer.query.filter(func.lower(func.trim(Customer.email)) == ident.lower(),
                                     Customer.institution_id.is_(None)).limit(2).all()
        return rows[0] if len(rows) == 1 else None
    phone = ident.replace('-', '').replace(' ', '')
    if phone.startswith('+972'):
        phone = '0' + phone[4:]
    elif phone.startswith('972'):
        phone = '0' + phone[3:]
    if not phone.isdigit():
        return None
    from services.customer_phones import find_customer_by_phone
    c = find_customer_by_phone(phone)
    return c if c and not c.institution_id else None


# ------------------------------------------------------------------ סשן
def _key():
    return (current_app.secret_key or '').encode()


def session_fp(c):
    acc = c.account
    return hmac.new(_key(), f'cust-session|{c.id}|{acc.password_hash if acc else "-"}|{acc.session_ver if acc else 0}'.encode(),
                    hashlib.sha256).hexdigest()[:40]


def mark_session(c, rotate=True):
    session['cust_fp'] = session_fp(c)
    if rotate or not session.get('csrf'):
        session['csrf'] = secrets.token_urlsafe(24)
    session.permanent = True


def session_valid(c):
    return hmac.compare_digest(str(session.get('cust_fp', '')), session_fp(c))


def csrf_token():
    tok = session.get('csrf')
    if not tok:
        tok = session['csrf'] = secrets.token_urlsafe(24)
    return tok


def csrf_ok():
    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
    tok = session.get('csrf') or ''
    return bool(tok) and hmac.compare_digest(sent.encode(), tok.encode())


# ------------------------------------------------------------------ נעילה
def locked(acc):
    return bool(acc and acc.locked_until and acc.locked_until > datetime.utcnow())


def register_fail(acc):
    acc.failed = (acc.failed or 0) + 1
    if acc.failed >= MAX_FAILS:
        acc.locked_until = datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)
        acc.failed = 0
        log.warning(f'customer portal: account {acc.customer_id} locked for {LOCK_MINUTES} min')


def clear_fails(acc):
    acc.failed = 0
    acc.locked_until = None


# ------------------------------------------------------------------ קודים חד-פעמיים
def _code_hash(c, purpose, code, target=''):
    return hmac.new(_key(), f'cust-code|{c.id}|{purpose}|{target}|{code}'.encode(), hashlib.sha256).hexdigest()


def issue_code(c, purpose, target=''):
    acc = account_for(c)
    code = ''.join(secrets.choice('0123456789') for _ in range(6))
    acc.code_hash = _code_hash(c, purpose, code, target)
    acc.code_purpose = purpose
    acc.code_target = target or None
    acc.code_expires = datetime.utcnow() + timedelta(minutes=CODE_MINUTES)
    acc.code_tries = 0
    return code


def check_code(c, purpose, code):
    """True אם הקוד נכון. כל ניסיון שגוי נספר; אחרי 5 - הקוד נפסל."""
    acc = c.account
    if not acc or not acc.code_hash or acc.code_purpose != purpose or not acc.code_expires \
            or acc.code_expires < datetime.utcnow():
        return False
    code = ''.join(ch for ch in (code or '') if ch.isdigit())
    if len(code) != 6:
        acc.code_tries = (acc.code_tries or 0) + 1
    elif hmac.compare_digest(acc.code_hash, _code_hash(c, purpose, code, acc.code_target or '')):
        acc.code_hash = None
        acc.code_expires = None
        acc.code_tries = 0
        return True
    else:
        acc.code_tries = (acc.code_tries or 0) + 1
    if acc.code_tries >= CODE_TRIES:
        acc.code_hash = None
        acc.code_expires = None
    return False


# ------------------------------------------------------------------ מכשירים מהימנים
def _tok_hash(tok):
    return hashlib.sha256(('dev|' + tok).encode()).hexdigest()


def device_trusted(c):
    from models import CustomerDevice
    tok = request.cookies.get(DEVICE_COOKIE) or ''
    if not tok or len(tok) > 100:
        return None
    d = CustomerDevice.query.filter_by(token_hash=_tok_hash(tok), customer_id=c.id, revoked=False).first()
    if not d or (datetime.utcnow() - (d.created_at or datetime.utcnow())).days > DEVICE_DAYS:
        return None
    return d


def trust_device(c, resp):
    """רושם את המכשיר הנוכחי כמהימן ושותל עוגייה (HttpOnly, Secure)."""
    from app import db
    from models import CustomerDevice
    d = device_trusted(c)
    if d:
        d.last_seen = datetime.utcnow()
        return d
    tok = secrets.token_urlsafe(32)
    d = CustomerDevice(customer_id=c.id, token_hash=_tok_hash(tok), label=device_label(),
                       ip=client_ip()[:64])
    db.session.add(d)
    secure = request.is_secure or (current_app.config.get('SESSION_COOKIE_SECURE') is True)
    resp.set_cookie(DEVICE_COOKIE, tok, max_age=DEVICE_DAYS * 86400, httponly=True, secure=secure,
                    samesite='Lax', path='/my')
    return d


def device_label():
    ua = (request.headers.get('User-Agent') or '')
    os_ = next((n for k, n in (('Windows', 'Windows'), ('Android', 'אנדרואיד'), ('iPhone', 'אייפון'),
                               ('iPad', 'אייפד'), ('Mac OS', 'מק'), ('Linux', 'לינוקס')) if k in ua), 'מכשיר')
    br = next((n for k, n in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Chrome/', 'Chrome'), ('Firefox/', 'Firefox'),
                              ('Safari/', 'Safari')) if k in ua), 'דפדפן')
    return f'{br} · {os_}'


def client_ip():
    from services.security import real_client_ip
    return real_client_ip()


def log_login(c, method, ok):
    try:
        from app import db
        from models import CustomerLoginLog
        db.session.add(CustomerLoginLog(customer_id=c.id, method=method, success=bool(ok), ip=client_ip()[:64],
                                        user_agent=(request.headers.get('User-Agent') or '')[:200]))
        db.session.commit()
    except Exception:
        log.exception('customer log_login failed')
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass


def password_error(pw1, pw2, c=None):
    import re as _re
    if len(pw1) < 8 or not _re.search(r'\d', pw1) or not _re.search(r'[^\d]', pw1):
        return 'הסיסמה חייבת להיות באורך 8 תווים לפחות, ולכלול גם ספרות וגם אותיות'
    if len(pw1) > 128:
        return 'הסיסמה ארוכה מדי'
    if len(set(pw1)) < 4:
        return 'הסיסמה פשוטה מדי - בחרו סיסמה מגוונת יותר'
    if c is not None:
        low = pw1.lower()
        for bad in [c.phone or '', (c.email or '').split('@')[0]]:
            if bad and len(bad) >= 4 and bad.lower() in low:
                return 'הסיסמה לא יכולה לכלול את הטלפון או המייל שלכם'
    if pw1 != pw2:
        return 'הסיסמאות אינן תואמות'
    return None


# ------------------------------------------------------------------ מיילים
def send_mail(to, subject, title, body_html):
    import os
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email
    from services import email_layout as L
    card = L.title(title) + body_html + L.signature(can_reply=False)
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=subject, html_content=L.wrap(card, show_circles=False))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)


def code_block(code):
    return (f'<div style="background:#eef2ff;border-radius:12px;padding:16px;text-align:center;font-size:30px;'
            f'font-weight:800;letter-spacing:6px;margin:16px 0;direction:ltr;color:#3730a3">{code}</div>')
