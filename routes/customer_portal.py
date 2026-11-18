"""
האזור האישי ללקוח פרטי - /my

אותו ממשק כמו אזור המוסד (בלי ניהול תלמידים): תמלול חדש (קובץ / הקלטה / קישור), כל התמלולים
עם נגן ועריכה, לוח שנה, הורדה מרוכזת, דוחות, יתרה וחיובים, הגדרות הטלפון, אבטחה וצור קשר.
דפי התמלולים, הנגן והדוחות - אותו קוד בדיוק כמו במוסד (routes/institution_portal.py), שמזהה
לקוח מחובר ומציג רק את ההקלטות שלו.

אבטחה - ראו services/customer_auth.py. בקצרה: כניסה רק ללקוח פרטי שהמנהל פתח לו, סיסמה + קוד
למייל בכל מכשיר חדש, Google רק אחרי קישור מתוך החשבון, סשן חתום, CSRF כפול, נעילה והגבלת קצב,
ורשימה סגורה של שדות שהלקוח יכול לשנות.
"""
import os
import re
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Blueprint, render_template, request, redirect, url_for, flash, session, jsonify,
                   make_response, abort)
from flask_login import current_user, login_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash

from app import db
from models import Customer, Recording, Transaction, CustomerDevice, CustomerLoginLog
from services import customer_auth as CA

log = logging.getLogger(__name__)
customer_bp = Blueprint('customer', __name__, url_prefix='/my')
IL = ZoneInfo('Asia/Jerusalem')
_DUMMY_HASH = generate_password_hash('dummy-password-for-timing')


def _wants_json():
    return (request.headers.get('X-Requested-With') == 'fetch' or request.is_json
            or 'application/json' in (request.headers.get('Accept') or ''))


def customer_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user
        ok = u.is_authenticated and u.__class__.__name__ == 'Customer'
        c = u._get_current_object() if ok else None
        if ok and (not CA.portal_allowed(c) or not CA.session_valid(c)):
            logout_user()
            session.clear()
            ok = False
            flash('יש להתחבר מחדש')
        if not ok:
            if _wants_json():
                return jsonify({'error': 'יש להתחבר מחדש'}), 401
            return redirect(url_for('customer.login'))
        if session.get('cust_must_pw') and request.endpoint not in ('customer.set_password', 'customer.logout'):
            return redirect(url_for('customer.set_password'))
        return view(*args, **kwargs)
    return wrapped


@customer_bp.app_context_processor
def _cust_ctx():
    try:
        if (request.path or '').startswith('/my') and current_user.is_authenticated \
                and current_user.__class__.__name__ == 'Customer':
            from routes.institution_portal import customer_chat_unread
            return {'cust_chat_unread': customer_chat_unread(current_user)}
    except Exception:
        pass
    return {}


def _loc(dt, fmt='%d/%m/%Y %H:%M'):
    return dt.replace(tzinfo=timezone.utc).astimezone(IL).strftime(fmt) if dt else ''


# =========================================================================== כניסה
def _login_state():
    st = session.get('cust_login') or {}
    if st and (datetime.utcnow().timestamp() - st.get('ts', 0)) > CA.CODE_MINUTES * 60:
        session.pop('cust_login', None)
        return {}
    return st


def _rate(key, limit, window):
    from services import security as sec
    if sec.hits_blocked(key, limit, window):
        return False
    sec.hits_add(key)
    return True


def _send_code_mail(c, code, purpose):
    what = {'login': 'כניסה לאזור האישי', 'device': 'אימות כניסה ממכשיר חדש'}.get(purpose, 'אימות')
    body = (f'<p style="line-height:1.8">שלום {c.name or ""},</p>'
            f'<p style="line-height:1.8">הקוד ל{what} בתמלול פון:</p>' + CA.code_block(code) +
            f'<p style="line-height:1.8">הקוד תקף ל-{CA.CODE_MINUTES} דקות. אל תמסרו אותו לאף אחד - '
            'הצוות שלנו לעולם לא יבקש אותו.</p>'
            '<p style="color:#6b7280;font-size:13px;line-height:1.6">אם לא ניסיתם להיכנס - התעלמו מההודעה. '
            'אם זה חוזר על עצמו, מומלץ להחליף סיסמה.</p>')
    CA.send_mail(c.email, f'קוד כניסה: {code} - תמלול פון', 'קוד חד-פעמי', body)


def _start_code(c, purpose, **extra):
    code = CA.issue_code(c, purpose)
    db.session.commit()
    try:
        _send_code_mail(c, code, purpose)
    except Exception:
        log.exception('customer portal: sending code failed')
    session['cust_login'] = dict({'cid': c.id, 'purpose': purpose, 'ts': datetime.utcnow().timestamp()}, **extra)


def _mask_email(e):
    e = (e or '').strip()
    if '@' not in e:
        return ''
    u, d = e.split('@', 1)
    return (u[:2] + '•' * max(2, len(u) - 2)) + '@' + d


def _finish_login(c, method, trust=True):
    acc = CA.account_for(c)
    CA.clear_fails(acc)
    acc.last_login_at = datetime.utcnow()
    new_device = CA.device_trusted(c) is None
    must_pw = not acc.password_hash and not acc.google_id     # נרשם עם Google - נכנס עם Google, בלי סיסמה
    db.session.commit()
    session.clear()
    login_user(c)
    CA.mark_session(c)
    if must_pw:
        session['cust_must_pw'] = True
    CA.log_login(c, method, True)
    resp = redirect(url_for('customer.set_password') if must_pw else url_for('customer.dashboard'))
    if trust:
        CA.trust_device(c, resp)
        db.session.commit()
    if new_device and not must_pw:
        _alert_new_login(c, method)
    return resp


def _alert_new_login(c, method):
    try:
        how = {'password': 'סיסמה + קוד למייל', 'google': 'חשבון Google', 'code': 'קוד למייל'}.get(method, method)
        body = (f'<p style="line-height:1.8">שלום {c.name or ""},</p>'
                f'<p style="line-height:1.8">בוצעה כניסה לאזור האישי שלך ממכשיר חדש: <b>{CA.device_label()}</b>, '
                f'{_loc(datetime.utcnow())} ({how}).</p>'
                '<p style="line-height:1.8">אם זה לא היית את/ה - היכנסו מיד, החליפו סיסמה ולחצו "ניתוק מכל המכשירים" '
                'בהגדרות, או פנו אלינו.</p>')
        threading.Thread(target=_send_bg, args=(c.email, 'כניסה חדשה לאזור האישי - תמלול פון', 'כניסה ממכשיר חדש', body),
                         daemon=True).start()
    except Exception:
        log.exception('alert_new_login failed')


def _send_bg(to, subject, title, body):
    try:
        CA.send_mail(to, subject, title, body)
    except Exception:
        log.exception('customer mail failed')


@customer_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated and current_user.__class__.__name__ == 'Customer' and request.method == 'GET':
        return redirect(url_for('customer.dashboard'))
    from services import security as sec
    ip = sec.client_ip()
    st = _login_state()
    stage = 'code' if st.get('cid') and request.args.get('step') != 'start' else 'start'
    if request.args.get('step') == 'start':
        session.pop('cust_login', None)

    if request.method == 'POST':
        action = request.form.get('action')
        generic = 'פרטי ההתחברות שגויים (אם זו הכניסה הראשונה - בחרו "כניסה ראשונה / שכחתי סיסמה")'

        if action in ('password', 'send_code'):
            if not _rate('cl-ip:' + ip, 15, 15 * 60):
                flash('יותר מדי ניסיונות. נסו שוב בעוד רבע שעה.')
                return render_template('customer/login.html', stage='start')
            ident = (request.form.get('identifier') or '').strip()
            c = CA.find_customer(ident)
            allowed = bool(c) and CA.portal_allowed(c)
            acc = CA.account_for(c, create=allowed) if allowed else None

            if action == 'send_code':
                # תמיד אותה תשובה - לא חושפים אם החשבון קיים
                if allowed and not CA.locked(acc) and _rate(f'cl-send:{c.id}', 4, 15 * 60):
                    _start_code(c, 'login', hint=_mask_email(c.email))
                else:
                    session['cust_login'] = {'cid': 0, 'purpose': 'login', 'ts': datetime.utcnow().timestamp(), 'hint': ''}
                flash('אם הפרטים רשומים אצלנו והאזור האישי פתוח עבורכם - נשלח קוד למייל הרשום')
                return redirect(url_for('customer.login'))

            pw = request.form.get('password') or ''
            if not allowed or not acc or not acc.password_hash:
                check_password_hash(_DUMMY_HASH, pw)          # אותו זמן תגובה - לא חושפים אם החשבון קיים
                if c and acc:
                    CA.log_login(c, 'password', False)
                flash(generic)
                return render_template('customer/login.html', stage='start', ident=ident)
            if CA.locked(acc):
                flash('החשבון ננעל זמנית בגלל ניסיונות שגויים. נסו שוב בעוד רבע שעה, או היכנסו בקוד למייל.')
                return render_template('customer/login.html', stage='start', ident=ident)
            if not check_password_hash(acc.password_hash, pw):
                CA.register_fail(acc)
                db.session.commit()
                CA.log_login(c, 'password', False)
                flash(generic)
                return render_template('customer/login.html', stage='start', ident=ident)
            # סיסמה נכונה. ממכשיר מוכר - נכנסים; ממכשיר חדש - גם קוד למייל
            if CA.device_trusted(c):
                return _finish_login(c, 'password')
            if not _rate(f'cl-send:{c.id}', 4, 15 * 60):
                flash('נשלחו יותר מדי קודים. נסו שוב בעוד רבע שעה.')
                return render_template('customer/login.html', stage='start', ident=ident)
            _start_code(c, 'device', hint=_mask_email(c.email), pw_ok=True)
            return redirect(url_for('customer.login'))

        if action == 'verify':
            st = _login_state()
            code = request.form.get('code') or ''
            if not st or not _rate('cl-vip:' + ip, 20, 15 * 60):
                flash('הקוד פג תוקף או שנעשו יותר מדי ניסיונות. התחילו מחדש.')
                session.pop('cust_login', None)
                return redirect(url_for('customer.login'))
            c = Customer.query.get(st.get('cid') or 0)
            if not c or not CA.portal_allowed(c) or (st.get('purpose') == 'device' and not st.get('pw_ok')):
                flash('הקוד שגוי או שפג תוקפו')
                return redirect(url_for('customer.login'))
            acc = CA.account_for(c)
            if CA.locked(acc):
                flash('החשבון ננעל זמנית. נסו שוב בעוד רבע שעה.')
                return redirect(url_for('customer.login'))
            if not CA.check_code(c, st.get('purpose'), code):
                CA.register_fail(acc)
                db.session.commit()
                CA.log_login(c, 'code', False)
                flash('הקוד שגוי או שפג תוקפו')
                return redirect(url_for('customer.login'))
            db.session.commit()
            return _finish_login(c, 'password' if st.get('purpose') == 'device' else 'code')

    st = _login_state()
    google_on = bool(os.environ.get('GOOGLE_OAUTH_CLIENT_ID'))
    return render_template('customer/login.html', stage=stage if st else 'start', hint=st.get('hint', ''),
                           purpose=st.get('purpose'), google_on=google_on, signup_on=_signup_on())


@customer_bp.route('/set-password', methods=['GET', 'POST'])
@customer_login_required
def set_password():
    acc = CA.account_for(current_user)
    if acc.password_hash and not session.get('cust_must_pw'):
        return redirect(url_for('customer.settings'))
    if request.method == 'POST':
        pw1, pw2 = request.form.get('password') or '', request.form.get('password2') or ''
        err = CA.password_error(pw1, pw2, current_user)
        if err:
            flash(err)
        else:
            acc.password_hash = generate_password_hash(pw1)
            acc.session_ver = (acc.session_ver or 0) + 1     # כל שאר המחוברים מתנתקים
            db.session.commit()
            session.pop('cust_must_pw', None)
            CA.mark_session(current_user, rotate=False)
            flash('הסיסמה נשמרה. מעכשיו נכנסים עם מייל/טלפון וסיסמה')
            return redirect(url_for('customer.dashboard'))
    return render_template('customer/set_password.html')


# =========================================================================== הרשמה עצמית
def _signup_on():
    from models import Settings
    row = Settings.query.filter_by(key='customer_signup_on').first()
    return CA.portal_mode() != 'off' and bool(row and row.value == '1')


def _signup_hash(email, code):
    import hmac as _h
    import hashlib as _hl
    from flask import current_app
    return _h.new((current_app.secret_key or '').encode(), f'cust-signup|{email}|{code}'.encode(), _hl.sha256).hexdigest()


_EMAIL_RE = re.compile(r'^[^@\s<>"\'`]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$')

MSG_EMAIL_HAS_ACCOUNT = ('כתובת המייל הזו כבר רשומה אצלנו, יש לה חשבון קיים. אין צורך להירשם שוב: '
                         'בדף הכניסה בחרו "כניסה ראשונה / שכחתי סיסמה" ונשלח לכם קוד למייל הזה.')
MSG_EMAIL_TAKEN = ('כתובת המייל הזו כבר רשומה אצלנו על שם משתמש אחר (למשל תלמיד של מוסד), ולכן אי אפשר '
                   'לפתוח איתה חשבון חדש. אפשר להירשם עם כתובת מייל אחרת, או לכתוב לנו ונסדר את זה.')


def _email_status(email):
    """None - פנוי; אחרת הסבר ברור למה אי אפשר להירשם איתו."""
    from sqlalchemy import func
    rows = Customer.query.filter(func.lower(func.trim(Customer.email)) == email).limit(3).all()
    if not rows:
        return None
    if len(rows) == 1 and CA.is_private_customer(rows[0]) and not rows[0].is_blocked:
        return MSG_EMAIL_HAS_ACCOUNT
    return MSG_EMAIL_TAKEN


def _create_signup_customer(email, pw_hash=None, google_id=None, google_email=None):
    c = Customer(name=email, email=email, balance=0.0, delivery_method='email', transcription_tier='basic')
    db.session.add(c)
    db.session.flush()
    acc = CA.account_for(c)
    acc.enabled = True
    acc.password_hash = pw_hash
    acc.google_id = google_id
    acc.google_email = google_email
    try:
        from models import ManagerMessage
        how = 'עם Google' if google_id else 'עם מייל וסיסמה'
        db.session.add(ManagerMessage(phone='אתר', name=email, email=email, delivery_method='email', status='new',
                                      source='customer_signup',
                                      transcript=f'לקוח חדש נרשם באזור האישי באתר {how} (#{c.id}). '
                                                 'אם ירצה לקשר טלפון - הוא יכול לאמת אותו בעצמו בשיחה למערכת.'))
    except Exception:
        log.exception('signup manager message failed')
    db.session.commit()
    return c


def _google_signup(gid, info):
    email = (info.get('email') or '').strip().lower()[:200]
    if not email or not info.get('email_verified', True) or not _EMAIL_RE.match(email):
        flash('לא קיבלנו מ-Google כתובת מייל מאומתת. אפשר להירשם עם מייל וסיסמה.')
        return redirect(url_for('customer.signup'))
    msg = _email_status(email)
    if msg:
        flash(msg)
        return redirect(url_for('customer.signup'))
    c = _create_signup_customer(email, google_id=gid, google_email=email)
    if not CA.portal_allowed(c):
        flash('החשבון נפתח, אבל האזור האישי עוד לא פתוח עבורו. ניצור קשר בהקדם.')
        return redirect(url_for('customer.login'))
    flash('ברוכים הבאים! החשבון נפתח.')
    return _finish_login(c, 'google')


@customer_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    """לקוח חדש: מייל + סיסמה (או Google), אימות קוד למייל - וזהו. שם הלקוח = כתובת המייל.
    החשבון נפתח בלי טלפון; את הטלפון הלקוח מאמת בעצמו בשיחה למערכת (הגדרות ← טלפון)."""
    import secrets
    import time
    from services import security as sec
    if current_user.is_authenticated and current_user.__class__.__name__ == 'Customer':
        return redirect(url_for('customer.dashboard'))
    if not _signup_on():
        flash('ההרשמה דרך האתר סגורה כרגע. לפתיחת חשבון התקשרו או כתבו לנו.')
        return redirect(url_for('customer.login'))
    ip = sec.client_ip()
    google_on = bool(os.environ.get('GOOGLE_OAUTH_CLIENT_ID'))
    st = session.get('cust_signup') or {}
    if st and time.time() - st.get('ts', 0) > CA.CODE_MINUTES * 60:
        session.pop('cust_signup', None)
        st = {}
    if request.args.get('step') == 'start':
        session.pop('cust_signup', None)
        st = {}

    def form_page(form):
        return render_template('customer/signup.html', stage='form', form=form, t0=time.time(), google_on=google_on)

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'start':
            email = (request.form.get('email') or '').strip().lower()[:200]
            pw1, pw2 = request.form.get('password') or '', request.form.get('password2') or ''
            form = {'email': email}
            # מלכודת לבוטים: שדה נסתר שבני אדם לא ממלאים, וטופס שנשלח תוך פחות מ-3 שניות
            try:
                quick = time.time() - float(request.form.get('t0') or 0) < 3
            except ValueError:
                quick = True
            if request.form.get('website') or quick:
                flash('משהו השתבש. נסו שוב.')
                return form_page(form)
            if not _rate('cs-ip:' + ip, 6, 60 * 60):
                flash('יותר מדי ניסיונות הרשמה. נסו שוב בעוד שעה.')
                return form_page(form)
            err = 'כתובת המייל לא תקינה' if not _EMAIL_RE.match(email) else (_email_status(email) or CA.password_error(pw1, pw2))
            if err:
                flash(err)
                return form_page(form)
            if not _rate('cs-mail:' + email, 3, 60 * 60):
                flash('נשלחו כבר כמה קודים למייל הזה. נסו שוב בעוד שעה.')
                return form_page(form)
            code = ''.join(secrets.choice('0123456789') for _ in range(6))
            session['cust_signup'] = {'email': email, 'pw': generate_password_hash(pw1),
                                      'h': _signup_hash(email, code), 'tries': 0, 'ts': time.time()}
            body = ('<p style="line-height:1.8">שלום,</p>'
                    '<p style="line-height:1.8">כדי לסיים את פתיחת החשבון בתמלול פון, הזינו את הקוד:</p>'
                    + CA.code_block(code) +
                    f'<p style="line-height:1.8">הקוד תקף ל-{CA.CODE_MINUTES} דקות. אם לא נרשמתם - התעלמו מההודעה.</p>')
            threading.Thread(target=_send_bg, args=(email, f'קוד אימות: {code} - תמלול פון', 'אימות מייל', body),
                             daemon=True).start()
            return redirect(url_for('customer.signup'))

        if action == 'verify':
            if not st or not _rate('cs-vip:' + ip, 20, 15 * 60):
                session.pop('cust_signup', None)
                flash('הקוד פג תוקף או שנעשו יותר מדי ניסיונות. התחילו מחדש.')
                return redirect(url_for('customer.signup'))
            code = ''.join(ch for ch in (request.form.get('code') or '') if ch.isdigit())
            import hmac as _h
            if len(code) != 6 or not _h.compare_digest(st['h'], _signup_hash(st['email'], code)):
                st['tries'] = st.get('tries', 0) + 1
                if st['tries'] >= CA.CODE_TRIES:
                    session.pop('cust_signup', None)
                    flash('יותר מדי ניסיונות שגויים. התחילו מחדש.')
                    return redirect(url_for('customer.signup'))
                session['cust_signup'] = st
                flash('הקוד שגוי')
                return render_template('customer/signup.html', stage='code', email=_mask_email(st['email']))
            session.pop('cust_signup', None)
            msg = _email_status(st['email'])
            if msg:
                flash(msg)
                return redirect(url_for('customer.signup'))
            c = _create_signup_customer(st['email'], pw_hash=st['pw'])
            if not CA.portal_allowed(c):
                flash('החשבון נפתח, אבל האזור האישי עוד לא פתוח עבורו. ניצור קשר בהקדם.')
                return redirect(url_for('customer.login'))
            CA.log_login(c, 'signup', True)
            flash('ברוכים הבאים! החשבון נפתח.')
            return _finish_login(c, 'code')

    if st:
        return render_template('customer/signup.html', stage='code', email=_mask_email(st['email']))
    return form_page({})


@customer_bp.route('/logout')
def logout():
    if current_user.is_authenticated and current_user.__class__.__name__ == 'Customer':
        logout_user()
    session.clear()
    return redirect(url_for('customer.login'))


# --------------------------------------------------------------------------- Google
GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


@customer_bp.route('/auth/google/start')
def google_start():
    import secrets
    import urllib.parse
    client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
    mode = request.args.get('mode') if request.args.get('mode') in ('link', 'signup') else 'login'
    if mode == 'signup' and not _signup_on():
        flash('ההרשמה דרך האתר סגורה כרגע')
        return redirect(url_for('customer.login'))
    if not client_id:
        flash('כניסה עם Google לא הוגדרה בשרת')
        return redirect(url_for('customer.settings' if mode == 'link' else 'customer.login'))
    if mode == 'link':
        # קישור רק מתוך חשבון מחובר (ועם סשן תקף)
        if not (current_user.is_authenticated and current_user.__class__.__name__ == 'Customer'
                and CA.session_valid(current_user)):
            return redirect(url_for('customer.login'))
    state = secrets.token_urlsafe(24)
    session['cust_g_state'] = state
    session['cust_g_mode'] = mode
    session['cust_g_cid'] = current_user.id if mode == 'link' else None
    params = {'client_id': client_id, 'redirect_uri': url_for('customer.google_callback', _external=True),
              'response_type': 'code', 'scope': 'openid email', 'state': state, 'prompt': 'select_account'}
    return redirect(f'{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}')


@customer_bp.route('/auth/google/callback')
def google_callback():
    import hmac
    import requests as rq
    state = session.pop('cust_g_state', None)
    mode = session.pop('cust_g_mode', 'login')
    link_cid = session.pop('cust_g_cid', None)
    if not state or not hmac.compare_digest(str(request.args.get('state') or ''), state) or not request.args.get('code'):
        flash('ההתחברות עם Google בוטלה או נכשלה')
        return redirect(url_for('customer.settings' if mode == 'link' else 'customer.login'))
    try:
        tok = rq.post(GOOGLE_TOKEN_URL, data={
            'code': request.args.get('code'), 'client_id': os.environ.get('GOOGLE_OAUTH_CLIENT_ID'),
            'client_secret': os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET'),
            'redirect_uri': url_for('customer.google_callback', _external=True), 'grant_type': 'authorization_code'},
            timeout=15)
        tok.raise_for_status()
        info = rq.get(GOOGLE_USERINFO_URL, headers={'Authorization': f'Bearer {tok.json().get("access_token")}'}, timeout=15)
        info.raise_for_status()
        info = info.json()
    except Exception:
        log.exception('customer google callback failed')
        flash('שגיאה בהתחברות עם Google, נסו שוב')
        return redirect(url_for('customer.settings' if mode == 'link' else 'customer.login'))
    gid = info.get('sub')
    from models import CustomerAccount
    if mode == 'link':
        if not (current_user.is_authenticated and current_user.__class__.__name__ == 'Customer'
                and current_user.id == link_cid and CA.session_valid(current_user)):
            return redirect(url_for('customer.login'))
        other = CustomerAccount.query.filter_by(google_id=gid).first()
        if other and other.customer_id != current_user.id:
            flash('חשבון ה-Google הזה כבר מקושר לחשבון אחר')
            return redirect(url_for('customer.settings') + '#security')
        acc = CA.account_for(current_user)
        acc.google_id = gid
        acc.google_email = (info.get('email') or '')[:200]
        db.session.commit()
        flash('חשבון Google קושר - מעכשיו אפשר להיכנס גם איתו')
        return redirect(url_for('customer.settings') + '#security')
    # כניסה: רק לחשבון שקושר מראש מתוך האזור האישי, או שנפתח בהרשמה עם Google (לא לפי מייל - מונע השתלטות)
    acc = CustomerAccount.query.filter_by(google_id=gid).first() if gid else None
    if not acc and mode == 'signup' and gid:
        return _google_signup(gid, info)
    c = Customer.query.get(acc.customer_id) if acc else None
    if not c or not CA.portal_allowed(c):
        flash('חשבון ה-Google הזה לא מקושר לאזור אישי. היכנסו בסיסמה וקשרו אותו בהגדרות ← אבטחה')
        return redirect(url_for('customer.login'))
    if CA.locked(acc):
        flash('החשבון ננעל זמנית. נסו שוב בעוד רבע שעה.')
        return redirect(url_for('customer.login'))
    return _finish_login(c, 'google')


# =========================================================================== PWA
@customer_bp.route('/manifest.webmanifest')
def manifest():
    gh = 'https://raw.githubusercontent.com/saryDavidovich/phone-transcription/main/static/pwa/'
    name = current_user.name if (current_user.is_authenticated and current_user.__class__.__name__ == 'Customer') else ''
    data = {'name': f'{name} - האזור האישי' if name else 'האזור האישי - תמלול פון', 'short_name': 'תמלול פון',
            'lang': 'he', 'dir': 'rtl', 'id': '/my/', 'start_url': '/my/', 'scope': '/my/', 'display': 'standalone',
            'background_color': '#f3f5fa', 'theme_color': '#4f46e5',
            'icons': [{'src': gh + 'cust-icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                      {'src': gh + 'cust-icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                      {'src': gh + 'cust-icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]}
    resp = make_response(json.dumps(data, ensure_ascii=False))
    resp.mimetype = 'application/json'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@customer_bp.route('/sw.js')
def sw():
    from flask import send_from_directory
    resp = send_from_directory(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'pwa'),
                               'inst-sw.js', mimetype='application/javascript', max_age=0)
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/my/'
    return resp


# =========================================================================== עמודים
@customer_bp.route('/')
@customer_bp.route('/dashboard')
@customer_login_required
def dashboard():
    c = current_user
    from sqlalchemy import func
    from services import pricing as P
    now_l = datetime.now(IL)
    month_start = now_l.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    base = Recording.query.filter(Recording.customer_id == c.id)
    month = (db.session.query(func.count(Recording.id), func.coalesce(func.sum(Recording.duration_seconds), 0),
                              func.coalesce(func.sum(Recording.cost), 0))
             .filter(Recording.customer_id == c.id, Recording.created_at >= month_start,
                     Recording.status.in_(('completed', 'transcribed', 'delivered', 'sent', 'done'))).first())
    pending = base.filter(Recording.status == 'pending_payment').count()
    total = base.filter(Recording.transcript.isnot(None)).count()
    return render_template('customer/dashboard.html', c=c, month_count=month[0], month_minutes=round((month[1] or 0) / 60),
                           month_cost=round(month[2] or 0, 2), pending=pending, total=total,
                           unit=P.unit_minutes(c), price_basic=P.unit_price(c, 'basic'),
                           inbound_email=os.environ.get('TRANSCRIBE_INBOUND_EMAIL', '033131795@sheasystem.com'))


@customer_bp.route('/transcribe')
@customer_login_required
def transcribe():
    from services import pricing as P
    from sqlalchemy.orm import defer
    recs = (Recording.query.filter(Recording.customer_id == current_user.id)
            .options(defer(Recording.file_data), defer(Recording.excel_file), defer(Recording.transcript),
                     defer(Recording.transcript_original), defer(Recording.summary))
            .order_by(Recording.created_at.desc()).limit(25).all())
    d = _defaults(current_user)
    return render_template('customer/transcribe.html', recs=recs, d=d, unit=P.unit_minutes(current_user),
                           price_basic='%.2f' % P.unit_price(current_user, 'basic'),
                           price_premium='%.2f' % P.unit_price(current_user, 'premium'))


@customer_bp.route('/transcribe/upload', methods=['POST'])
@customer_login_required
def transcribe_upload():
    """קובץ / הקלטה מהדפדפן / קישור - אותו עיבוד כמו העלאה של מוסד לתלמיד (Recording של הלקוח, חיוב מהיתרה)."""
    from services import portal_media as PM
    from services.security import safe_audio_ext
    from services import security as sec
    c = current_user
    if not _rate(f'cu-up:{c.id}', 30, 3600):
        return jsonify({'error': 'יותר מדי העלאות בשעה האחרונה. נסו שוב מאוחר יותר'}), 429
    file = request.files.get('audio_file')
    link = (request.form.get('link') or '').strip()
    if (not file or not file.filename) and not link:
        return jsonify({'error': 'יש לבחור קובץ, להקליט או להדביק קישור'}), 400
    if link and not PM.link_kind(link):
        return jsonify({'error': PM.LINK_HELP}), 400
    if (c.balance or 0) <= 0:
        return jsonify({'error': 'אין יתרה בחשבון. טענו יתרה בלשונית "יתרה וחיובים" ונסו שוב'}), 402
    static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
    os.makedirs(static_dir, exist_ok=True)
    local_path = None
    if file and file.filename:
        local_path = os.path.join(static_dir, f"cust_{uuid.uuid4().hex}{safe_audio_ext(file.filename)}")
        file.save(local_path)
        name = file.filename
    else:
        name = 'קישור Drive' if PM.link_kind(link) == 'drive' else "קישור ג'מבו מייל"
    name = (request.form.get('title') or '').strip()[:200] or name[:200]
    tier = 'premium' if request.form.get('tier') == 'premium' else 'gemini'
    language = request.form.get('language') if request.form.get('language') in ('he', 'yi', 'en') else 'he'
    output_language = 'original' if request.form.get('output_language') == 'original' else 'he'
    rec = Recording(customer_id=c.id, call_id=f"web_{uuid.uuid4().hex[:12]}", status='processing',
                    source_filename=name, transcription_tier=tier, language=language, output_language=output_language)
    db.session.add(rec)
    db.session.commit()
    from app import app as flask_app
    from routes.institution_students import _process_student_upload
    threading.Thread(target=_process_student_upload, args=(flask_app, rec.id, local_path, link or None),
                     kwargs={'send': bool(request.form.get('send_email'))}, daemon=True).start()
    return jsonify({'recordingId': rec.id, 'name': name})


@customer_bp.route('/transcribe/status/<int:rec_id>')
@customer_login_required
def transcribe_status(rec_id):
    r = Recording.query.filter_by(id=rec_id, customer_id=current_user.id).first()
    if not r:
        return jsonify({'error': 'לא נמצא'}), 404
    done = r.status in ('completed', 'transcribed', 'delivered', 'sent', 'done')
    return jsonify({'status': r.status, 'name': r.source_filename, 'cost': r.cost,
                    'durationSeconds': r.duration_seconds,
                    'viewUrl': f'/my/t/r/{r.id}' if done else None,
                    'downloadUrl': f'/my/t/r/{r.id}/download' if done else None})


@customer_bp.route('/billing')
@customer_login_required
def billing():
    from services import pricing as P
    from services import auto_topup as ATU
    c = current_user
    tx = Transaction.query.filter_by(customer_id=c.id).order_by(Transaction.created_at.desc()).limit(100).all()
    pend = Recording.query.filter_by(customer_id=c.id, status='pending_payment').all()
    kinds = ('basic', 'premium', 'video')
    prices = {k: P.unit_price(c, k) for k in kinds}
    return render_template('customer/billing.html', c=c, tx=tx, pending=len(pend),
                           pending_sum=round(sum((r.cost or 0) for r in pend), 2),
                           unit=P.unit_minutes(c), prices=prices, names=P.KIND_NAMES,
                           nedarim_on=bool(os.environ.get('NEDARIM_MOSAD') and os.environ.get('NEDARIM_API_VALID')),
                           auto=ATU.get(c), auto_on=ATU.enabled_for(c) and ATU.configured(),
                           has_receipt=lambda t: (t.amount or 0) > 0 and '[נדרים:' in (t.description or ''),
                           loc=_loc)


@customer_bp.route('/billing/receipt/<int:tx_id>')
@customer_login_required
def billing_receipt(tx_id):
    from services import customer_billing as CB
    tx = Transaction.query.filter_by(id=tx_id, customer_id=current_user.id).first()
    if not tx:
        abort(404)
    if not _rate(f'cu-rcpt:{current_user.id}', 30, 3600):
        flash('יותר מדי בקשות קבלה בשעה האחרונה. נסו שוב מאוחר יותר')
        return redirect(url_for('customer.billing'))
    url, err = CB.receipt_url(tx)
    if not url:
        flash(err)
        return redirect(url_for('customer.billing'))
    return redirect(url)


def _auto_vals(f):
    import math
    try:
        amount, threshold = float(f.get('amount')), float(f.get('threshold'))
    except (TypeError, ValueError):
        return None, None, 'ערכים לא תקינים'
    if not (math.isfinite(amount) and math.isfinite(threshold)) or not 20 <= amount <= 500 or not 0 <= threshold <= 200:
        return None, None, 'סכום הטעינה: 20 עד 500 ₪. הסף: 0 עד 200 ₪'
    return round(amount), round(threshold), None


@customer_bp.route('/billing/auto/setup', methods=['POST'])
@customer_login_required
def billing_auto_setup():
    """מכין שמירת כרטיס באייפרם (הוראת קבע מוקפאת אצל נדרים). הכרטיס לא מגיע אלינו."""
    from services import auto_topup as ATU
    c = current_user
    if not (ATU.enabled_for(c) and ATU.configured()):
        return _err('טעינה אוטומטית עוד לא זמינה', 503)
    if not _rate(f'cu-auto:{c.id}', 6, 3600):
        return _err('יותר מדי ניסיונות. נסו שוב בעוד שעה', 429)
    amount, threshold, err = _auto_vals(request.get_json(silent=True) or {})
    if err:
        return _err(err)
    return jsonify({'ok': True, 'tx': ATU.start_setup(c, amount, threshold)})


@customer_bp.route('/billing/auto/status')
@customer_login_required
def billing_auto_status():
    from services import auto_topup as ATU
    a = ATU.get(current_user)
    return jsonify({'status': a.status if a else 'off', 'last4': a.last4 if a else None})


@customer_bp.route('/billing/auto/save', methods=['POST'])
@customer_login_required
def billing_auto_save():
    from services import auto_topup as ATU
    a = ATU.get(current_user)
    if not a or not a.keva_id:
        return _err('אין כרטיס שמור')
    amount, threshold, err = _auto_vals(request.get_json(silent=True) or {})
    if err:
        return _err(err)
    a.amount, a.threshold = amount, threshold
    a.enabled = True
    a.status = 'active'
    a.fails = 0
    db.session.commit()
    return _ok('נשמר')


@customer_bp.route('/billing/auto/off', methods=['POST'])
@customer_login_required
def billing_auto_off():
    from services import auto_topup as ATU
    ATU.disable(current_user)
    return _ok('הטעינה האוטומטית בוטלה והכרטיס נמחק מנדרים')


@customer_bp.route('/billing/topup', methods=['POST'])
@customer_login_required
def billing_topup():
    """פותח עסקה בנדרים פלוס לאייפרם שבתוך הדף. פרטי הכרטיס מוזנים רק באייפרם של נדרים ולא מגיעים
    אלינו; הזיכוי - רק מעדכון השרת של נדרים (services/customer_billing.credit_charge)."""
    import math
    import time
    import requests
    from models import CustomerChargeLog
    from routes.institution_billing import NEDARIM_CREATE_URL, NEDARIM_CATEGORY
    from services import customer_billing as CB
    c = current_user
    mosad, api_valid = os.environ.get('NEDARIM_MOSAD', ''), os.environ.get('NEDARIM_API_VALID', '')
    if not mosad or not api_valid:
        return _err('טעינה באשראי עוד לא זמינה. אפשר לטעון בטלפון', 503)
    try:
        amount = round(float((request.get_json(silent=True) or {}).get('amount')), 2)
    except (TypeError, ValueError):
        return _err('סכום לא תקין')
    if not math.isfinite(amount) or amount < 5 or amount > 3000:
        return _err('אפשר לטעון בין 5 ל-3,000 ₪')
    if not _rate(f'cu-pay:{c.id}', 10, 3600):
        return _err('יותר מדי ניסיונות תשלום בשעה האחרונה. נסו שוב מאוחר יותר', 429)
    charge = CustomerChargeLog(customer_id=c.id, amount=amount, status='pending')
    db.session.add(charge)
    db.session.commit()
    app_url = (os.environ.get('APP_URL', '').rstrip('/') or request.url_root.rstrip('/')).replace('http://', 'https://', 1)
    cb = f"{app_url}/api/nedarim/customer-callback/{charge.id}"
    if os.environ.get('NEDARIM_WEBHOOK_KEY'):
        import urllib.parse as _up
        cb += '?key=' + _up.quote(os.environ['NEDARIM_WEBHOOK_KEY'], safe='')
    clean = lambda v: re.sub(r'[<>]', '', v or '')[:100]
    try:
        resp = requests.post(NEDARIM_CREATE_URL, data={
            'Mosad': mosad, 'ApiValid': api_valid, 'PaymentType': 'Ragil', 'Amount': str(amount), 'Currency': '1',
            'Tashlumim': '1', 'FirstName': clean(c.name)[:50] or 'לקוח', 'Mail': clean(c.email)[:50],
            'Phone': c.phone if (c.phone or '').isdigit() else '', 'Groupe': NEDARIM_CATEGORY,
            'Comment': f'טעינת יתרה - האזור האישי, לקוח {c.id} {CB.marker(charge.id)}', 'Param2': str(charge.id),
            'AjaxId': f'c{charge.id}-{int(time.time())}', 'CallBack': cb,
        }, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error(f'customer topup: CreateTransaction failed: {e}')
        charge.status = 'failed'
        db.session.commit()
        return _err('שגיאה בפנייה לשירות הסליקה, נסו שוב', 502)
    if data.get('Status') != 'OK':
        charge.status = 'failed'
        db.session.commit()
        return _err(data.get('Message') or 'שגיאה ביצירת העסקה')
    charge.provider_ref = str(data.get('ID') or '')
    db.session.commit()
    return jsonify({'ok': True, 'nedarimId': data.get('ID'), 'chargeId': charge.id})


@customer_bp.route('/billing/topup/<int:charge_id>')
@customer_login_required
def billing_topup_status(charge_id):
    from models import CustomerChargeLog
    ch = CustomerChargeLog.query.filter_by(id=charge_id, customer_id=current_user.id).first()
    if not ch:
        return _err('לא נמצא', 404)
    return jsonify({'status': ch.status, 'balance': round(current_user.balance or 0, 2)})


# --------------------------------------------------------------------------- הגדרות
def _defaults(c):
    ds = c.default_settings
    if isinstance(ds, str):
        try:
            ds = json.loads(ds)
        except ValueError:
            ds = {}
    ds = ds if isinstance(ds, dict) else {}
    return {'tier': 'premium' if ds.get('tier') == 'premium' else 'gemini',
            'language': ds.get('language') if ds.get('language') in ('he', 'yi', 'en') else 'he',
            'output_language': ds.get('output_language') if ds.get('output_language') in ('he', 'yi', 'en', 'original') else 'he'}


@customer_bp.route('/settings')
@customer_login_required
def settings():
    c = current_user
    acc = CA.account_for(c)
    db.session.commit()
    devices = CustomerDevice.query.filter_by(customer_id=c.id, revoked=False).order_by(CustomerDevice.last_seen.desc()).all()
    logins = CustomerLoginLog.query.filter_by(customer_id=c.id).order_by(CustomerLoginLog.created_at.desc()).limit(15).all()
    cur_dev = CA.device_trusted(c)
    from services.customer_phones import all_phones
    return render_template('customer/settings.html', c=c, acc=acc, d=_defaults(c), devices=devices, logins=logins,
                           cur_dev=cur_dev.id if cur_dev else None, phones=all_phones(c), loc=_loc,
                           phone_link_on=__import__('services.phone_link', fromlist=['x']).enabled_for(c.phone),
                           google_on=bool(os.environ.get('GOOGLE_OAUTH_CLIENT_ID')),
                           pending_email=acc.code_target if acc.code_purpose == 'email' and acc.code_expires
                           and acc.code_expires > datetime.utcnow() else None)


def _ok(msg, **extra):
    return jsonify(dict(ok=True, message=msg, **extra))


def _err(msg, code=400):
    return jsonify({'error': msg}), code


@customer_bp.route('/settings/phone', methods=['POST'])
@customer_login_required
def settings_phone():
    """ההגדרות שאפשר לשנות גם בטלפון (שלוחה 4 - ברירת מחדל להקלטה, ואופן קבלת התמלול). רשימה סגורה."""
    c = current_user
    f = request.get_json(silent=True) or {}
    tier = 'premium' if f.get('tier') == 'premium' else 'gemini'
    if tier == 'premium':
        new = {'tier': 'premium', 'language': _defaults(c)['language'],
               'output_language': 'original' if f.get('output_language') == 'original' else 'he'}
    else:
        lang = f.get('language') if f.get('language') in ('he', 'yi', 'en') else 'he'
        out = f.get('output_language')
        out = out if (lang != 'he' and out in (lang, 'he')) else ('he' if lang == 'he' else lang)
        new = {'tier': 'gemini', 'language': lang, 'output_language': out}
    method = f.get('delivery_method')
    if method not in ('email', 'fax'):
        return _err('אופן קבלה לא תקין')
    if method == 'fax' and not (c.fax or '').strip():
        return _err('כדי לקבל בפקס צריך קודם לשמור מספר פקס')
    c.default_settings = new
    c.delivery_method = method
    db.session.commit()
    return _ok('ההגדרות נשמרו - חלות גם בשיחות הטלפון')


@customer_bp.route('/settings/profile', methods=['POST'])
@customer_login_required
def settings_profile():
    c = current_user
    f = request.get_json(silent=True) or {}
    name = re.sub(r'[<>"\\]', '', (f.get('name') or '')).strip()[:100]
    fax = re.sub(r'[\s\-]', '', f.get('fax') or '')
    if fax and not re.fullmatch(r'0\d{8,9}', fax):
        return _err('מספר הפקס לא תקין (9-10 ספרות, מתחיל ב-0)')
    if not fax and c.delivery_method == 'fax':
        return _err('אי אפשר למחוק את הפקס כשהתמלולים נשלחים בפקס - שנו קודם את אופן הקבלה')
    if name:
        c.name = name
    c.fax = fax or None
    db.session.commit()
    return _ok('הפרטים נשמרו')


@customer_bp.route('/settings/email/start', methods=['POST'])
@customer_login_required
def settings_email_start():
    """החלפת מייל: סיסמה נוכחית + קוד שנשלח לכתובת החדשה (המייל הוא גם הזהות לשליחת הקלטות במייל)."""
    c = current_user
    acc = CA.account_for(c)
    f = request.get_json(silent=True) or {}
    new = (f.get('email') or '').strip().lower()
    if not re.fullmatch(r'[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}', new):
        return _err('כתובת המייל לא תקינה')
    if new == (c.email or '').strip().lower():
        return _err('זו כבר הכתובת הרשומה')
    if not acc.password_hash or not check_password_hash(acc.password_hash, f.get('password') or ''):
        CA.register_fail(acc)
        db.session.commit()
        return _err('הסיסמה שגויה', 403)
    if not _rate(f'cu-mail:{c.id}', 3, 3600):
        return _err('נשלחו יותר מדי קודים. נסו שוב בעוד שעה', 429)
    code = CA.issue_code(c, 'email', new)
    db.session.commit()
    try:
        CA.send_mail(new, f'קוד לאימות המייל: {code} - תמלול פון', 'אימות כתובת מייל',
                     '<p style="line-height:1.8">כדי לעדכן את המייל באזור האישי, הקלידו את הקוד:</p>' + CA.code_block(code) +
                     f'<p style="color:#6b7280;font-size:13px">הקוד תקף ל-{CA.CODE_MINUTES} דקות.</p>')
    except Exception:
        log.exception('email change code failed')
        return _err('שליחת המייל נכשלה. בדקו את הכתובת ונסו שוב', 502)
    return _ok('נשלח קוד לכתובת החדשה', pending=new)


@customer_bp.route('/settings/email/verify', methods=['POST'])
@customer_login_required
def settings_email_verify():
    c = current_user
    acc = CA.account_for(c)
    target = acc.code_target
    if not target or not CA.check_code(c, 'email', (request.get_json(silent=True) or {}).get('code') or ''):
        db.session.commit()
        return _err('הקוד שגוי או שפג תוקפו')
    old = c.email
    c.email = target
    db.session.commit()
    if old:
        threading.Thread(target=_send_bg, args=(old, 'כתובת המייל באזור האישי שונתה - תמלול פון', 'המייל שונה',
                         f'<p style="line-height:1.8">כתובת המייל בחשבון שונתה ל-{_mask_email(target)}. '
                         'אם לא אתם ביצעתם את השינוי - פנו אלינו מיד.</p>'), daemon=True).start()
    return _ok('המייל עודכן', email=target)


@customer_bp.route('/settings/password', methods=['POST'])
@customer_login_required
def settings_password():
    c = current_user
    acc = CA.account_for(c)
    f = request.get_json(silent=True) or {}
    if CA.locked(acc):
        return _err('החשבון ננעל זמנית. נסו שוב בעוד רבע שעה', 423)
    first = not acc.password_hash        # נרשם עם Google ועוד לא הגדיר סיסמה - המחובר עצמו קובע
    if not first and not check_password_hash(acc.password_hash, f.get('current') or ''):
        CA.register_fail(acc)
        db.session.commit()
        return _err('הסיסמה הנוכחית שגויה', 403)
    err = CA.password_error(f.get('password') or '', f.get('password2') or '', c)
    if err:
        return _err(err)
    if not first and check_password_hash(acc.password_hash, f.get('password') or ''):
        return _err('הסיסמה החדשה זהה לנוכחית')
    acc.password_hash = generate_password_hash(f['password'])
    acc.session_ver = (acc.session_ver or 0) + 1
    CA.clear_fails(acc)
    db.session.commit()
    CA.mark_session(c, rotate=False)        # המכשיר הזה נשאר מחובר; כל השאר מתנתקים
    return _ok('הסיסמה נשמרה. מעכשיו אפשר להיכנס גם במייל וסיסמה' if first else 'הסיסמה הוחלפה. שאר המכשירים נותקו')


@customer_bp.route('/settings/phone-link', methods=['POST'])
@customer_login_required
def settings_phone_link():
    from services import phone_link as PL
    if not _rate(f'cu-plink:{current_user.id}', 8, 3600):
        return _err('יותר מדי בקשות. נסו שוב בעוד שעה', 429)
    req, err = PL.create(current_user, (request.get_json(silent=True) or {}).get('phone'))
    if err:
        return _err(err)
    return _ok('הבקשה נפתחה', id=req.id, phone=req.phone, minutes=PL.MINUTES)


@customer_bp.route('/settings/phone-link/<int:req_id>')
@customer_login_required
def settings_phone_link_status(req_id):
    from models import PhoneLinkRequest
    from services.customer_phones import all_phones
    req = PhoneLinkRequest.query.filter_by(id=req_id, customer_id=current_user.id).first()
    if not req:
        return _err('לא נמצא', 404)
    status = req.status
    if status == 'pending' and req.expires_at < datetime.utcnow():
        status = 'expired'
    return jsonify({'status': status, 'phones': all_phones(current_user)})


@customer_bp.route('/settings/devices/<int:dev_id>/revoke', methods=['POST'])
@customer_login_required
def settings_device_revoke(dev_id):
    d = CustomerDevice.query.filter_by(id=dev_id, customer_id=current_user.id).first()
    if not d:
        return _err('לא נמצא', 404)
    d.revoked = True
    db.session.commit()
    return _ok('המכשיר הוסר - בכניסה הבאה ממנו יידרש קוד למייל')


@customer_bp.route('/settings/logout-all', methods=['POST'])
@customer_login_required
def settings_logout_all():
    acc = CA.account_for(current_user)
    acc.session_ver = (acc.session_ver or 0) + 1
    cur = CA.device_trusted(current_user)
    q = CustomerDevice.query.filter_by(customer_id=current_user.id, revoked=False)
    if cur:
        q = q.filter(CustomerDevice.id != cur.id)
    q.update({CustomerDevice.revoked: True}, synchronize_session=False)
    db.session.commit()
    CA.mark_session(current_user, rotate=False)
    return _ok('נותקת מכל שאר המכשירים, וכולם יצטרכו קוד למייל בכניסה הבאה')


@customer_bp.route('/settings/google/unlink', methods=['POST'])
@customer_login_required
def settings_google_unlink():
    acc = CA.account_for(current_user)
    if not acc.password_hash:
        return _err('לפני ניתוק Google צריך להגדיר סיסמה - אחרת לא תוכלו להיכנס')
    acc.google_id = None
    acc.google_email = None
    db.session.commit()
    return _ok('חשבון Google נותק')


# --------------------------------------------------------------------------- צור קשר
def _ser_msg(m):
    return {'id': m.id, 'dir': m.direction, 'body': m.body, 'read': bool(m.is_read) if m.direction == 'in' else True,
            'time': _loc(m.created_at, '%H:%M'), 'date': _loc(m.created_at, '%d/%m/%Y')}


@customer_bp.route('/contact', methods=['GET', 'POST'])
@customer_login_required
def contact():
    from models import CustomerMessage, ConversationThread
    c = current_user
    if request.method == 'POST':
        body = ((request.get_json(silent=True) or {}).get('message') or '').strip()[:5000]
        if not body:
            return _err('ההודעה ריקה')
        if not _rate(f'cu-msg:{c.id}', 20, 3600):
            return _err('נשלחו יותר מדי הודעות. נסו שוב מאוחר יותר', 429)
        thread = ConversationThread.query.filter_by(customer_id=c.id).order_by(ConversationThread.created_at.desc()).first()
        if not thread:
            thread = ConversationThread(customer_id=c.id)
            db.session.add(thread)
            db.session.flush()
        m = CustomerMessage(thread_id=thread.id, customer_id=c.id, direction='in', body=body, is_read=False)
        db.session.add(m)
        db.session.commit()
        return jsonify({'ok': True, 'message': _ser_msg(m)})
    msgs = CustomerMessage.query.filter_by(customer_id=c.id).order_by(CustomerMessage.created_at.asc()).limit(300).all()
    acc = CA.account_for(c)
    acc.chat_seen_at = datetime.utcnow()
    db.session.commit()
    return render_template('customer/contact.html', messages=[_ser_msg(m) for m in msgs])


@customer_bp.route('/contact/messages')
@customer_login_required
def contact_messages():
    from models import CustomerMessage
    after = request.args.get('after', type=int) or 0
    msgs = (CustomerMessage.query.filter(CustomerMessage.customer_id == current_user.id, CustomerMessage.id > after)
            .order_by(CustomerMessage.created_at.asc()).limit(100).all())
    if any(m.direction == 'out' for m in msgs):
        acc = CA.account_for(current_user)
        acc.chat_seen_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'messages': [_ser_msg(m) for m in msgs]})


# =========================================================================== משותף עם אזור המוסד
def _register_shared():
    """דפי התמלולים / הנגן / הדוחות / העדכונים - אותן פונקציות של אזור המוסד, מאחורי הכניסה
    של הלקוח. הפונקציות מזהות לקוח מחובר ומציגות רק את ההקלטות שלו."""
    from routes import institution_portal as IP
    rules = [
        ('/t/<kind>/<int:item_id>', 'transcript_page', IP.transcript_page, ['GET']),
        ('/t/<kind>/<int:item_id>/save', 'transcript_save', IP.transcript_save, ['POST']),
        ('/t/<kind>/<int:item_id>/rename', 'transcript_rename', IP.transcript_rename, ['POST']),
        ('/t/<kind>/<int:item_id>/revert', 'transcript_revert', IP.transcript_revert, ['POST']),
        ('/t/<kind>/<int:item_id>/download', 'transcript_download', IP.transcript_download, ['GET']),
        ('/t/<kind>/<int:item_id>/editor', 'transcript_editor', IP.transcript_editor, ['GET']),
        ('/t/<kind>/<int:item_id>/editor/save', 'transcript_editor_save', IP.transcript_editor_save, ['POST']),
        ('/dictate', 'dictate', IP.dictate, ['POST']),
        ('/t/<kind>/<int:item_id>/audio-state', 'audio_state', IP.audio_state, ['GET']),
        ('/t/<kind>/<int:item_id>/audio-chunk/<int:n>', 'audio_chunk', IP.audio_chunk, ['GET']),
        ('/transcripts', 'transcripts', IP.transcripts, ['GET']),
        ('/transcripts/data', 'transcripts_data', IP.transcripts_data, ['GET']),
        ('/transcripts/export', 'transcripts_export', IP.transcripts_export, ['POST']),
        ('/transcripts/organize', 'transcripts_organize', IP.transcripts_organize, ['POST']),
        ('/folders', 'folders_list', IP.folders_list, ['GET']),
        ('/folders', 'folders_save', IP.folders_save, ['POST']),
        ('/reports', 'reports', IP.reports, ['GET']),
        ('/reports/data', 'reports_data', IP.reports_data, ['GET']),
        ('/pulse', 'pulse', IP.pulse, ['GET']),
    ]
    for rule, endpoint, fn, methods in rules:
        raw = getattr(fn, '__wrapped__', None)
        if raw is None:
            raise RuntimeError(f'{endpoint}: missing __wrapped__')
        customer_bp.add_url_rule(rule, endpoint, customer_login_required(raw), methods=methods)


_register_shared()
