from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, make_response
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from app import db, login_manager
from models import Customer, Recording, Transaction, Settings, AdminUser, ManagerMessage, CustomerMessage
from datetime import datetime, timedelta
from sqlalchemy import func
import io
import os
import logging

log = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)

@admin_bp.context_processor
def inject_new_messages_count():
    from models import ManagerMessage, CustomerMessage, GeneralInboxMessage
    try:
        from sqlalchemy import or_, and_, select
        # שלוש הספירות בשאילתה אחת (רצה בכל עמוד - כל פנייה למסד הנתונים עולה זמן)
        mm = select(func.count()).select_from(ManagerMessage).where(
            ManagerMessage.status == 'new',
            or_(
                and_(ManagerMessage.rec_url.isnot(None), ManagerMessage.rec_url != ''),
                ManagerMessage.source != 'ivr',
            )).scalar_subquery()
        cm = select(func.count()).select_from(CustomerMessage).where(
            CustomerMessage.direction == 'in', CustomerMessage.is_read.is_(False)).scalar_subquery()
        gi = select(func.count()).select_from(GeneralInboxMessage).where(
            GeneralInboxMessage.is_read.is_(False)).scalar_subquery()
        count = int(db.session.execute(select(mm + cm + gi)).scalar() or 0)
    except Exception:
        count = 0
    try:
        maintenance_on = get_setting('maintenance_mode', '0') == '1'
    except Exception:
        maintenance_on = False
    return {'new_messages_count': count, 'maintenance_on': maintenance_on}

@login_manager.user_loader
def load_user(user_id):
    # מוסדות מתחברים דרך אותו Flask-Login, אבל עם קידומת 'inst-' ב-get_id
    # (ראה models.Institution.get_id) כדי להבדיל מהתחברות מנהל-על רגילה.
    if isinstance(user_id, str) and user_id.startswith('inst-'):
        from models import Institution
        return Institution.query.get(int(user_id.split('-', 1)[1]))
    return AdminUser.query.get(int(user_id))

def get_setting(key, default=''):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = Settings.query.filter_by(key=key).first()
    if s:
        s.value = value
        s.updated_at = datetime.utcnow()
    else:
        s = Settings(key=key, value=value)
        db.session.add(s)
    db.session.commit()

def _admin_credentials_from_env():
    """שם משתמש וסיסמה של מנהל-העל נקבעים אך ורק במשתני הסביבה (Railway):
    ADMIN_USERNAME (ברירת מחדל: admin) ו-ADMIN_PASSWORD. שום דבר מזה לא נשמר
    או נקרא מהגדרות הניהול / מסד הנתונים."""
    from services.security import admin_credentials
    return admin_credentials()


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        import hmac, time
        from services import security as sec
        if sec.login_blocked():
            flash('יותר מדי ניסיונות כניסה שגויים - נסה שוב בעוד 15 דקות')
            return render_template('admin/login.html'), 429
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        env_user, env_pass = _admin_credentials_from_env()
        if not env_pass:
            log.error('admin login refused: ADMIN_PASSWORD environment variable is not set')
            flash('לא הוגדר משתנה הסביבה ADMIN_PASSWORD בשרת - יש להגדיר אותו ב-Railway')
            return render_template('admin/login.html')
        ok = (hmac.compare_digest(username.encode('utf-8'), env_user.encode('utf-8'))
              and hmac.compare_digest(password.encode('utf-8'), env_pass.encode('utf-8')))
        if ok:
            if sec.google_required():
                # שלב ב': אימות Google, רק מהכתובות שהוגדרו ב-ADMIN_GOOGLE_EMAILS.
                # עד שלב ב' המשתמש עוד לא מחובר - רק סימון זמני ש"הסיסמה עברה".
                if not (os.environ.get('GOOGLE_OAUTH_CLIENT_ID') and os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')):
                    log.error('admin login: ADMIN_GOOGLE_EMAILS is set but GOOGLE_OAUTH_CLIENT_ID/SECRET are missing')
                    flash('נדרש אימות Google אך Google OAuth לא הוגדר בשרת (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET)')
                    return render_template('admin/login.html')
                from flask import session as flask_session
                flask_session.clear()
                flask_session['admin_pw_ok'] = time.time()
                return redirect(url_for('admin.google_start'))
            return _finish_admin_login(env_user)
        sec.register_failed_login()
        log.warning(f'admin login failed from {sec.client_ip()}')
        time.sleep(1)  # האטה קלה נגד ניחושים
        flash('שם משתמש או סיסמה שגויים')
    return render_template('admin/login.html')

def _finish_admin_login(env_user, google_email=None):
    """מסיים כניסה של מנהל אחרי שכל שלבי האימות עברו."""
    from flask import session as flask_session
    from services import security as sec
    # Flask-Login צריך אובייקט משתמש; השורה בטבלה משמשת רק כמזהה (ללא סיסמה)
    user = AdminUser.query.filter_by(username=env_user).first()
    if not user:
        user = AdminUser(username=env_user, password_hash='!env-only')
        db.session.add(user)
        db.session.commit()
    flask_session.clear()
    login_user(user)
    flask_session['admin_fp'] = sec.admin_session_fp()
    if google_email:
        flask_session['admin_google'] = google_email.lower()
    flask_session.permanent = True
    sec.clear_failed_logins()
    return redirect(url_for('admin.dashboard'))


GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
PW_STEP_MAX_AGE = 600  # אחרי שהסיסמה עברה - 10 דקות להשלים את שלב Google


@admin_bp.route('/auth/google/start')
def google_start():
    import secrets, time, urllib.parse
    from flask import session as flask_session
    from services import security as sec
    if not sec.google_required():
        return redirect(url_for('admin.login'))
    if time.time() - float(flask_session.get('admin_pw_ok') or 0) > PW_STEP_MAX_AGE:
        flash('יש להתחיל את הכניסה מחדש עם שם משתמש וסיסמה')
        return redirect(url_for('admin.login'))
    client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
    if not client_id:
        flash('Google OAuth לא הוגדר בשרת')
        return redirect(url_for('admin.login'))
    state = secrets.token_urlsafe(24)
    flask_session['admin_google_state'] = state
    params = {
        'client_id': client_id,
        'redirect_uri': url_for('admin.google_callback', _external=True),
        'response_type': 'code', 'scope': 'openid email', 'state': state,
        'prompt': 'select_account',
    }
    return redirect(f'{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}')


@admin_bp.route('/auth/google/callback')
def google_callback():
    import time, hmac
    import requests as req
    from flask import session as flask_session
    from services import security as sec
    if not sec.google_required():
        return redirect(url_for('admin.login'))
    if sec.login_blocked():
        flash('יותר מדי ניסיונות כניסה שגויים - נסה שוב בעוד 15 דקות')
        return render_template('admin/login.html'), 429
    expected_state = flask_session.pop('admin_google_state', None)
    pw_ok_at = float(flask_session.get('admin_pw_ok') or 0)
    got_state = request.args.get('state') or ''
    if (not expected_state or not hmac.compare_digest(got_state.encode(), expected_state.encode())
            or time.time() - pw_ok_at > PW_STEP_MAX_AGE):
        flask_session.clear()
        flash('אירעה שגיאת אימות, יש להתחיל את הכניסה מחדש')
        return redirect(url_for('admin.login'))
    code = request.args.get('code')
    if not code:
        flask_session.clear()
        flash('ההתחברות עם Google בוטלה')
        return redirect(url_for('admin.login'))
    try:
        token_res = req.post(GOOGLE_TOKEN_URL, data={
            'code': code,
            'client_id': os.environ.get('GOOGLE_OAUTH_CLIENT_ID'),
            'client_secret': os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET'),
            'redirect_uri': url_for('admin.google_callback', _external=True),
            'grant_type': 'authorization_code',
        }, timeout=15)
        token_res.raise_for_status()
        access_token = token_res.json().get('access_token')
        info_res = req.get(GOOGLE_USERINFO_URL, headers={'Authorization': f'Bearer {access_token}'}, timeout=15)
        info_res.raise_for_status()
        info = info_res.json()
    except Exception:
        log.exception('admin google callback: token/userinfo exchange failed')
        flask_session.clear()
        flash('שגיאה באימות Google, נסה שוב')
        return redirect(url_for('admin.login'))
    email = (info.get('email') or '').strip().lower()
    verified = info.get('email_verified') in (True, 'true', 'True')
    if not (email and verified and email in sec.allowed_admin_emails()):
        sec.register_failed_login()
        log.warning(f'admin google login DENIED for {email!r} (verified={verified}) from {sec.client_ip()}')
        flask_session.clear()
        flash('חשבון Google זה אינו מורשה להיכנס לממשק הניהול')
        return redirect(url_for('admin.login'))
    env_user, _ = _admin_credentials_from_env()
    log.info(f'admin login OK via password + google ({email})')
    return _finish_admin_login(env_user, email)


@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    from flask import session as flask_session
    flask_session.clear()
    return redirect(url_for('admin.login'))

# --- אפליקציה מותקנת (PWA): התקנה מכרום/אדג' כתוכנה, חלון נפרד, צפייה בלי חיבור ---
_PWA_ICONS = {}


def _pwa_icon_data(name):
    if name not in _PWA_ICONS:
        import base64
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'pwa', name)
        with open(path, 'rb') as f:
            _PWA_ICONS[name] = 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
    return _PWA_ICONS[name]


@admin_bp.route('/manifest.webmanifest')
def pwa_manifest():
    resp = jsonify({
        'name': 'מערכת תמלול - ניהול', 'short_name': 'מערכת תמלול',
        'description': 'ממשק הניהול של מערכת התמלול',
        'lang': 'he', 'dir': 'rtl',
        'id': '/admin/', 'start_url': '/admin/dashboard', 'scope': '/admin/',
        'display': 'standalone', 'background_color': '#0f1117', 'theme_color': '#1a1d27',
        # הסמלים מוטמעים כטקסט בתוך הקובץ הזה (data:), ולא כקובצי תמונה נפרדים - נטפרי חוסם תמונות
        'icons': [
            {'src': _pwa_icon_data('icon-192.png'), 'sizes': '192x192', 'type': 'image/png'},
            {'src': _pwa_icon_data('icon-512.png'), 'sizes': '512x512', 'type': 'image/png'},
            {'src': _pwa_icon_data('icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    })
    resp.mimetype = 'application/manifest+json'
    return resp


@admin_bp.route('/sw.js')
def pwa_service_worker():
    """מוגש מתוך /admin כדי שהתחום שלו יהיה כל עמודי הניהול (הקובץ עצמו: static/pwa/sw.js)."""
    from flask import send_from_directory
    resp = send_from_directory(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'pwa'),
                               'sw.js', mimetype='application/javascript', max_age=0)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@admin_bp.route('/live-calls')
@login_required
def live_calls():
    """לוח חי בדשבורד: השיחות שעל הקו עכשיו, משרת ה-IVR (/internal/live-calls).
    נקרא מהדפדפן כל כמה שניות - רק כשהדשבורד פתוח וגלוי."""
    import requests
    ivr_url = os.environ.get('NODE_IVR_URL', '').rstrip('/')
    secret = os.environ.get('INTERNAL_PROXY_SECRET', '')
    if not (ivr_url and secret):
        return jsonify({'ok': False, 'error': 'NODE_IVR_URL / INTERNAL_PROXY_SECRET לא מוגדרים'})
    try:
        r = requests.get(f'{ivr_url}/internal/live-calls', headers={'X-Internal-Secret': secret}, timeout=5)
        r.raise_for_status()
        calls = r.json().get('calls', [])
    except Exception as e:
        return jsonify({'ok': False, 'error': f'שרת ה-IVR לא ענה ({type(e).__name__})'})
    phones = {c.get('phone') for c in calls if c.get('phone')}
    by_phone = {}
    if phones:
        for cu in Customer.query.filter(Customer.phone.in_(phones)).all():
            by_phone[cu.phone] = cu
    for c in calls:
        cu = by_phone.get(c.get('phone'))
        c['name'] = (cu.name or '') if cu else ''
        c['customer_id'] = cu.id if cu else None
    return jsonify({'ok': True, 'calls': calls})


@admin_bp.route('/live-count')
@login_required
def live_count():
    """מספר השיחות החיות בלבד - לעיגול ליד "דשבורד" בתפריט הצד (כל 10 שניות)."""
    import requests
    ivr_url = os.environ.get('NODE_IVR_URL', '').rstrip('/')
    secret = os.environ.get('INTERNAL_PROXY_SECRET', '')
    if not (ivr_url and secret):
        return jsonify({'ok': False})
    try:
        r = requests.get(f'{ivr_url}/internal/live-calls', headers={'X-Internal-Secret': secret}, timeout=4)
        return jsonify({'ok': True, 'count': len(r.json().get('calls', []))})
    except Exception:
        return jsonify({'ok': False})


@admin_bp.route('/live-calls/action', methods=['POST'])
@login_required
def live_call_action():
    """העברת מתקשר מהלוח החי: לתפריט / לאפשרות בתפריט הראשי / לשלוחה בימות / ניתוק."""
    import requests
    ivr_url = os.environ.get('NODE_IVR_URL', '').rstrip('/')
    secret = os.environ.get('INTERNAL_PROXY_SECRET', '')
    if not (ivr_url and secret):
        return jsonify({'ok': False, 'error': 'NODE_IVR_URL / INTERNAL_PROXY_SECRET לא מוגדרים'})
    data = request.get_json(silent=True) or {}
    payload = {'id': str(data.get('id', '')), 'type': str(data.get('type', '')),
               'target': str(data.get('target', ''))[:40]}
    try:
        r = requests.post(f'{ivr_url}/internal/live-action', json=payload,
                          headers={'X-Internal-Secret': secret}, timeout=25)
        out = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'שרת ה-IVR לא ענה ({type(e).__name__})'})
    logging.getLogger(__name__).info(f"live-call action by {getattr(current_user, 'username', '?')}: {payload} -> {out}")
    return jsonify(out)


@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def dashboard():
    from datetime import timedelta
    today = datetime.utcnow().date()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    one_month_ago = datetime.utcnow() - timedelta(days=30)
    one_year_ago = datetime.utcnow() - timedelta(days=365)

    # סטטיסטיקות כלליות
    stats = {
        'total_customers': Customer.query.count(),
        'active_customers': Customer.query.filter_by(is_blocked=False).count(),
        'blocked_customers': Customer.query.filter_by(is_blocked=True).count(),
        'total_recordings': Recording.query.count(),
        'today_recordings': Recording.query.filter(
            Recording.created_at >= today_start
        ).count(),
        'month_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge',
            Transaction.created_at >= month_start
        ).scalar() or 0,
        'total_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge'
        ).scalar() or 0,
        'total_balance': db.session.query(func.sum(Customer.balance)).scalar() or 0,
        # לקוחות לא פעילים
        'inactive_month': Customer.query.filter(
            ~Customer.id.in_(
                db.session.query(Recording.customer_id).filter(
                    Recording.created_at >= one_month_ago
                )
            )
        ).count(),
        'inactive_year': Customer.query.filter(
            ~Customer.id.in_(
                db.session.query(Recording.customer_id).filter(
                    Recording.created_at >= one_year_ago
                )
            )
        ).count(),
    }

    # חיובי ארנקות היום — לפי סוג, ערוץ, ושיטת שליחה
    today_debits = Recording.query.options(*_light(Recording)).filter(
        Recording.created_at >= today_start,
        Recording.status == 'delivered',
        Recording.cost > 0
    ).all()

    # סטטיסטיקות OCR היום
    from models import OcrResult
    today_ocr = OcrResult.query.options(*_light(OcrResult)).filter(
        OcrResult.created_at >= today_start,
        OcrResult.status == 'completed'
    ).all()
    ocr_stats = {
        'count': len(today_ocr),
        'total_cost': sum(r.cost or 0 for r in today_ocr),
        'total_chars': sum(r.char_count or 0 for r in today_ocr),
    }

    billing_stats = {
        'total_cost': sum(r.cost or 0 for r in today_debits),
        'count': len(today_debits),
        # לפי סוג תמלול
        'basic_cost': sum(r.cost or 0 for r in today_debits if (r.transcription_tier or 'gemini') not in ('premium', 'video')),
        'premium_cost': sum(r.cost or 0 for r in today_debits if r.transcription_tier == 'premium'),
        'video_cost': sum(r.cost or 0 for r in today_debits if r.transcription_tier == 'video'),
        'basic_count': sum(1 for r in today_debits if (r.transcription_tier or 'gemini') not in ('premium', 'video')),
        'premium_count': sum(1 for r in today_debits if r.transcription_tier == 'premium'),
        'video_count': sum(1 for r in today_debits if r.transcription_tier == 'video'),
        # לפי ערוץ
        'phone_cost': sum(r.cost or 0 for r in today_debits if not (r.call_id or '').startswith('email-')),
        'email_cost': sum(r.cost or 0 for r in today_debits if (r.call_id or '').startswith('email-')),
        'phone_count': sum(1 for r in today_debits if not (r.call_id or '').startswith('email-')),
        'email_count': sum(1 for r in today_debits if (r.call_id or '').startswith('email-')),
        # לפי שיטת שליחה
        'sent_email_count': sum(1 for r in today_debits if r.delivery_method == 'email'),
        'sent_fax_count': sum(1 for r in today_debits if r.delivery_method == 'fax'),
    }

    from sqlalchemy.orm import joinedload
    recent_recordings = Recording.query.options(*_light(Recording), joinedload(Recording.customer)) \
        .order_by(Recording.created_at.desc()).limit(10).all()
    recent_transactions = Transaction.query.options(joinedload(Transaction.customer)) \
        .order_by(Transaction.created_at.desc()).limit(10).all()

    pending_stats = {
        'recordings': Recording.query.filter_by(status='pending_payment').count(),
        'ocr': OcrResult.query.filter_by(status='pending_payment').count(),
    }
    pending_stats['total'] = pending_stats['recordings'] + pending_stats['ocr']

    return render_template('admin/dashboard.html',
        stats=stats,
        billing_stats=billing_stats,
        ocr_stats=ocr_stats,
        pending_stats=pending_stats,
        recent_recordings=recent_recordings,
        recent_transactions=recent_transactions
    )

@admin_bp.route('/customers')
@login_required
def customers():
    from sqlalchemy import case, or_

    search = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    sort = request.args.get('sort', '')

    # תתי-שאילתות מצטברות (לא N+1) - מספר/סכום טעינות (charge) ומספר הקלטות, לכל לקוח
    load_sq = db.session.query(
        Transaction.customer_id.label('cid'),
        func.count(Transaction.id).label('load_count'),
        func.sum(Transaction.amount).label('load_total')
    ).filter(Transaction.type == 'charge').group_by(Transaction.customer_id).subquery()

    rec_sq = db.session.query(
        Recording.customer_id.label('cid'),
        func.count(Recording.id).label('rec_count')
    ).group_by(Recording.customer_id).subquery()

    load_count_col = func.coalesce(load_sq.c.load_count, 0)
    load_total_col = func.coalesce(load_sq.c.load_total, 0)
    rec_count_col = func.coalesce(rec_sq.c.rec_count, 0)
    has_contact_col = case(
        (or_(
            (Customer.email.isnot(None)) & (Customer.email != ''),
            (Customer.fax.isnot(None)) & (Customer.fax != '')
        ), 1),
        else_=0
    )

    query = Customer.query \
        .outerjoin(load_sq, Customer.id == load_sq.c.cid) \
        .outerjoin(rec_sq, Customer.id == rec_sq.c.cid) \
        .add_columns(load_count_col, load_total_col, rec_count_col)

    if search:
        from models import CustomerPhone
        _extra_ids = db.session.query(CustomerPhone.customer_id).filter(
            CustomerPhone.phone.contains(search) | CustomerPhone.email.contains(search))
        query = query.filter(
            Customer.phone.contains(search) |
            Customer.name.contains(search) |
            Customer.email.contains(search) |
            Customer.id.in_(_extra_ids)
        )

    sort_map = {
        'contact': [has_contact_col.desc(), Customer.created_at.desc()],
        'ever_loaded': [(load_count_col > 0).desc(), load_total_col.desc()],
        'balance': [Customer.balance.desc()],
        'most_used': [rec_count_col.desc()],
        'load_count': [load_count_col.desc()],
    }
    query = query.order_by(*sort_map.get(sort, [Customer.created_at.desc()]))

    paginated = query.paginate(page=page, per_page=50)

    # חיפוש מהסרגל הצדדי (go=1): תוצאה אחת בלבד - נכנסים ישר לכרטיס הלקוח
    if search and request.args.get('go') and paginated.total == 1:
        return redirect(url_for('admin.customer_detail', id=paginated.items[0][0].id))

    customers_list = [row[0] for row in paginated.items]
    recording_counts = {row[0].id: row[3] for row in paginated.items}
    load_counts = {row[0].id: row[1] for row in paginated.items}
    load_totals = {row[0].id: float(row[2]) for row in paginated.items}

    return render_template('admin/customers.html',
        customers=paginated, customers_list=customers_list, search=search,
        recording_counts=recording_counts, load_counts=load_counts, load_totals=load_totals,
        current_sort=sort)

@admin_bp.route('/customers/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        fax = request.form.get('fax', '').strip()
        balance = float(request.form.get('balance', 0) or 0)
        delivery_method = request.form.get('delivery_method', 'email')

        if not phone:
            flash('מספר טלפון הוא שדה חובה')
            return render_template('admin/add_customer.html')

        from services.customer_phones import find_customer_by_phone
        existing = find_customer_by_phone(phone)
        if existing:
            flash('לקוח עם מספר טלפון זה כבר קיים')
            return render_template('admin/add_customer.html')

        customer = Customer(
            phone=phone,
            name=name,
            email=email,
            fax=fax,
            balance=balance,
            delivery_method=delivery_method
        )
        db.session.add(customer)
        db.session.commit()
        from services.welcome_email import welcome_if_new
        welcome_if_new(None, email, name, phone, balance)

        if balance > 0:
            txn = Transaction(
                customer_id=customer.id,
                amount=balance,
                type='credit',
                description='יתרה ראשונית'
            )
            db.session.add(txn)
            db.session.commit()

        flash(f'לקוח {phone} נוסף בהצלחה')
        return redirect(url_for('admin.customer_detail', id=customer.id))

    return render_template('admin/add_customer.html')

@admin_bp.route('/customers/export/excel')
@login_required
def export_customers_excel():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'לקוחות'

    headers = ['ID', 'טלפון', 'שם', 'מייל', 'פקס', 'יתרה', 'שיטת משלוח', 'חסום', 'תאריך הצטרפות']
    ws.append(headers)

    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2563EB', end_color='2563EB', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')

    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    for c in customers:
        ws.append([
            c.id,
            c.phone,
            c.name or '',
            c.email or '',
            c.fax or '',
            round(c.balance, 2),
            c.delivery_method or '',
            'כן' if c.is_blocked else 'לא',
            c.created_at.strftime('%d/%m/%Y %H:%M') if c.created_at else ''
        ])

    for col in ws.columns:
        max_length = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max(max_length + 2, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'customers_{datetime.now().strftime("%Y%m%d")}.xlsx'
    )

def _light(model, keep=()):
    """אפשרויות שאילתה שלא טוענות עמודות כבדות (קבצים, JSON, טקסט ארוך) - לעמודי רשימה.
    העמודה נטענת רק אם ניגשים אליה בפועל (שאילתה קטנה נוספת), כך ששום דבר לא נשבר."""
    from sqlalchemy.orm import defer
    from sqlalchemy import LargeBinary, Text, JSON
    opts = []
    for col in model.__table__.columns:
        if col.key in keep:
            continue
        if isinstance(col.type, (LargeBinary, Text, JSON)) or col.type.__class__.__name__ in ('JSONB',):
            opts.append(defer(getattr(model, col.key)))
    return opts


def _excel_templates_for(customer):
    """תבניות אקסל של הלקוח לתצוגה בכרטיס (שלוחה 1 ← 3)."""
    try:
        from models import ExcelTemplate
        from services.excel_tables import template_columns, template_rows
        out = []
        for t in ExcelTemplate.query.filter_by(customer_id=customer.id).order_by(ExcelTemplate.id).all():
            out.append({'id': t.id, 'name': t.name or '(השם עוד לא תומלל)', 'columns': template_columns(t),
                        'rows': len(template_rows(t)), 'updated': t.rows_updated_at or t.updated_at})
        return out
    except Exception as e:
        logging.getLogger(__name__).error(f'excel templates for customer {customer.id}: {e}')
        return []


@admin_bp.route('/customers/<int:id>/cost-report')
@login_required
def customer_cost_report(id):
    """הורדת דוח עלויות (Excel) - ?from=YYYY-MM-DD&to=YYYY-MM-DD, ריק = מההתחלה / עד היום."""
    from services import cost_report as CR
    customer = Customer.query.get_or_404(id)
    f, t = request.args.get('from') or None, request.args.get('to') or None
    data, _totals = CR.build_xlsx(customer, CR.load_transactions(id, f, t), f, t)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=CR.filename(customer),
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _customer_defaults(customer):
    """ברירת המחדל להקלטה (שלוחה 4) - נשמרת לפעמים כטקסט JSON ולפעמים כאובייקט."""
    import json
    ds = customer.default_settings
    if isinstance(ds, str):
        try:
            ds = json.loads(ds)
        except ValueError:
            ds = {}
    ds = ds if isinstance(ds, dict) else {}
    return {'tier': 'premium' if ds.get('tier') == 'premium' else 'gemini',
            'language': ds.get('language') if ds.get('language') in ('he', 'yi', 'en') else 'he',
            'output_language': ds.get('output_language') or 'he',
            'is_set': bool(ds)}


@admin_bp.route('/customers/<int:id>/defaults', methods=['POST'])
@login_required
def customer_defaults_save(id):
    """עריכת ברירת המחדל להקלטה מהממשק - אותם ערכים בדיוק כמו בשלוחה 4 בטלפון."""
    customer = Customer.query.get_or_404(id)
    tier = 'premium' if request.form.get('tier') == 'premium' else 'gemini'
    if tier == 'premium':
        new = {'tier': 'premium', 'output_language': 'original' if request.form.get('output_language') == 'original' else 'he'}
        old = _customer_defaults(customer)
        new['language'] = old['language']
    else:
        lang = request.form.get('language') if request.form.get('language') in ('he', 'yi', 'en') else 'he'
        out = request.form.get('output_language')
        out = out if (lang != 'he' and out in (lang, 'he')) else ('he' if lang == 'he' else lang)
        new = {'tier': 'gemini', 'language': lang, 'output_language': out}
    customer.default_settings = new
    db.session.commit()
    flash('✔ ברירת המחדל להקלטה עודכנה')
    return redirect(url_for('admin.customer_detail', id=id) + '#defaults')


def _voice_messages_for(customer_id):
    """הודעות קוליות לתצוגה בממשק (customer_id=None - תפוצתיות), עם מי שמע."""
    from models import VoiceMessage, VoiceMessageHeard
    q = VoiceMessage.query.options(*_light(VoiceMessage, keep=('text',)))
    q = q.filter(VoiceMessage.customer_id == customer_id) if customer_id else q.filter(VoiceMessage.customer_id.is_(None))
    msgs = q.order_by(VoiceMessage.created_at.desc()).limit(50).all()
    out = []
    for m in msgs:
        heard = VoiceMessageHeard.query.filter_by(message_id=m.id)
        first = heard.order_by(VoiceMessageHeard.heard_at).first() if customer_id else None
        out.append({'m': m, 'heard_count': heard.count(), 'heard_at': first.heard_at if first else None})
    return out


@admin_bp.route('/voice-messages/create', methods=['POST'])
@login_required
def voice_message_create():
    from services import voice_messages as VM
    cid = request.form.get('customer_id', '')
    customer_id = int(cid) if cid.isdigit() else None
    back = (url_for('admin.customer_detail', id=customer_id) + '#voice' if customer_id
            else url_for('admin.broadcast_message') + '#voice')
    source = request.form.get('source')
    audio, filename = None, ''
    if source in ('upload', 'record'):
        f = request.files.get('audio_record' if source == 'record' else 'audio_file')
        if f and f.filename:
            audio, filename = f.read(), f.filename
    try:
        m = VM.create(customer_id, 'tts' if source == 'tts' else 'file',
                      text=request.form.get('text', ''), audio=audio, filename=filename)
        flash(f'✔ ההודעה הקולית #{m.id} נשמרה - ' +
              ('הלקוח ישמע אותה בכניסה הבאה למערכת' if customer_id else 'כל הלקוחות ישמעו אותה בכניסה הבאה למערכת'))
    except Exception as e:
        flash(f'⚠ ההודעה לא נשמרה: {e}')
    return redirect(back)


@admin_bp.route('/voice-messages/<int:mid>/toggle', methods=['POST'])
@login_required
def voice_message_toggle(mid):
    from models import VoiceMessage
    m = VoiceMessage.query.get_or_404(mid)
    m.active = not m.active
    db.session.commit()
    flash('ההודעה ' + ('הופעלה' if m.active else 'הושבתה - לא תושמע יותר'))
    return redirect((url_for('admin.customer_detail', id=m.customer_id) if m.customer_id else url_for('admin.broadcast_message')) + '#voice')


@admin_bp.route('/voice-messages/<int:mid>/delete', methods=['POST'])
@login_required
def voice_message_delete(mid):
    from models import VoiceMessage, VoiceMessageHeard
    m = VoiceMessage.query.get_or_404(mid)
    back = (url_for('admin.customer_detail', id=m.customer_id) if m.customer_id else url_for('admin.broadcast_message')) + '#voice'
    VoiceMessageHeard.query.filter_by(message_id=m.id).delete()
    db.session.delete(m)
    db.session.commit()
    flash('ההודעה הקולית נמחקה')
    return redirect(back)


@admin_bp.route('/voice-messages/<int:mid>/listen')
@login_required
def voice_message_listen(mid):
    """השמעה בממשק: מימות (נטפרי חוסמת שמע מהאתר שלנו), עם המפתח המוגבל להורדה בלבד."""
    from models import VoiceMessage
    from routes.recording_proxy import _YEMOT_DOWNLOAD_API_KEY
    m = VoiceMessage.query.get_or_404(mid)
    if m.yemot_path and _YEMOT_DOWNLOAD_API_KEY:
        from urllib.parse import quote
        return redirect('https://www.call2all.co.il/ym/api/DownloadFile?token='
                        f'{quote(_YEMOT_DOWNLOAD_API_KEY, safe="")}&path={quote("ivr2:" + m.yemot_path, safe="")}')
    if not m.file_data:
        return 'אין קובץ שמע', 404
    from routes.download_utils import plain_audio_response
    return plain_audio_response(m.file_data, 'audio/wav', f'הודעה_{m.id}.wav', 'message.wav')


@admin_bp.route('/customers/<int:id>/cost-report/email', methods=['POST'])
@login_required
def customer_cost_report_email(id):
    from services import cost_report as CR
    customer = Customer.query.get_or_404(id)
    to = (request.form.get('to_email') or customer.email or '').strip()
    if not to or '@' not in to:
        flash('אין ללקוח כתובת מייל - הדוח לא נשלח')
        return redirect(url_for('admin.customer_detail', id=id) + '#cost-report')
    f, t = request.form.get('from') or None, request.form.get('to') or None
    txns = CR.load_transactions(id, f, t)
    data, totals = CR.build_xlsx(customer, txns, f, t)
    try:
        CR.send_email(to, customer, data, totals, CR.period_label(txns, f, t))
        flash(f'דוח העלויות נשלח ל-{to}')
    except Exception as e:
        logging.getLogger(__name__).error(f'cost report email failed (customer {id}): {e}')
        flash(f'שליחת הדוח נכשלה: {e}')
    return redirect(url_for('admin.customer_detail', id=id) + '#cost-report')


@admin_bp.route('/customers/<int:id>/excel/<int:tid>')
@login_required
def customer_excel_download(id, tid):
    """הורדת הטבלה המצטברת הנוכחית של תבנית (כמו שהלקוח יקבל בהקלטה הבאה ב"המשך")."""
    from models import ExcelTemplate
    from services import excel_tables as X
    t = ExcelTemplate.query.get_or_404(tid)
    if t.customer_id != id:
        return 'not found', 404
    data = X.build_xlsx(t.name or 'טבלה', X.template_columns(t), X.template_rows(t))
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f'{X.safe_filename(t.name)}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@admin_bp.route('/customers/<int:id>')
@login_required
def customer_detail(id):
    from models import OcrResult, ConversationThread, ManuscriptPage, ProofingRound, Institution
    customer = Customer.query.get_or_404(id)
    # בלי עמודות כבדות (קבצים/תמלולים/JSON) - העמוד מציג רק נתוני רשימה
    # לקוח עם אלפי הקלטות/עסקאות - מציגים את האחרונות בלבד (מהירות), עם "הצג הכל"
    show_all = request.args.get('all') == '1'
    LIST_LIMIT = None if show_all else 150
    rec_q = Recording.query.options(*_light(Recording)).filter_by(customer_id=id).order_by(Recording.created_at.desc())
    txn_q = Transaction.query.filter_by(customer_id=id).order_by(Transaction.created_at.desc())
    recordings = rec_q.limit(LIST_LIMIT).all() if LIST_LIMIT else rec_q.all()
    transactions = txn_q.limit(LIST_LIMIT).all() if LIST_LIMIT else txn_q.all()
    recordings_total = Recording.query.filter_by(customer_id=id).count() if LIST_LIMIT and len(recordings) >= LIST_LIMIT else len(recordings)
    transactions_total = Transaction.query.filter_by(customer_id=id).count() if LIST_LIMIT and len(transactions) >= LIST_LIMIT else len(transactions)
    ocr_results = OcrResult.query.options(*_light(OcrResult)).filter_by(customer_id=id).order_by(OcrResult.created_at.desc()).all()
    manuscript_pages = ManuscriptPage.query.options(*_light(ManuscriptPage)).filter_by(customer_id=id).order_by(ManuscriptPage.created_at.desc()).all()
    # כל סבבי ההגהה ששייכים לכל כתבי-היד של הלקוח הזה (יכול להיות כמה סבבים
    # על אותו כתב-יד - ראה models.ProofingRound) - כל שורה מסמנת בבירור
    # לאיזה הקראה מקורית היא שייכת (r.manuscript_page), כדי להבדיל בפרופיל
    # הלקוח בין עלות ההקראה הראשונית לבין עלות ההגהה.
    proofing_rounds = (ProofingRound.query.options(*_light(ProofingRound)).join(ManuscriptPage)
                        .filter(ManuscriptPage.customer_id == id)
                        .order_by(ProofingRound.requested_at.desc()).all())
    threads = ConversationThread.query.filter_by(customer_id=id).order_by(ConversationThread.created_at.desc()).all()
    # צפייה בעמוד מסמנת הודעות נכנסות כ"נקראו" בכל השיחות - כדי שהתראה בעמוד הודעות למנהל תיעלם
    unread = [m for t in threads for m in t.messages if m.direction == 'in' and not m.is_read]
    # טעינה מוקדמת בריחוף (Turbo prefetch) - הלקוח עוד לא נפתח בפועל, לא מסמנים כנקרא
    if unread and request.headers.get('X-Sec-Purpose') != 'prefetch':
        for m in unread:
            m.is_read = True
        db.session.commit()
    return render_template('admin/customer_detail.html',
        customer=customer, recordings=recordings, transactions=transactions,
        ocr_results=ocr_results, manuscript_pages=manuscript_pages, proofing_rounds=proofing_rounds,
        threads=threads, timedelta=timedelta,
        excel_templates=_excel_templates_for(customer),
        cust_defaults=_customer_defaults(customer),
        voice_messages=_voice_messages_for(customer.id),
        recordings_total=recordings_total, transactions_total=transactions_total,
        msg_attachments=_attachments_for('customer_message_id', [m.id for t in threads for m in t.messages]),
        all_institutions=Institution.query.order_by(Institution.name).all())

@admin_bp.route('/customers/<int:id>/link-institution', methods=['POST'])
@login_required
def force_link_institution(id):
    """מנהל-העל הופך לקוח לתלמיד של מוסד - מיד, בלי בקשת אישור ללקוח."""
    from models import Institution
    from services.student_links import admin_force_link
    customer = Customer.query.get_or_404(id)
    inst = Institution.query.get(request.form.get('institution_id', type=int) or 0)
    ok, msg = admin_force_link(inst, customer, request.form.get('display_name'))
    flash(('✅ ' if ok else '⚠️ ') + msg)
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/block', methods=['POST'])
@login_required
def block_customer(id):
    customer = Customer.query.get_or_404(id)
    customer.is_blocked = not customer.is_blocked
    db.session.commit()
    status = 'נחסם' if customer.is_blocked else 'בוטל חסם'
    flash(f'לקוח {status} בהצלחה')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/delete', methods=['POST'])
@login_required
def delete_customer(id):
    from models import OcrResult
    customer = Customer.query.get_or_404(id)
    Transaction.query.filter_by(customer_id=id).delete()
    Recording.query.filter_by(customer_id=id).delete()
    OcrResult.query.filter_by(customer_id=id).delete()
    db.session.delete(customer)
    db.session.commit()
    flash(f'לקוח {customer.phone} נמחק בהצלחה')
    return redirect(url_for('admin.customers'))

@admin_bp.route('/customers/<int:id>/credit', methods=['POST'])
@login_required
def credit_customer(id):
    customer = Customer.query.get_or_404(id)
    amount = float(request.form.get('amount', 0))
    reason = request.form.get('reason', 'זיכוי ידני')
    if amount > 0:
        customer.balance = round(customer.balance + amount, 2)
        txn = Transaction(
            customer_id=id,
            amount=amount,
            type='credit',
            description=reason
        )
        db.session.add(txn)
        db.session.commit()
        try:
            from routes.dictate import trigger_pending_manuscripts
            trigger_pending_manuscripts(customer_id=id)
        except Exception:
            pass
        flash(f'לקוח זוכה ב-{amount:.2f} ₪')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/charge', methods=['POST'])
@login_required
def charge_customer(id):
    customer = Customer.query.get_or_404(id)
    amount = float(request.form.get('amount', 0))
    reason = request.form.get('reason', 'חיוב ידני')
    if amount > 0:
        customer.balance = round(customer.balance - amount, 2)
        txn = Transaction(
            customer_id=id,
            amount=-amount,
            type='debit',
            description=reason
        )
        db.session.add(txn)
        db.session.commit()
        flash(f'לקוח חויב ב-{amount:.2f} ₪')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/update', methods=['POST'])
@login_required
def update_customer(id):
    customer = Customer.query.get_or_404(id)
    old_email = customer.email
    customer.name = request.form.get('name', customer.name)
    customer.email = request.form.get('email', customer.email)
    customer.fax = request.form.get('fax', customer.fax)
    customer.delivery_method = request.form.get('delivery_method', customer.delivery_method)
    db.session.commit()
    from services.welcome_email import welcome_if_new
    sent = welcome_if_new(old_email, customer.email, customer.name, customer.phone, customer.balance)
    flash('פרטי לקוח עודכנו' + (' - נשלח מייל "ברוכים הבאים" לכתובת החדשה' if sent else ''))
    return redirect(url_for('admin.customer_detail', id=id))

# ---------------------------------------------------------------------
# מספרי טלפון נוספים על לקוח אחד (חשבון חיוב משותף - למשל בעל ואשה)
# ---------------------------------------------------------------------
@admin_bp.route('/customers/<int:id>/phones/add', methods=['POST'])
@login_required
def add_customer_phone(id):
    from services.customer_phones import add_phone
    customer = Customer.query.get_or_404(id)
    ok, msg = add_phone(customer, request.form.get('phone', ''), request.form.get('label'),
                        request.form.get('email'), request.form.get('fax'))
    if ok:
        from services.welcome_email import welcome_if_new
        from services.customer_phones import normalize_phone
        if welcome_if_new(None, request.form.get('email'), customer.name,
                          normalize_phone(request.form.get('phone', '')), customer.balance):
            msg += ' - נשלח מייל "ברוכים הבאים"'
    flash(msg)
    return redirect(url_for('admin.customer_detail', id=id) + '#extra-phones')


@admin_bp.route('/customers/<int:id>/phones/<int:phone_id>/update', methods=['POST'])
@login_required
def update_customer_phone(id, phone_id):
    from models import CustomerPhone
    row = CustomerPhone.query.filter_by(id=phone_id, customer_id=id).first_or_404()
    old_email = row.email
    row.label = (request.form.get('label') or '').strip() or None
    row.email = (request.form.get('email') or '').strip() or None
    row.fax = (request.form.get('fax') or '').strip() or None
    dm = request.form.get('delivery_method') or ''
    row.delivery_method = dm if dm in ('email', 'fax') else None
    db.session.commit()
    from services.welcome_email import welcome_if_new
    sent = welcome_if_new(old_email, row.email, row.customer.name if row.customer else None, row.phone,
                          row.customer.balance if row.customer else 0)
    flash('פרטי המספר עודכנו' + (' - נשלח מייל "ברוכים הבאים" לכתובת החדשה' if sent else ''))
    return redirect(url_for('admin.customer_detail', id=id) + '#extra-phones')


@admin_bp.route('/customers/<int:id>/phones/<int:phone_id>/delete', methods=['POST'])
@login_required
def delete_customer_phone(id, phone_id):
    from models import CustomerPhone
    row = CustomerPhone.query.filter_by(id=phone_id, customer_id=id).first_or_404()
    phone = row.phone
    db.session.delete(row)
    db.session.commit()
    flash(f'המספר {phone} הוסר מהלקוח')
    return redirect(url_for('admin.customer_detail', id=id) + '#extra-phones')


@admin_bp.route('/customers/<int:id>/phones/mode', methods=['POST'])
@login_required
def set_customer_phones_mode(id):
    customer = Customer.query.get_or_404(id)
    customer.phones_separate = request.form.get('separate') == '1'
    db.session.commit()
    flash('מצב המשלוח עודכן: ' + ('מייל/פקס נפרד לכל מספר' if customer.phones_separate else 'כל התמלולים למייל/פקס של הלקוח'))
    return redirect(url_for('admin.customer_detail', id=id) + '#extra-phones')


@admin_bp.route('/customers/<int:id>/send-recordings', methods=['POST'])
@login_required
def send_recordings(id):
    import os
    from services.transcribe import _send_email, _send_fax
    customer = Customer.query.get_or_404(id)
    recording_ids = request.form.getlist('recording_ids')
    send_method = request.form.get('send_method', 'email')
    send_to = request.form.get('send_to', '').strip()

    if not recording_ids:
        flash('לא נבחרו הקלטות')
        return redirect(url_for('admin.customer_detail', id=id))

    if not send_to:
        flash('יש להזין כתובת מייל או מספר פקס')
        return redirect(url_for('admin.customer_detail', id=id))

    sent = 0
    for rec_id in recording_ids:
        rec = Recording.query.get(int(rec_id))
        if not rec or not rec.transcript:
            continue
        try:
            from routes.recording_proxy import recording_download_url
            rec_url = recording_download_url(rec.id)
            if rec.transcription_tier == 'excel' and rec.excel_file:
                # טבלת אקסל - שולחים את הטבלה עצמה (מייל: קובץ אקסל, פקס: הטבלה כ-PDF), לא תמלול
                from services import excel_tables as X
                title = _excel_template_name(rec) or 'טבלה'
                if send_method == 'email':
                    X.send_excel_email(send_to, customer, title, rec.excel_file, 0, 0, (rec.duration_seconds or 0) // 60)
                else:
                    res = X.send_excel_fax(send_to, rec.excel_file, f'table_resend_{rec.id}.pdf')
                    if not res.get('ok'):
                        raise RuntimeError(res.get('error') or 'שליחת הפקס נכשלה')
            elif send_method == 'email':
                _send_email(send_to, rec.transcript, customer, rec_url, rec.duration_seconds, source_filename=rec.source_filename, is_premium=(rec.transcription_tier == 'premium'))
            else:
                _send_fax(send_to, rec.transcript, customer, rec.duration_seconds)
            sent += 1
        except Exception as e:
            flash(f'שגיאה בשליחת הקלטה {rec_id}: {e}')

    flash(f'נשלחו {sent} הקלטות בהצלחה')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/send-message', methods=['POST'])
@login_required
def send_customer_message(id):
    """שליחת הודעה ללקוח במייל, מתוך חשבונו בממשק הניהול. אם נשלח thread_id -
    ממשיכים שיחה קיימת (שרשור אמיתי, In-Reply-To/References). אם לא (או
    thread_id='new') - נפתחת שיחה חדשה ונפרדת, שתוצג בממשק בנפרד מהשיחות
    האחרות. השמירה ב-DB מיידית (מהירה); שליחת המייל בפועל רצה ברקע."""
    import threading, uuid as _uuid
    from models import CustomerMessage, ConversationThread
    customer = Customer.query.get_or_404(id)
    body = (request.form.get('body') or '').strip()
    thread_id = request.form.get('thread_id') or ''

    if not body:
        flash('יש להזין תוכן להודעה')
        return redirect(url_for('admin.customer_detail', id=id))

    if not (customer.email or '').strip():
        flash('ללקוח אין כתובת מייל רשומה')
        return redirect(url_for('admin.customer_detail', id=id))

    if thread_id and thread_id != 'new':
        thread = ConversationThread.query.filter_by(id=int(thread_id), customer_id=id).first()
        if not thread:
            flash('השיחה לא נמצאה')
            return redirect(url_for('admin.customer_detail', id=id))
    else:
        thread = ConversationThread(customer_id=id)
        db.session.add(thread)
        db.session.flush()  # כדי לקבל thread.id לפני commit

    # שרשור אמיתי בתוך אותה שיחה: מוצאים את ה-Message-ID של ההודעה היוצאת
    # האחרונה בשיחה הזו (אם יש), כדי לצרף In-Reply-To/References.
    prev_out = CustomerMessage.query.filter_by(thread_id=thread.id, direction='out') \
        .filter(CustomerMessage.message_id.isnot(None)) \
        .order_by(CustomerMessage.created_at.desc()).first()
    domain = (os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@tamlulphone.co.il')).split('@')[-1]
    new_message_id = f"<conv-{thread.id}-{_uuid.uuid4().hex}@{domain}>"

    msg = CustomerMessage(thread_id=thread.id, customer_id=id, direction='out', body=body,
                           is_read=True, message_id=new_message_id)
    db.session.add(msg)
    db.session.commit()

    in_reply_to = prev_out.message_id if prev_out else None
    threading.Thread(
        target=_send_customer_conversation_email,
        args=(customer.email, customer.phone, body, new_message_id, in_reply_to),
        daemon=True,
    ).start()

    flash('ההודעה נשלחת...')
    return redirect(url_for('admin.customer_detail', id=id))


@admin_bp.route('/customers/<int:id>/threads/<int:thread_id>/delete', methods=['POST'])
@login_required
def delete_conversation_thread(id, thread_id):
    from models import ConversationThread, CustomerMessage
    thread = ConversationThread.query.filter_by(id=thread_id, customer_id=id).first_or_404()
    from models import MessageAttachment
    ids = [m.id for m in CustomerMessage.query.filter_by(thread_id=thread.id).all()]
    if ids:
        MessageAttachment.query.filter(MessageAttachment.customer_message_id.in_(ids)).delete(synchronize_session=False)
    CustomerMessage.query.filter_by(thread_id=thread.id).delete()
    db.session.delete(thread)
    db.session.commit()
    flash('השיחה נמחקה')
    return redirect(url_for('admin.customer_detail', id=id))


def _service_message_html(body, phone=None):
    """הודעת שירות לקוחות (שיחה עם לקוח / תיבת פניות) - באותו עיצוב של כל המיילים.
    body נכנס כמו שהוא (כמו קודם), כולל ירידות שורה."""
    from services import email_layout as L
    card = L.title('הודעה מתמלול פון') + L.text_block(body, escape_text=False) + L.signature(can_reply=True)
    return L.wrap(card, phone=phone)


def _send_customer_conversation_email(to_email, phone, body, message_id, in_reply_to=None):
    """שולח מייל שיחה ללקוח (מהמנהל). רץ ברקע (thread נפרד) כדי לא לעכב את
    התגובה למנהל. Reply-To מוגדר לכתובת הקליטה הכללית של המערכת. Message-ID
    עצמאי + In-Reply-To/References - כדי שתוכנות מייל ישרשרו נכון בתוך אותה
    שיחה, במקום להציג כל הודעה כמייל נפרד."""
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL, CONVERSATION_MARKER
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, ReplyTo, Header

        html = _service_message_html(body, phone=phone)

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
            to_emails=to_email,
            subject=f'{CONVERSATION_MARKER} {phone}',
            html_content=html,
        )
        message.reply_to = ReplyTo(TRANSCRIBE_INBOUND_EMAIL, 'תמלול פון')
        message.header = Header('Message-ID', message_id)
        if in_reply_to:
            message.header = Header('In-Reply-To', in_reply_to)
            message.header = Header('References', in_reply_to)
        sg.send(message)
    except Exception as e:
        log.error(f"send_customer_conversation_email failed (to={to_email}): {e}")


@admin_bp.route('/customers/<int:id>/send-instructions', methods=['POST'])
@login_required
def admin_send_instructions(id):
    """כפתור ידני בממשק הניהול - שולח ללקוח את אותו מייל הוראות ששלוחה 5-1
    בטלפון שולחת (הוראות לשליחת קבצים לתמלול במייל)."""
    from routes.email_inbound import _send_instructions_email
    customer = Customer.query.get_or_404(id)
    if not (customer.email or '').strip():
        flash('ללקוח אין כתובת מייל רשומה')
        return redirect(url_for('admin.customer_detail', id=id))
    _send_instructions_email(customer.email, customer.phone, customer.name)
    flash('נשלחו הוראות לשליחת קבצים במייל')
    return redirect(url_for('admin.customer_detail', id=id))


@admin_bp.route('/customers/<int:id>/send-handwriting-instructions', methods=['POST'])
@login_required
def admin_send_handwriting_instructions(id):
    """כפתור ידני בממשק הניהול - שולח ללקוח את אותו מייל הוראות ששלוחה 6-1
    בטלפון שולחת (הוראות לשליחת כתב יד לזיהוי OCR)."""
    from routes.email_inbound import _send_handwriting_instructions_email
    customer = Customer.query.get_or_404(id)
    if not (customer.email or '').strip():
        flash('ללקוח אין כתובת מייל רשומה')
        return redirect(url_for('admin.customer_detail', id=id))
    _send_handwriting_instructions_email(customer.email, customer.phone, customer.name)
    flash('נשלחו הוראות לשליחת כתב יד במייל')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/recordings')
@login_required
def recordings():
    page = request.args.get('page', 1, type=int)
    from sqlalchemy.orm import joinedload
    recordings = Recording.query.options(*_light(Recording), joinedload(Recording.customer)) \
        .order_by(Recording.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/recordings.html', recordings=recordings, timedelta=timedelta)

@admin_bp.route('/recordings/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_recordings():
    recording_ids = request.form.getlist('recording_ids')
    if not recording_ids:
        flash('לא נבחרו הקלטות')
    else:
        count = Recording.query.filter(Recording.id.in_(recording_ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'{count} הקלטות נמחקו בהצלחה')
    next_url = request.form.get('next') or url_for('admin.recordings')
    return redirect(next_url)

def _recording_prior_net_charge(rec):
    """כמה כסף נגבה בפועל מהלקוח על ההקלטה הזו (חיובים פחות זיכויים שקשורים
    אליה). 0 אם לא נגבה כלום או שכבר זוכה."""
    total = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.recording_id == rec.id).scalar() or 0
    return round(-float(total), 2) if total < 0 else 0.0


@admin_bp.route('/recordings/<int:id>')
@login_required
def recording_detail(id):
    import math
    recording = Recording.query.get_or_404(id)
    customer = recording.customer
    units = math.ceil((recording.duration_seconds or 0) / 1200) or 1
    price_basic = float(get_setting('price_per_20min_basic', '0.90') or 0.90)
    price_premium = float(get_setting('price_per_20min_premium', '1.90') or 1.90)
    return render_template(
        'admin/recording_detail.html', recording=recording,
        rt_units=units, rt_cost_basic=round(units * price_basic, 2),
        rt_cost_premium=round(units * price_premium, 2),
        rt_balance=float(customer.balance or 0) if customer else 0.0,
        rt_prior_charge=_recording_prior_net_charge(recording),
        rt_in_progress=recording.status in ('recording', 'processing', 'transcribing', 'alefbot_pending', 'queued'),
        excel_template_name=_excel_template_name(recording),
    )


def _excel_template_name(recording):
    if recording.transcription_tier != 'excel' or not recording.excel_template_id:
        return None
    from models import ExcelTemplate
    t = ExcelTemplate.query.get(recording.excel_template_id)
    return (t.name or 'ללא שם') if t else '(התבנית נמחקה)'


@admin_bp.route('/recordings/<int:id>/excel')
@login_required
def recording_excel_download(id):
    from services.excel_tables import safe_filename
    rec = Recording.query.get_or_404(id)
    if not rec.excel_file:
        return 'אין טבלה להקלטה הזו', 404
    return send_file(io.BytesIO(rec.excel_file), as_attachment=True,
                     download_name=f'{safe_filename(_excel_template_name(rec) or "טבלה")}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _recording_source_available(rec):
    """בודק שקובץ האודיו של ההקלטה עדיין זמין לפני שליחה חוזרת לתמלול.
    מחזיר (True, '') או (False, הסבר)."""
    import requests as _rq
    url = (rec.rec_url or '').strip()
    if not url:
        return False, 'אין כתובת קובץ שמורה להקלטה הזו - אי אפשר לשלוח אותה שוב לתמלול'
    if '/api/recordings-email/' in url:
        # הקלטה שהגיעה במייל - נשמרת בדיסק המקומי של השרת (נמחק בדפלוי)
        from routes.email_inbound import RECORDINGS_EMAIL_DIR
        filename = url.rsplit('/', 1)[-1]
        path = os.path.join(RECORDINGS_EMAIL_DIR, filename)
        if os.path.exists(path):
            return True, ''
        if rec.file_data:
            try:
                os.makedirs(RECORDINGS_EMAIL_DIR, exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(rec.file_data)
                return True, ''
            except OSError as e:
                return False, f'שחזור הקובץ נכשל: {e}'
        return False, 'קובץ האודיו של ההקלטה הזו (שהגיעה במייל) כבר לא נמצא בשרת - כנראה נמחק בעדכון מערכת. יש לבקש מהלקוח לשלוח אותו שוב'
    try:
        r = _rq.get(url, timeout=20, stream=True, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')})
        ok = r.status_code < 400
        r.close()
        if not ok:
            return False, f'קובץ האודיו לא זמין כרגע (שגיאה {r.status_code})'
        return True, ''
    except Exception as e:
        return False, f'לא ניתן להגיע לקובץ האודיו: {e}'


@admin_bp.route('/recordings/<int:id>/retranscribe', methods=['POST'])
@login_required
def retranscribe_recording(id):
    """שולח הקלטה שוב לתמלול (רגיל=ג'מיני / מקצועי=אלף-בוט) - חיוב ושליחה ללקוח
    בדיוק כמו תמלול רגיל. אם כבר נגבה כסף על ההקלטה, אפשר לסמן זיכוי של החיוב
    הקודם כדי שהלקוח לא ישלם פעמיים."""
    from services.transcribe import transcribe_async
    rec = Recording.query.get_or_404(id)
    customer = rec.customer
    back = redirect(url_for('admin.recording_detail', id=id))
    choice = request.form.get('tier', '')
    tier = {'regular': 'gemini', 'professional': 'premium'}.get(choice)
    if not tier:
        flash('יש לבחור סוג תמלול: רגיל או מקצועי')
        return back
    if not customer:
        flash('לא נמצא לקוח משויך להקלטה')
        return back

    method = rec.delivery_method
    target = (rec.delivered_to or '').strip()
    if not target:
        if (customer.email or '').strip():
            method, target = 'email', customer.email.strip()
        else:
            flash('אין כתובת מייל/פקס למסירה ללקוח הזה - אי אפשר לשלוח את התמלול בסיום')
            return back
    if method not in ('email', 'fax'):
        method = 'email' if '@' in target else 'fax'

    ok, why = _recording_source_available(rec)
    if not ok:
        flash('⚠ ' + why)
        return back

    prior = _recording_prior_net_charge(rec)
    if prior > 0 and request.form.get('refund') == '1':
        customer.balance = (customer.balance or 0) + prior
        db.session.add(Transaction(
            customer_id=customer.id, amount=prior, type='credit', recording_id=rec.id,
            description=f'זיכוי על חיוב קודם - תמלול חוזר (הקלטה #{rec.id})'))

    rec.transcription_tier = tier
    rec.delivery_method = method
    rec.delivered_to = target
    rec.transcript = None
    rec.summary = None
    rec.alefbot_job_id = None
    rec.expires_at = None
    rec.cost = 0.0
    maintenance_on = get_setting('maintenance_mode', '0') == '1'
    rec.status = 'queued_maintenance' if maintenance_on else 'queued'
    db.session.commit()

    if maintenance_on:
        flash('מצב תחזוקה דלוק - ההקלטה תיכנס לתמלול (' + ('מקצועי' if tier == 'premium' else 'רגיל') + ') כשיכבה')
        return back
    transcribe_async(
        call_id=rec.call_id, rec_url=rec.rec_url, customer_id=customer.id,
        delivery_method=method, delivered_to=target,
        duration_seconds=rec.duration_seconds or 0, transcription_tier=tier,
        language=rec.language or 'he', output_language=rec.output_language or 'he',
    )
    flash('✔ ההקלטה נשלחה שוב לתמלול ' + ('מקצועי' if tier == 'premium' else 'רגיל') +
          ' - תחויב ותישלח ללקוח בסיום (' + target + ')')
    return back

_YEMOT_PATH_RE = __import__('re').compile(r'^[\w\-/.]+$')


def _normalize_yemot_path(raw):
    """'1507' / '1507.wav' / 'recordings/1507' / '/recordings/1507.wav' / 'ivr2:/recordings/1507.wav'
    -> '/recordings/1507.wav'. מספר בלבד = תיקיית ההקלטות הרגילה. None אם לא תקין."""
    p = (raw or '').strip().replace('\\', '/')
    if p.lower().startswith('ivr2:'):
        p = p[5:]
    if not p or '..' in p or not _YEMOT_PATH_RE.match(p):
        return None
    if '/' not in p.strip('/'):
        p = 'recordings/' + p.strip('/')
    p = '/' + p.strip('/')
    if not p.lower().endswith('.wav'):
        p += '.wav'
    return p


def _yemot_probe(url):
    """בודק שהקובץ קיים בימות ומחזיר (True, שניות_משוערות) או (False, הסבר)."""
    import requests as _rq
    import struct
    try:
        with _rq.get(url, timeout=25, stream=True, headers={
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')}) as r:
            if r.status_code >= 400:
                return False, f'ימות החזירו שגיאה {r.status_code}'
            head = next(r.iter_content(chunk_size=4096), b'')
            size = int(r.headers.get('Content-Length') or 0)
    except Exception as e:
        return False, f'לא ניתן להתחבר לימות: {e}'
    if not head.startswith(b'RIFF'):
        return False, 'הקובץ לא נמצא בימות (בדוק את מספר ההקלטה)'
    secs = 0
    try:
        byte_rate = struct.unpack('<I', head[28:32])[0]
        if byte_rate and size > 44:
            secs = int((size - 44) / byte_rate)
    except Exception:
        pass
    return True, secs


@admin_bp.route('/customers/<int:id>/manual-transcribe', methods=['POST'])
@login_required
def customer_manual_transcribe(id):
    """תמלול חוזר / הוספת הקלטה ידנית ללקוח: קובץ שמע שמעלים, או הקלטה מימות לפי
    מספר (למשל 1507 = /recordings/1507.wav). נוצרת הקלטה חדשה בכרטיס הלקוח והיא
    עוברת את המסלול הרגיל בדיוק: תמלול, חיוב מהארנק ושליחה במייל/פקס."""
    import uuid
    from services.transcribe import transcribe_async
    customer = Customer.query.get_or_404(id)
    back = redirect(url_for('admin.customer_detail', id=id) + '#manual-transcribe')
    tier = {'regular': 'gemini', 'professional': 'premium'}.get(request.form.get('tier', ''))
    if not tier:
        flash('יש לבחור סוג תמלול: רגיל או מקצועי')
        return back
    method = 'fax' if request.form.get('send_method') == 'fax' else 'email'
    target = (request.form.get('send_to') or '').strip()
    if not target:
        target = ((customer.fax if method == 'fax' else customer.email) or '').strip()
    if not target:
        flash('אין כתובת מייל/מספר פקס לשליחת התמלול')
        return back
    if method == 'email' and '@' not in target:
        flash('כתובת המייל לא תקינה')
        return back

    source = request.form.get('source')
    file_backup, source_filename = None, None
    if source == 'yemot':
        path = _normalize_yemot_path(request.form.get('yemot_path'))
        if not path:
            flash('מספר/נתיב ההקלטה בימות לא תקין. דוגמה: 1507')
            return back
        token = os.environ.get('YEMOT_TOKEN', '') or get_setting('yemot_token', '')
        if not token:
            flash('YEMOT_TOKEN לא מוגדר בשרת - אי אפשר למשוך מימות')
            return back
        rec_url = f'https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path=ivr2:{path}'
        dup = (Recording.query.filter(Recording.rec_url.like(f'%path=ivr2:{path}'))
               .order_by(Recording.id.desc()).first())
        if dup and request.form.get('confirm_dup') != '1':
            who = dup.customer.name if dup.customer else 'לקוח לא ידוע'
            flash(f'⚠ ההקלטה {path} כבר קיימת במערכת (הקלטה #{dup.id}, {who}, סטטוס {dup.status}). '
                  f'אם בכל זאת לתמלל שוב - סמן "לתמלל למרות שכבר קיימת" ושלח שוב')
            return back
        ok, info = _yemot_probe(rec_url)
        if not ok:
            flash('⚠ ' + info + f' ({path})')
            return back
        duration = info
        source_filename = f'ימות {path}'
    elif source == 'file':
        from routes.email_inbound import RECORDINGS_EMAIL_DIR, AUDIO_VIDEO_EXTENSIONS, _estimate_duration_seconds
        f = request.files.get('audio_file')
        if not f or not f.filename:
            flash('לא נבחר קובץ')
            return back
        ext = os.path.splitext(f.filename)[1].lstrip('.').lower()
        if ext not in AUDIO_VIDEO_EXTENSIONS:
            flash(f'סוג קובץ לא נתמך ({ext or "ללא סיומת"}) - יש להעלות קובץ שמע/וידאו')
            return back
        os.makedirs(RECORDINGS_EMAIL_DIR, exist_ok=True)
        filename = f'{uuid.uuid4().hex}.{ext}'
        filepath = os.path.join(RECORDINGS_EMAIL_DIR, filename)
        f.save(filepath)
        if os.path.getsize(filepath) <= 150 * 1024 * 1024:   # גיבוי ב-DB - הדיסק נמחק בכל דיפלוי
            with open(filepath, 'rb') as fh:
                file_backup = fh.read()
        rec_url = f"{os.environ.get('APP_BASE_URL', '').rstrip('/')}/api/recordings-email/{filename}"
        duration = _estimate_duration_seconds(filepath) or 0
        source_filename = f.filename
    else:
        flash('יש לבחור מקור: העלאת קובץ או משיכה מימות')
        return back

    maintenance_on = get_setting('maintenance_mode', '0') == '1'
    call_id = f'manual-{uuid.uuid4().hex[:12]}'
    rec = Recording(
        customer_id=customer.id, call_id=call_id, duration_seconds=duration,
        status='queued_maintenance' if maintenance_on else 'processing',
        delivery_method=method, delivered_to=target, rec_url=rec_url,
        source_filename=source_filename, transcription_tier=tier,
        language='he', output_language='he', file_data=file_backup,
    )
    db.session.add(rec)
    db.session.commit()
    logging.getLogger(__name__).info(
        f'manual-transcribe: customer={customer.id} rec={rec.id} source={source} tier={tier} ~{duration}s')
    label = 'מקצועי' if tier == 'premium' else 'רגיל'
    if maintenance_on:
        flash(f'מצב תחזוקה דלוק - ההקלטה #{rec.id} תיכנס לתמלול {label} כשיכבה')
        return back
    transcribe_async(call_id=call_id, rec_url=rec_url, customer_id=customer.id,
                     delivery_method=method, delivered_to=target, duration_seconds=duration,
                     transcription_tier=tier, language='he', output_language='he')
    mins = f' (~{duration // 60} דק\')' if duration else ''
    flash(f'✔ הקלטה #{rec.id}{mins} נשלחה לתמלול {label} - תחויב ותישלח ל-{target} בסיום')
    return back


@admin_bp.route('/recordings/<int:id>/download-audio')
@login_required
def download_audio(id):
    import requests as req
    recording = Recording.query.get_or_404(id)

    yemot_username = __import__('os').environ.get('YEMOT_USERNAME', '')
    yemot_password = __import__('os').environ.get('YEMOT_PASSWORD', '')

    call_id = recording.call_id or ''
    rec_url = f'https://www.call2all.co.il/ym/api/DownloadFile?username={yemot_username}&password={yemot_password}&path=ivr2:/recordings/{call_id}.wav'

    try:
        # User-Agent "רגיל" - ראו הערה מפורטת ב-routes/recording_proxy.py:
        # בלי זה ימות המשיח (או הגנה שיושבת לפניהם) מגיבים לפעמים עם שגיאת
        # 418 לבקשות שרת-לשרת "חשודות". אם השגיאה נמשכת גם עם ה-header הזה,
        # כדאי לבדוק שמשתני הסביבה YEMOT_USERNAME/YEMOT_PASSWORD אכן מוגדרים
        # נכון בריילוואי - אם הם ריקים, הבקשה נשלחת עם username/password ריקים.
        r = req.get(rec_url, timeout=60, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
        })
        r.raise_for_status()
        return send_file(
            io.BytesIO(r.content),
            mimetype='audio/wav',
            as_attachment=True,
            download_name=f'recording_{id}.wav'
        )
    except Exception as e:
        flash(f'שגיאה בהורדת ההקלטה: {e}')
        return redirect(url_for('admin.recording_detail', id=id))

@admin_bp.route('/recordings/<int:id>/play-audio')
@login_required
def play_audio(id):
    import requests as req
    import os
    recording = Recording.query.get_or_404(id)

    yemot_username = os.environ.get('YEMOT_USERNAME', '')
    yemot_password = os.environ.get('YEMOT_PASSWORD', '')

    call_id = recording.call_id or ''
    rec_url = f'https://www.call2all.co.il/ym/api/DownloadFile?username={yemot_username}&password={yemot_password}&path=ivr2:/recordings/{call_id}.wav'

    try:
        r = req.get(rec_url, timeout=60, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
        })
        r.raise_for_status()
        response = make_response(r.content)
        response.headers['Content-Type'] = 'audio/wav'
        response.headers['Content-Disposition'] = 'inline'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@admin_bp.route('/recordings/<int:id>/download-word')
@login_required
def download_word(id):
    """אותו קובץ Word בדיוק שהלקוח מקבל במייל (services/transcribe._build_word_doc)."""
    from services.transcribe import _build_word_doc
    recording = Recording.query.get_or_404(id)
    customer = Customer.query.get(recording.customer_id)
    if not recording.transcript:
        return 'אין עדיין תמלול להקלטה הזו', 404
    name = (customer.name or customer.phone) if customer else ''
    secs = recording.duration_seconds or 0
    call_time = None
    if recording.created_at:                       # שעת ההקלטה (שמורה ב-UTC) בשעון ישראל
        from datetime import timezone
        from zoneinfo import ZoneInfo
        call_time = recording.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Jerusalem'))
    output = io.BytesIO(_build_word_doc(name, f'{secs // 60}:{secs % 60:02d}', recording.transcript,
                                        title=recording.source_filename or 'תמלול שיחה', call_time=call_time))

    # נטפרי חוסמת קובץ Word שיורד ישירות מהאתר שלנו; כמו בפורטל המוסדות - עמוד HTML
    # שהקובץ מוטמע בתוכו, וההורדה קורית בדפדפן עצמו (routes/download_utils)
    from routes.download_utils import render_data_uri_download_page
    who = (customer.name or customer.phone) if customer else ''
    date = recording.created_at.strftime('%d-%m-%Y') if recording.created_at else str(id)
    return render_data_uri_download_page(
        output.getvalue(), f'תמלול - {who} - {date}.docx'.replace('/', '-'),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


@admin_bp.route('/recordings/<int:id>/listen')
@login_required
def recording_listen(id):
    """השמעה בנגן / הורדה של הקלטה. נטפרי חוסמת שמע שמגיע מהאתר שלנו, אז הקלטה
    מהטלפון מופנית ישר לימות (המפתח המוגבל להורדה בלבד - routes/recording_proxy).
    הקלטה שהגיעה במייל נמצאת אצלנו - מוגשת כמו שהיא (משוחזרת מה-DB אם נמחקה מהדיסק)."""
    from routes.recording_proxy import _best_yemot_url
    rec = Recording.query.get_or_404(id)
    url = (rec.rec_url or '').strip()
    if '/api/recordings-email/' in url:
        ok, why = _recording_source_available(rec)       # משחזר לדיסק מה-DB במידת הצורך
        if not ok:
            return why, 404
        from routes.email_inbound import RECORDINGS_EMAIL_DIR
        from flask import send_from_directory
        return send_from_directory(os.path.abspath(RECORDINGS_EMAIL_DIR), url.rsplit('/', 1)[-1],
                                   as_attachment=bool(request.args.get('download')),
                                   download_name=rec.source_filename or None)
    target = _best_yemot_url(rec)
    if not target:
        return 'אין קובץ שמע להקלטה הזו', 404
    return redirect(target, code=302)


@admin_bp.route('/recordings/live')
@login_required
def recordings_live():
    """הלוח החי (templates/admin/_rec_live.html): מצב עדכני של ההקלטות שבטיפול,
    והאם נכנסו הקלטות חדשות מאז שהדף נטען."""
    ids = [int(x) for x in (request.args.get('ids') or '').split(',') if x.strip().isdigit()][:200]
    rows = {}
    if ids:
        tpl = "{% include 'admin/_rec_status.html' %}"
        from flask import render_template_string
        for rec in Recording.query.options(*_light(Recording)).filter(Recording.id.in_(ids)).all():
            rows[rec.id] = {
                'st': render_template_string(tpl, rec=rec),
                'cost': '₪%.2f' % (rec.cost or 0),
                'dur': '%d:%02d' % ((rec.duration_seconds or 0) // 60, (rec.duration_seconds or 0) % 60),
            }
    newer = 0
    after = request.args.get('after', '')
    scope = request.args.get('scope', '')
    if after.isdigit():
        q = Recording.query.filter(Recording.id > int(after))
        if scope.isdigit():
            q = q.filter(Recording.customer_id == int(scope))
        elif scope != 'all':
            q = None
        newer = q.count() if q is not None else 0
    resp = jsonify({'rows': rows, 'newer': newer})
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@admin_bp.route('/recordings/cleanup', methods=['POST'])
@login_required
def cleanup_old_recordings():
    cutoff = datetime.utcnow() - timedelta(days=30)
    old = Recording.query.filter(Recording.created_at < cutoff).all()
    count = 0
    for rec in old:
        rec.transcript = None
        rec.summary = None
        count += 1
    db.session.commit()
    flash(f'נמחקו תמלולים של {count} הקלטות ישנות')
    return redirect(url_for('admin.recordings'))

@admin_bp.route('/reports')
@login_required
def reports():
    monthly = db.session.query(
        func.to_char(Transaction.created_at, 'YYYY-MM').label('month'),
        func.sum(Transaction.amount).label('revenue')
    ).filter(Transaction.type == 'charge').group_by('month').order_by('month').all()
    top_customers = db.session.query(
        Customer,
        func.sum(Transaction.amount).label('total_spend')
    ).join(Transaction).filter(
        Transaction.type == 'debit'
    ).group_by(Customer.id).order_by(func.sum(Transaction.amount).desc()).limit(10).all()

    return render_template('admin/reports.html',
        monthly=monthly, top_customers=top_customers)

@admin_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    # הסיסמה מוגדרת רק במשתנה הסביבה ADMIN_PASSWORD (Railway) - לא משתנה מכאן
    flash('סיסמת המנהל מוגדרת במשתני הסביבה בשרת (ADMIN_PASSWORD) ואי אפשר לשנות אותה מכאן')
    return redirect(url_for('admin.settings'))


@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        set_setting('price_per_20min_basic', request.form.get('price_per_20min_basic', '0.90'))
        set_setting('price_per_20min_premium', request.form.get('price_per_20min_premium', '1.90'))
        set_setting('price_per_20min_video', request.form.get('price_per_20min_video', '1.50'))
        set_setting('price_per_20min_excel', request.form.get('price_per_20min_excel', '1.50'))
        set_setting('price_per_1000_chars_ocr', request.form.get('price_per_1000_chars_ocr', '0.10'))
        set_setting('ocr_engine', request.form.get('ocr_engine', 'gemini'))
        set_setting('manuscript_char_unit_size', request.form.get('manuscript_char_unit_size', '1000'))
        set_setting('price_per_manuscript_char_unit', request.form.get('price_per_manuscript_char_unit', '0.10'))
        set_setting('price_manuscript_proofing', request.form.get('price_manuscript_proofing', '5.00'))
        set_setting('fax_inbound_number', request.form.get('fax_inbound_number', ''))
        set_setting('yemot_token', request.form.get('yemot_token', ''))
        set_setting('yemot_log_path', request.form.get('yemot_log_path', 'ivr2:/199/LogCreditCardOK.ymgr'))
        set_setting('payment_callback_secret', request.form.get('payment_callback_secret', ''))
        set_setting('nedarim_mosad_number', request.form.get('nedarim_mosad_number', ''))
        set_setting('nedarim_api_password', request.form.get('nedarim_api_password', ''))
        set_setting('nedarim_tamal_type', request.form.get('nedarim_tamal_type', '400'))
        # הגדרות בונוס - עד 3 רמות
        for i in range(1, 4):
            set_setting(f'bonus_threshold_{i}', request.form.get(f'bonus_threshold_{i}', ''))
            set_setting(f'bonus_amount_{i}', request.form.get(f'bonus_amount_{i}', ''))
        set_setting('min_balance', request.form.get('min_balance', '5'))
        set_setting('max_recording_seconds', request.form.get('max_recording_seconds', '1800'))
        if request.form.get('_topup_field') == '1':
            set_setting('allow_student_self_topup', '1' if request.form.get('allow_student_self_topup') == '1' else '0')
        flash('הגדרות נשמרו בהצלחה')
        return redirect(url_for('admin.settings'))

    current_settings = {
        'price_per_20min_basic': get_setting('price_per_20min_basic', '0.90'),
        'price_per_20min_premium': get_setting('price_per_20min_premium', '1.90'),
        'price_per_20min_video': get_setting('price_per_20min_video', '1.50'),
        'price_per_20min_excel': get_setting('price_per_20min_excel', '1.50'),
        'price_per_1000_chars_ocr': get_setting('price_per_1000_chars_ocr', '0.10'),
        'ocr_engine': get_setting('ocr_engine', 'gemini'),
        'manuscript_char_unit_size': get_setting('manuscript_char_unit_size', '1000'),
        'price_per_manuscript_char_unit': get_setting('price_per_manuscript_char_unit', '0.10'),
        'price_manuscript_proofing': get_setting('price_manuscript_proofing', '5.00'),
        'fax_inbound_number': get_setting('fax_inbound_number', ''),
        'yemot_token': get_setting('yemot_token', ''),
        'yemot_log_path': get_setting('yemot_log_path', 'ivr2:/199/LogCreditCardOK.ymgr'),
        'payment_callback_secret': get_setting('payment_callback_secret', ''),
        'nedarim_mosad_number': get_setting('nedarim_mosad_number', ''),
        'nedarim_api_password': get_setting('nedarim_api_password', ''),
        'nedarim_tamal_type': get_setting('nedarim_tamal_type', '400'),
        'bonus_thresholds': [
            {'threshold': get_setting(f'bonus_threshold_{i}', ''), 'amount': get_setting(f'bonus_amount_{i}', '')}
            for i in range(1, 4)
        ],
        'allow_student_self_topup': get_setting('allow_student_self_topup', '0') == '1',
        'min_balance': get_setting('min_balance', '5'),
        'max_recording_seconds': get_setting('max_recording_seconds', '1800'),
        'welcome_new': get_setting('welcome_new', 'שלום וברוכים הבאים למערכת התמלול.'),
        'welcome_returning': get_setting('welcome_returning', 'שלום וברוכים השבים.'),
        'system_explanation': get_setting('system_explanation', 'מערכת התמלול מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס.'),
    }
    return render_template('admin/settings.html', settings=current_settings)


@admin_bp.route('/maintenance')
@login_required
def maintenance():
    return render_template('admin/maintenance.html',
                            maintenance_mode=(get_setting('maintenance_mode', '0') == '1'))


@admin_bp.route('/maintenance/status')
@login_required
def maintenance_status():
    from services.job_tracker import snapshot
    from models import Recording, OcrResult
    data = snapshot()
    data['maintenance_mode'] = (get_setting('maintenance_mode', '0') == '1')
    data['queued_calls'] = Recording.query.filter_by(status='queued_maintenance').count()
    data['queued_ocr'] = OcrResult.query.filter_by(status='queued_maintenance').count()
    return jsonify(data)


@admin_bp.route('/maintenance/toggle', methods=['POST'])
@login_required
def maintenance_toggle():
    turn_on = request.form.get('enable') == '1'
    set_setting('maintenance_mode', '1' if turn_on else '0')
    released_calls = 0
    released_ocr = 0
    if not turn_on:
        # כבים מצב תחזוקה - משחררים שיחות ותמונות OCR שהצטברו בזמן שהמצב היה דלוק
        from services.transcribe import resume_queued_recordings
        from routes.email_inbound import resume_queued_ocr
        released_calls = resume_queued_recordings()
        released_ocr = resume_queued_ocr()
    return jsonify({
        'ok': True, 'maintenance_mode': turn_on,
        'released': released_calls,  # תאימות לאחור לשם השדה הקיים
        'released_calls': released_calls,
        'released_ocr': released_ocr,
    })


@admin_bp.route('/maintenance/clear-stale', methods=['POST'])
@login_required
def maintenance_clear_stale():
    from services.job_tracker import clear_stale
    n = clear_stale()
    return jsonify({'ok': True, 'cleared': n})


@admin_bp.route('/api/stats')
@login_required
def api_stats():
    last_30 = []
    for i in range(29, -1, -1):
        day = datetime.utcnow().date() - timedelta(days=i)
        revenue = db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge',
            func.date(Transaction.created_at) == day
        ).scalar() or 0
        recordings = Recording.query.filter(
            func.date(Recording.created_at) == day
        ).count()
        last_30.append({'date': str(day), 'revenue': float(revenue), 'recordings': recordings})
    return jsonify(last_30)
@admin_bp.route('/messages')
@login_required
def manager_messages():
    status_filter = request.args.get('status', '')
    # מסננים החוצה הודעות "משוריינות" שמעולם לא הוקלטו בפועל (הלקוח ניתק לפני
    # שהקליט, או הקיש סולמית בלי להשאיר תוכן) - ל-Yemot עדיין דרוש שריון ID
    # מראש כדי שמספר ההודעה בשלוחה יתאים למספר בממשק, אבל אין סיבה להציג
    # רשומות בלי rec_url בפועל כאילו הן הודעות אמיתיות.
    from sqlalchemy import or_, and_
    query = ManagerMessage.query.filter(or_(
        and_(ManagerMessage.rec_url.isnot(None), ManagerMessage.rec_url != ''),
        ManagerMessage.source != 'ivr',
    ))
    if status_filter:
        query = query.filter_by(status=status_filter)
    messages = query.order_by(ManagerMessage.created_at.desc()).all()
    # טלפון -> מספר לקוח (גם טלפון נוסף של לקוח), כדי שהטלפון יהיה קישור לכרטיס הלקוח
    from models import CustomerPhone
    phones = {m.phone for m in messages if m.phone}
    customer_ids = {}
    if phones:
        for row in CustomerPhone.query.filter(CustomerPhone.phone.in_(phones)).with_entities(
                CustomerPhone.phone, CustomerPhone.customer_id):
            customer_ids[row[0]] = row[1]
        for row in Customer.query.filter(Customer.phone.in_(phones)).with_entities(Customer.phone, Customer.id):
            customer_ids[row[0]] = row[1]
    unread_replies = CustomerMessage.query.filter_by(direction='in', is_read=False) \
        .order_by(CustomerMessage.created_at.desc()).all()
    from models import GeneralInboxMessage
    unread_inbox_count = GeneralInboxMessage.query.filter_by(is_read=False).count()
    return render_template('admin/manager_messages.html', messages=messages,
        status_filter=status_filter, timedelta=timedelta, unread_replies=unread_replies,
        unread_inbox_count=unread_inbox_count, customer_ids=customer_ids)


@admin_bp.route('/messages/<int:id>/play')
@login_required
def play_manager_message(id):
    import requests as req, io
    msg = ManagerMessage.query.get_or_404(id)
    if not msg.rec_url:
        return jsonify({'error': 'אין הקלטה'}), 404
    # נטפרי חוסמת קובצי שמע שמוגשים מהדומיין שלנו, אבל דומיין ימות פתוח - לכן מפנים
    # את הנגן ישר לימות, עם המפתח המוגבל להורדה בלבד (כמו בהקלטות, routes/recording_proxy.py)
    from routes.recording_proxy import _YEMOT_DOWNLOAD_API_KEY, _with_replaced_token
    if _YEMOT_DOWNLOAD_API_KEY and 'call2all.co.il' in msg.rec_url:
        target = _with_replaced_token(msg.rec_url, _YEMOT_DOWNLOAD_API_KEY)
        if target:
            return redirect(target, code=302)
    try:
        r = req.get(msg.rec_url, timeout=120, stream=False)
        r.raise_for_status()
        return send_file(
            io.BytesIO(r.content),
            mimetype='audio/wav',
            as_attachment=False,
            download_name='message.wav'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
 
@admin_bp.route('/messages/<int:id>/status', methods=['POST'])
@login_required
def update_message_status(id):
    msg = ManagerMessage.query.get_or_404(id)
    msg.status     = request.form.get('status', msg.status)
    msg.admin_note = request.form.get('admin_note', msg.admin_note)
    db.session.commit()
    flash('סטטוס עודכן בהצלחה')
    return_filter = request.form.get('return_filter', '')
    return redirect(url_for('admin.manager_messages', status=return_filter) if return_filter
                     else url_for('admin.manager_messages'))


@admin_bp.route('/messages/<int:id>/set-email', methods=['POST'])
@login_required
def message_set_email(id):
    """בקשה לעדכון מייל (הודעה שהלקוח השאיר בטלפון): הנציג מקליד את המייל ששמע,
    והוא נשמר ללקוח של אותו טלפון (ושיטת השליחה עוברת למייל), וההודעה מסומנת "טופל"."""
    import re
    from services.customer_phones import find_customer_by_phone
    msg = ManagerMessage.query.get_or_404(id)
    back = redirect(url_for('admin.manager_messages', status=request.form.get('return_filter') or None))
    email = (request.form.get('email') or '').strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        flash('כתובת המייל לא תקינה')
        return back
    customer = find_customer_by_phone(msg.phone) if msg.phone else None
    if not customer:
        flash(f'לא נמצא לקוח עם הטלפון {msg.phone}')
        return back
    old = customer.email or ''
    customer.email = email
    customer.delivery_method = 'email'
    msg.status = 'done'
    msg.admin_note = ((msg.admin_note or '') + f'\nהמייל עודכן ל-{email}' + (f' (היה {old})' if old else '')).strip()
    db.session.commit()
    flash(f'✔ המייל של {customer.name or msg.phone} עודכן ל-{email}')
    return back


@admin_bp.route('/messages/<int:id>/delete', methods=['POST'])
@login_required
def delete_manager_message(id):
    msg = ManagerMessage.query.get_or_404(id)
    db.session.delete(msg)
    db.session.commit()
    flash('הפניה נמחקה')
    return_filter = request.form.get('return_filter', '')
    return redirect(url_for('admin.manager_messages', status=return_filter) if return_filter
                     else url_for('admin.manager_messages'))
@admin_bp.route('/messages/debug')
@login_required
def debug_messages():
    from models import ManagerMessage
    msgs = ManagerMessage.query.all()
    return jsonify([{
        'id': m.id,
        'call_id': m.call_id,
        'rec_url': m.rec_url
    } for m in msgs])

@admin_bp.route('/test-transcribe', methods=['GET', 'POST'])
@login_required
def test_transcribe():
    if request.method == 'POST':
        import os, uuid, tempfile
        from services.transcribe import _gemini_from_url, finalize_alefbot_recording, _send_email, _send_fax

        file = request.files.get('audio_file')
        tier = request.form.get('tier', 'gemini')
        language = request.form.get('language', 'he')
        output_language = request.form.get('output_language', 'he')
        send_method = request.form.get('send_method', 'email')
        send_to = request.form.get('send_to', '')
        customer_id = request.form.get('customer_id', '')

        if not file:
            flash('יש להעלות קובץ')
            return redirect(url_for('admin.test_transcribe'))

        # שמור קובץ זמנית
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        file.save(tmp.name)
        tmp.close()

        # בנה URL זמני מה-static
        import shutil
        static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
        os.makedirs(static_dir, exist_ok=True)
        filename = f"test_{uuid.uuid4().hex}.wav"
        dest_path = os.path.join(static_dir, filename)
        shutil.copy(tmp.name, dest_path)
        os.remove(tmp.name)

        base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
        rec_url = f"{base_url}/static/fax_tmp/{filename}"

        customer = Customer.query.get(int(customer_id)) if customer_id and customer_id.isdigit() else None
        if customer and not send_to:
            send_to = customer.email if send_method == 'email' else customer.fax

        try:
            transcript_variants = None  # רשימת (כותרת, טקסט) להשוואה בתבנית, לטיירים נסיוניים
            if tier == 'gemini':
                transcript, duration, _ = _gemini_from_url(rec_url, language, output_language)
            elif tier == 'gemini_pro_solo':
                # נסיוני - פעימה אחת בלבד עם Gemini 3.1 Pro + פרומפט הגייה אשכנזית-חסידית
                from services.transcribe import _gemini_pro_solo
                transcript, duration = _gemini_pro_solo(rec_url, language, output_language)
            elif tier == 'gemini_review':
                # נסיוני - תמלול מקצועי מבוסס Gemini עם מעבר תיקון שני (במקום אלף בוט)
                from services.transcribe import _gemini_review_pass
                transcript, duration, transcript_raw_first_pass = _gemini_review_pass(rec_url, language, output_language)
                if transcript_raw_first_pass:
                    transcript_variants = [
                        ('לפני תיקון (תמלול ראשוני)', transcript_raw_first_pass),
                        ('אחרי תיקון (גרסה סופית)', transcript),
                    ]
            elif tier == 'gemini_dual_flash':
                # נסיוני - שני תמלולים עצמאיים + מיזוג ע"י Flash (במקום אלף בוט)
                from services.transcribe import _gemini_dual_transcribe_and_merge
                transcript, duration, transcript_a, transcript_b = _gemini_dual_transcribe_and_merge(
                    rec_url, language, output_language, merge_model='gemini-3.5-flash')
                if transcript_a:
                    variants = [('תמלול א\' (עצמאי)', transcript_a)]
                    if transcript_b:
                        variants.append(('תמלול ב\' (עצמאי)', transcript_b))
                    variants.append(('אחרי מיזוג - Flash (גרסה סופית)', transcript))
                    transcript_variants = variants
            elif tier == 'gemini_dual_pro':
                # נסיוני - שני תמלולים עצמאיים + מיזוג ע"י Pro (במקום אלף בוט)
                from services.transcribe import _gemini_dual_transcribe_and_merge
                transcript, duration, transcript_a, transcript_b = _gemini_dual_transcribe_and_merge(
                    rec_url, language, output_language, merge_model='gemini-3.1-pro-preview')
                if transcript_a:
                    variants = [('תמלול א\' (עצמאי)', transcript_a)]
                    if transcript_b:
                        variants.append(('תמלול ב\' (עצמאי)', transcript_b))
                    variants.append(('אחרי מיזוג - Pro (גרסה סופית)', transcript))
                    transcript_variants = variants
            else:
                # AlefBot — שלח ישירות
                from services.transcribe import _alefbot_submit
                job_id, duration = _alefbot_submit(rec_url, f"test_{uuid.uuid4().hex[:8]}")
                flash(f'אלף בוט קיבל את הקובץ — job_id: {job_id}. התמלול יגיע ב-webhook.')
                return redirect(url_for('admin.test_transcribe'))

            if not transcript:
                flash('התמלול נכשל')
                return redirect(url_for('admin.test_transcribe'))

            if send_to and customer:
                if send_method == 'email':
                    _send_email(send_to, transcript, customer, rec_url, duration)
                    flash(f'תמלול נשלח למייל: {send_to}')
                else:
                    _send_fax(send_to, transcript, customer, duration)
                    flash(f'תמלול נשלח לפקס: {send_to}')
            else:
                flash('התמלול הושלם — לא נשלח (לא הוזן לקוח/כתובת)')

            return render_template('admin/test_transcribe.html',
                transcript=transcript,
                duration=duration,
                transcript_variants=transcript_variants,
                customers=Customer.query.order_by(Customer.name).all()
            )

        except Exception as e:
            flash(f'שגיאה: {e}')
            return redirect(url_for('admin.test_transcribe'))

    customers = Customer.query.order_by(Customer.name).all()
    return render_template('admin/test_transcribe.html', transcript=None, transcript_variants=None, customers=customers)
@admin_bp.route('/messages/bulk-status', methods=['POST'])
@login_required
def bulk_update_status():
    data = request.json
    ids = data.get('ids', [])
    status = data.get('status', '')
    for msg_id in ids:
        msg = ManagerMessage.query.get(int(msg_id))
        if msg:
            msg.status = status
    db.session.commit()
    return jsonify({'ok': True})

@admin_bp.route('/messages/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_messages():
    data = request.json
    ids = data.get('ids', [])
    for msg_id in ids:
        msg = ManagerMessage.query.get(int(msg_id))
        if msg:
            db.session.delete(msg)
    db.session.commit()
    return jsonify({'ok': True})


@admin_bp.route('/customers/bulk-action', methods=['POST'])
@login_required
def bulk_customer_action():
    """ניהול לקוחות מרובים — הוספת כסף, אחוזים, סינון"""
    action = request.form.get('action')
    customer_ids = request.form.getlist('customer_ids')

    if not customer_ids:
        flash('לא נבחרו לקוחות')
        return redirect(url_for('admin.customers'))

    customers = Customer.query.filter(Customer.id.in_(customer_ids)).all()

    if action == 'add_amount':
        amount = float(request.form.get('amount', 0) or 0)
        if amount <= 0:
            flash('סכום לא תקין')
            return redirect(url_for('admin.customers'))
        for c in customers:
            c.balance = round(c.balance + amount, 2)
            txn = Transaction(
                customer_id=c.id,
                amount=amount,
                type='credit',
                description=f'זיכוי ידני מנהל — {amount}₪'
            )
            db.session.add(txn)
        db.session.commit()
        for _c in customers:
            try:
                from routes.dictate import trigger_pending_manuscripts
                trigger_pending_manuscripts(customer_id=_c.id)
            except Exception:
                pass
        flash(f'נוסף ₪{amount} ל-{len(customers)} לקוחות')

    elif action == 'add_percent':
        percent = float(request.form.get('percent', 0) or 0)
        if percent <= 0:
            flash('אחוז לא תקין')
            return redirect(url_for('admin.customers'))
        for c in customers:
            bonus = round(c.balance * percent / 100, 2)
            c.balance = round(c.balance + bonus, 2)
            txn = Transaction(
                customer_id=c.id,
                amount=bonus,
                type='credit',
                description=f'זיכוי {percent}% מנהל — {bonus}₪'
            )
            db.session.add(txn)
        db.session.commit()
        for _c in customers:
            try:
                from routes.dictate import trigger_pending_manuscripts
                trigger_pending_manuscripts(customer_id=_c.id)
            except Exception:
                pass
        flash(f'נוסף {percent}% ל-{len(customers)} לקוחות')

    elif action == 'delete':
        from models import OcrResult
        count = len(customers)
        for c in customers:
            Transaction.query.filter_by(customer_id=c.id).delete()
            Recording.query.filter_by(customer_id=c.id).delete()
            OcrResult.query.filter_by(customer_id=c.id).delete()
            db.session.delete(c)
        db.session.commit()
        flash(f'{count} לקוחות נמחקו בהצלחה')

    return redirect(url_for('admin.customers'))


@admin_bp.route('/customers-filter')
@login_required
def customers_filter():
    """סינון לקוחות לפי פעילות"""
    from datetime import timedelta
    filter_type = request.args.get('filter', '')
    search = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)

    query = Customer.query

    if filter_type == 'inactive_month':
        one_month_ago = datetime.utcnow() - timedelta(days=30)
        active_ids = db.session.query(Recording.customer_id).filter(
            Recording.created_at >= one_month_ago
        ).subquery()
        query = query.filter(~Customer.id.in_(active_ids))
    elif filter_type == 'inactive_year':
        one_year_ago = datetime.utcnow() - timedelta(days=365)
        active_ids = db.session.query(Recording.customer_id).filter(
            Recording.created_at >= one_year_ago
        ).subquery()
        query = query.filter(~Customer.id.in_(active_ids))

    if search:
        from models import CustomerPhone
        _extra_ids = db.session.query(CustomerPhone.customer_id).filter(
            CustomerPhone.phone.contains(search) | CustomerPhone.email.contains(search))
        query = query.filter(
            Customer.phone.contains(search) |
            Customer.name.contains(search) |
            Customer.email.contains(search) |
            Customer.id.in_(_extra_ids)
        )

    customers = query.order_by(Customer.created_at.desc()).paginate(page=page, per_page=50)

    customer_ids = [c.id for c in customers.items]
    counts = dict(
        db.session.query(Recording.customer_id, func.count(Recording.id))
        .filter(Recording.customer_id.in_(customer_ids))
        .group_by(Recording.customer_id)
        .all()
    ) if customer_ids else {}
    recording_counts = {cid: counts.get(cid, 0) for cid in customer_ids}

    return render_template('admin/customers.html',
        customers=customers,
        search=search,
        active_filter=filter_type,
        recording_counts=recording_counts
    )


@admin_bp.route('/ocr')
@login_required
def ocr_list():
    from models import OcrResult
    page = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')

    from sqlalchemy.orm import joinedload
    query = OcrResult.query.options(*_light(OcrResult), joinedload(OcrResult.customer)).order_by(OcrResult.created_at.desc())
    if search:
        query = query.join(Customer).filter(
            Customer.phone.contains(search) | OcrResult.original_filename.contains(search)
        )

    ocr_results = query.paginate(page=page, per_page=20)
    return render_template('admin/ocr_list.html', ocr_results=ocr_results, search=search)


@admin_bp.route('/ocr/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_ocr():
    from models import OcrResult
    ocr_ids = request.form.getlist('ocr_ids')
    if not ocr_ids:
        flash('לא נבחרו תוצאות OCR')
    else:
        items = OcrResult.query.filter(OcrResult.id.in_(ocr_ids)).all()
        count = len(items)
        for item in items:
            try:
                if item.original_file_path and os.path.exists(item.original_file_path):
                    os.remove(item.original_file_path)
            except Exception:
                pass
            db.session.delete(item)
        db.session.commit()
        flash(f'{count} תוצאות OCR נמחקו בהצלחה')
    next_url = request.form.get('next') or url_for('admin.ocr_list')
    return redirect(next_url)


@admin_bp.route('/ocr/<int:ocr_id>')
@login_required
def ocr_detail(ocr_id):
    from models import OcrResult
    ocr = OcrResult.query.get_or_404(ocr_id)
    return render_template('admin/ocr_detail.html', ocr=ocr)


@admin_bp.route('/ocr/<int:ocr_id>/file')
@login_required
def ocr_file(ocr_id):
    from models import OcrResult
    import os
    ocr = OcrResult.query.get_or_404(ocr_id)
    if not ocr.original_file_path or not os.path.exists(ocr.original_file_path):
        return "הקובץ אינו זמין", 404
    return send_file(ocr.original_file_path, as_attachment=False,
                     download_name=ocr.original_filename)


def _il_day_range_to_utc(start_str, end_str):
    """ממיר טווח תאריכים (YYYY-MM-DD, לפי שעון ישראל) לטווח UTC [start, end)
    להשוואה מול started_at שנשמר ב-UTC. מטפל אוטומטית בשעון קיץ/חורף."""
    from zoneinfo import ZoneInfo
    from datetime import timezone as _timezone
    il_tz = ZoneInfo('Asia/Jerusalem')
    start_local = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=il_tz)
    end_local = (datetime.strptime(end_str, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=il_tz)
    start_utc = start_local.astimezone(_timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(_timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


@admin_bp.route('/calls-report')
@login_required
def calls_report():
    from zoneinfo import ZoneInfo
    today_il = datetime.now(ZoneInfo('Asia/Jerusalem')).strftime('%Y-%m-%d')
    default_start = request.args.get('start', today_il)
    default_end = request.args.get('end', today_il)
    return render_template('admin/calls_report.html',
        default_start=default_start, default_end=default_end)


@admin_bp.route('/calls-report/export')
@login_required
def calls_report_export():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from models import CallLog
    from collections import defaultdict

    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    if not start_str or not end_str:
        flash('יש לבחור טווח תאריכים', 'error')
        return redirect(url_for('admin.calls_report'))

    start_utc, end_utc = _il_day_range_to_utc(start_str, end_str)

    calls = CallLog.query.filter(
        CallLog.started_at >= start_utc,
        CallLog.started_at < end_utc
    ).all()

    agg = defaultdict(lambda: {'count': 0, 'total_seconds': 0, 'incomplete': 0})
    for c in calls:
        a = agg[c.phone]
        a['count'] += 1
        if c.duration_seconds is not None:
            a['total_seconds'] += c.duration_seconds
        else:
            a['incomplete'] += 1  # שיחה שעדיין לא נרשם לה סיום (ננטשה/תקלה)

    def fmt_dur(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'פילוח שיחות'
    ws.sheet_view.rightToLeft = True

    headers = ['מספר טלפון', 'מספר שיחות', 'זמן שיחה כולל', 'זמן שיחה כולל (שניות)', 'שיחות ללא סיום רשום']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2563EB', end_color='2563EB', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')

    rows = sorted(agg.items(), key=lambda kv: -kv[1]['total_seconds'])
    for phone, a in rows:
        ws.append([phone, a['count'], fmt_dur(a['total_seconds']), a['total_seconds'], a['incomplete']])

    total_calls = sum(a['count'] for a in agg.values())
    total_seconds = sum(a['total_seconds'] for a in agg.values())
    ws.append([])
    ws.append(['סה"כ', total_calls, fmt_dur(total_seconds), total_seconds, ''])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    for col in ws.columns:
        max_length = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max(max_length + 2, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'calls_report_{start_str}_to_{end_str}.xlsx'
    )


# ==================== הודעה תפוצתית לכלל הלקוחות ====================
# הודעה חד-פעמית שנשלחת לכל הלקוחות עם כתובת מייל רשומה (לא קשור לשיחת
# CustomerMessage/thread ספציפית - זו הודעה כללית, לא שיחה עם לקוח יחיד).
# יש "שליחת ניסיון" לכתובת אחת לפני שמאשרים שליחה אמיתית לכולם - הטופס
# תמיד נטען מחדש עם אותו נושא/תוכן שהוזנו (render_template ולא redirect),
# כדי שכתיבת ניסיון לא תמחק את ההודעה שעדיין לא נשלחה לכולם.

def _broadcast_recipient_emails():
    """כתובות מייל ייחודיות של כל הלקוחות עם מייל רשום, בלי קשר לחסימה/יתרה -
    זו הודעה כללית, לא תמלול בתשלום."""
    rows = Customer.query.filter(Customer.email.isnot(None), Customer.email != '').all()
    seen = set()
    emails = []
    for c in rows:
        e = (c.email or '').strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            emails.append(e)
    return emails


def _broadcast_email_html(subject, body):
    """המייל המלא - אותה פונקציה משמשת לתצוגה המקדימה בעמוד, לשליחת הניסיון ולשליחה לכולם,
    כך שמה שרואים בצד הוא בדיוק מה שיישלח. התוכן מהעורך המעוצב (HTML מנוקה, ראה
    services/rich_email.py) או טקסט רגיל."""
    from markupsafe import escape
    from services import email_layout as L
    from services.rich_email import body_block
    card = L.title(str(escape(subject))) + body_block(body) + L.signature(can_reply=True)
    return L.wrap(card)


def _send_broadcast_email(to_email, subject, html):
    """Reply-To = כתובת הקליטה של המערכת: תשובה של לקוח נכנסת לממשק (להתכתבות של הלקוח
    לפי כתובת המייל שלו, או לתיבה הכללית) - לא הולכת לאיבוד."""
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email, ReplyTo
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    message = Mail(
        from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
        to_emails=to_email,
        subject=subject,
        html_content=html,
    )
    message.reply_to = ReplyTo(TRANSCRIBE_INBOUND_EMAIL, 'תמלול פון')
    sg.send(message)


def _send_broadcast_bg(subject, html, recipient_emails):
    """רץ ב-thread נפרד: שולח לכל הנמענים ברצף (לא thread לכל מייל - כדי לא
    להציף בהרבה threads כשיש הרבה לקוחות), עם לוג שגיאה לכל כתובת שנכשלה
    בלי לעצור את שאר השליחה."""
    sent, failed = 0, 0
    for email in recipient_emails:
        try:
            _send_broadcast_email(email, subject, html)
            sent += 1
        except Exception as e:
            failed += 1
            log.error(f"broadcast send failed (to={email}): {e}")
    log.info(f"הודעה תפוצתית הסתיימה: נשלחו {sent}, נכשלו {failed} (מתוך {len(recipient_emails)})")


@admin_bp.route('/broadcast', methods=['GET'])
@login_required
def broadcast_message():
    return render_template(
        'admin/broadcast.html', voice_broadcasts=_voice_messages_for(None),
        subject='', body='', test_email='',
        recipient_count=len(_broadcast_recipient_emails()),
        result=None,
    )


@admin_bp.route('/broadcast/preview', methods=['POST'])
@login_required
def broadcast_preview():
    """תצוגה מקדימה חיה בצד העורך - בדיוק ה-HTML שיישלח."""
    subject = (request.form.get('subject') or '').strip() or '(בלי נושא)'
    body = request.form.get('body') or ''
    resp = make_response(_broadcast_email_html(subject, body))
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@admin_bp.route('/broadcast/test', methods=['POST'])
@login_required
def broadcast_test():
    from services.rich_email import plain_text
    subject = (request.form.get('subject') or '').strip()
    body = (request.form.get('body') or '').strip()
    test_email = (request.form.get('test_email') or '').strip()
    recipient_count = len(_broadcast_recipient_emails())

    result = None
    if not subject or not plain_text(body):
        result = {'ok': False, 'text': 'יש למלא נושא ותוכן לפני שליחת ניסיון.'}
    elif not test_email:
        result = {'ok': False, 'text': 'יש להזין כתובת מייל לבדיקה.'}
    else:
        try:
            html = _broadcast_email_html(subject, body)
            _send_broadcast_email(test_email, f'[בדיקה] {subject}', html)
            result = {'ok': True, 'text': f'נשלח מייל בדיקה אל {test_email}. ההודעה נשארה כאן למטה - אפשר להמשיך לערוך או ללחוץ "שלח לכולם".'}
        except Exception as e:
            log.error(f"broadcast test send failed: {e}")
            result = {'ok': False, 'text': f'שגיאה בשליחת מייל הבדיקה: {e}'}

    return render_template(
        'admin/broadcast.html', voice_broadcasts=_voice_messages_for(None),
        subject=subject, body=body, test_email=test_email,
        recipient_count=recipient_count, result=result,
    )


@admin_bp.route('/broadcast/send', methods=['POST'])
@login_required
def broadcast_send():
    import threading
    from services.rich_email import plain_text
    subject = (request.form.get('subject') or '').strip()
    body = (request.form.get('body') or '').strip()
    recipient_emails = _broadcast_recipient_emails()

    result = None
    if not subject or not plain_text(body):
        result = {'ok': False, 'text': 'יש למלא נושא ותוכן.'}
    elif not recipient_emails:
        result = {'ok': False, 'text': 'אין כרגע אף לקוח עם מייל פעיל לשלוח אליו.'}
    else:
        html = _broadcast_email_html(subject, body)
        threading.Thread(
            target=_send_broadcast_bg,
            args=(subject, html, recipient_emails),
            daemon=True,
        ).start()
        result = {'ok': True, 'text': f'ההודעה נשלחת ברקע ל-{len(recipient_emails)} לקוחות. זה עשוי לקחת כמה דקות (ראה יומן השרת לפירוט תוצאה סופית).'}

    return render_template(
        'admin/broadcast.html', voice_broadcasts=_voice_messages_for(None),
        subject=subject, body=body, test_email='',
        recipient_count=len(recipient_emails), result=result,
    )


# ==================== תיבה כללית - מיילים ללא זיהוי לקוח ====================
# שרשורים עם גורמים שלא זוהו כלקוח רשום (לא נמצא מספר טלפון בנושא שתואם
# ללקוח קיים) - ראה routes/email_inbound.py: _find_customer_by_phone_anywhere.
# המנהל יכול להשיב במייל (בדיוק כמו שיחה עם לקוח - הצד השני יכול להשיב
# בחזרה וזה ימשיך את אותו שרשור), למחוק שרשור, או לשייך אותו ידנית ללקוח -
# שיוך מעתיק את כל ההודעות לתוך conversation_threads של אותו לקוח ומוחק
# את השרשור מהתיבה הכללית.

def _send_inbox_reply_email(to_email, subject, body, message_id, in_reply_to=None):
    """כמו _send_customer_conversation_email, אבל לשרשור אנונימי (לפי מייל,
    לא לקוח רשום) - נשלח ברקע (thread נפרד) כדי לא לעכב את התגובה למנהל."""
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, ReplyTo, Header

        html = _service_message_html(body, phone=None)

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
            to_emails=to_email,
            subject=subject,
            html_content=html,
        )
        message.reply_to = ReplyTo(TRANSCRIBE_INBOUND_EMAIL, 'תמלול פון')
        message.header = Header('Message-ID', message_id)
        if in_reply_to:
            message.header = Header('In-Reply-To', in_reply_to)
            message.header = Header('References', in_reply_to)
        sg.send(message)
    except Exception as e:
        log.error(f"send_inbox_reply_email failed (to={to_email}): {e}")


@admin_bp.route('/inbox')
@login_required
def general_inbox():
    from models import GeneralInboxMessage
    threads = GeneralInboxMessage.query.order_by(GeneralInboxMessage.updated_at.desc()).all()
    # שימו לב: בכוונה לא מסמנים כאן כלום כ"נקרא" - רק צפייה בפועל בשרשור ספציפי
    # (inbox_thread_detail למטה) מסמנת אותו. אחרת עצם טעינת הרשימה הזו הייתה
    # מנקה את כל ההתראות במחי יד, גם עבור שרשורים שלא נפתחו בפועל בכלל.
    return render_template('admin/inbox.html', threads=threads)


@admin_bp.route('/inbox/<int:id>')
@login_required
def inbox_thread_detail(id):
    from models import GeneralInboxMessage
    thread = GeneralInboxMessage.query.get_or_404(id)
    if not thread.is_read and request.headers.get('X-Sec-Purpose') != 'prefetch':
        thread.is_read = True
        db.session.commit()
    return render_template('admin/inbox_thread.html', thread=thread,
                           attachments=_attachments_for('inbox_message_id', [m.id for m in thread.messages]))


@admin_bp.route('/inbox/<int:id>/reply', methods=['POST'])
@login_required
def reply_inbox_thread(id):
    import threading, uuid as _uuid
    from models import GeneralInboxMessage, InboxMessage
    thread = GeneralInboxMessage.query.get_or_404(id)
    body = (request.form.get('body') or '').strip()
    if not body:
        flash('יש להזין תוכן להודעה')
        return redirect(url_for('admin.inbox_thread_detail', id=id))

    prev_out = InboxMessage.query.filter_by(thread_id=thread.id, direction='out') \
        .filter(InboxMessage.message_id.isnot(None)) \
        .order_by(InboxMessage.created_at.desc()).first()
    domain = (os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@tamlulphone.co.il')).split('@')[-1]
    new_message_id = f"<inbox-{thread.id}-{_uuid.uuid4().hex}@{domain}>"

    msg = InboxMessage(thread_id=thread.id, direction='out', body=body, message_id=new_message_id)
    db.session.add(msg)
    thread.is_read = True
    db.session.commit()

    reply_subject = thread.subject or 'תמלול פון'
    if not reply_subject.lower().startswith('re:'):
        reply_subject = f'Re: {reply_subject}'
    in_reply_to = prev_out.message_id if prev_out else None
    threading.Thread(
        target=_send_inbox_reply_email,
        args=(thread.from_email, reply_subject, body, new_message_id, in_reply_to),
        daemon=True,
    ).start()

    flash('ההודעה נשלחת...')
    return redirect(url_for('admin.inbox_thread_detail', id=id))


@admin_bp.route('/inbox/<int:id>/delete', methods=['POST'])
@login_required
def delete_inbox_message(id):
    from models import GeneralInboxMessage, InboxMessage
    thread = GeneralInboxMessage.query.get_or_404(id)
    from models import MessageAttachment
    ids = [m.id for m in InboxMessage.query.filter_by(thread_id=thread.id).all()]
    if ids:
        MessageAttachment.query.filter(MessageAttachment.inbox_message_id.in_(ids)).delete(synchronize_session=False)
    InboxMessage.query.filter_by(thread_id=thread.id).delete()
    db.session.delete(thread)
    db.session.commit()
    flash('השרשור נמחק')
    return redirect(url_for('admin.general_inbox'))


@admin_bp.route('/inbox/<int:id>/assign', methods=['POST'])
@login_required
def assign_inbox_message(id):
    """משייך שרשור מהתיבה הכללית ללקוח לפי מספר טלפון - מעתיק את כל ההודעות
    (בשני הכיוונים, לפי הסדר) לתוך שיחה חדשה בחשבון שלו, ומוחק את השרשור
    מהתיבה הכללית."""
    from models import GeneralInboxMessage, InboxMessage, CustomerMessage, ConversationThread
    from routes.email_inbound import _normalize_israeli_phone
    thread = GeneralInboxMessage.query.get_or_404(id)
    phone = _normalize_israeli_phone(request.form.get('phone', ''))

    from services.customer_phones import find_customer_by_phone
    customer = find_customer_by_phone(phone) if phone else None
    if not customer:
        flash('לא נמצא לקוח עם מספר הטלפון הזה')
        return redirect(url_for('admin.inbox_thread_detail', id=id))

    new_thread = ConversationThread(customer_id=customer.id)
    db.session.add(new_thread)
    db.session.flush()

    inbox_messages = InboxMessage.query.filter_by(thread_id=thread.id) \
        .order_by(InboxMessage.created_at.asc()).all()
    for i, m in enumerate(inbox_messages):
        body = m.body
        if i == 0 and thread.subject:
            body = f'[נושא מקורי: {thread.subject}]\n{body}'
        new_msg = CustomerMessage(
            thread_id=new_thread.id, customer_id=customer.id,
            direction=m.direction, body=body, is_read=True,
        )
        db.session.add(new_msg)
        db.session.flush()
        from models import MessageAttachment
        MessageAttachment.query.filter_by(inbox_message_id=m.id).update(
            {'inbox_message_id': None, 'customer_message_id': new_msg.id}, synchronize_session=False)

    InboxMessage.query.filter_by(thread_id=thread.id).delete()
    db.session.delete(thread)
    db.session.commit()
    flash(f'השרשור שויך ללקוח {customer.phone}' + (f' ({customer.name})' if customer.name else ''))
    return redirect(url_for('admin.customer_detail', id=customer.id))


# ===================== העלאה ידנית להקראה + קבצים מצורפים להודעות =====================

def _attachments_for(field, ids):
    """{message_id: [MessageAttachment בלי התוכן עצמו]} - לתצוגה ליד כל הודעה."""
    from sqlalchemy.orm import defer
    from models import MessageAttachment
    if not ids:
        return {}
    col = getattr(MessageAttachment, field)
    out = {}
    for a in MessageAttachment.query.options(defer(MessageAttachment.file_data)).filter(col.in_(ids)) \
            .order_by(MessageAttachment.id).all():
        out.setdefault(getattr(a, field), []).append(a)
    return out


@admin_bp.route('/customers/<int:id>/manuscript-upload', methods=['POST'])
@login_required
def manuscript_upload(id):
    """המנהל מעלה קבצים להקראה עבור הלקוח (גם אם לא נשלחו כראוי). כמה קבצים / ZIP
    מאוחדים לקובץ PDF אחד לפי סדר השמות. החיוב - כרגיל, בעת שליחת המסמך המוכן."""
    from services.manuscript_merge import create_manuscript_page
    customer = Customer.query.get_or_404(id)
    files = [(f.filename, f.read()) for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('לא נבחרו קבצים')
        return redirect(url_for('admin.customer_detail', id=id))
    try:
        page, note = create_manuscript_page(customer, files, (request.form.get('label') or '').strip() or None)
        flash(f'נוסף לתור ההקראה (דף #{page.id}): {note}')
    except ValueError as e:
        flash(f'לא נוסף להקראה: {e}')
    return redirect(url_for('admin.customer_detail', id=id))


@admin_bp.route('/attachments/<int:att_id>')
@login_required
def download_attachment(att_id):
    from io import BytesIO
    from flask import send_file
    from models import MessageAttachment
    a = MessageAttachment.query.get_or_404(att_id)
    return send_file(BytesIO(a.file_data or b''), download_name=a.filename or f'file_{a.id}',
                     mimetype=a.content_type or 'application/octet-stream', as_attachment=True)


@admin_bp.route('/attachments/to-manuscript', methods=['POST'])
@login_required
def attachments_to_manuscript():
    """מעביר את הקבצים של הודעה אחת להקראה: בשיחה עם לקוח - ללקוח הזה; בתיבה
    הכללית - ללקוח לפי הטלפון שהמנהל הקליד."""
    from models import MessageAttachment, CustomerMessage, InboxMessage
    from services.manuscript_merge import create_manuscript_page
    cmsg_id = request.form.get('customer_message_id', type=int)
    imsg_id = request.form.get('inbox_message_id', type=int)
    if cmsg_id:
        msg = CustomerMessage.query.get_or_404(cmsg_id)
        customer = Customer.query.get(msg.customer_id)
        atts = MessageAttachment.query.filter_by(customer_message_id=cmsg_id).order_by(MessageAttachment.id).all()
        back = url_for('admin.customer_detail', id=customer.id) + f'#thread-{msg.thread_id}'
    elif imsg_id:
        msg = InboxMessage.query.get_or_404(imsg_id)
        back = url_for('admin.inbox_thread_detail', id=msg.thread_id)
        from routes.email_inbound import _normalize_israeli_phone
        from services.customer_phones import find_customer_by_phone
        phone = _normalize_israeli_phone(request.form.get('phone', ''))
        customer = find_customer_by_phone(phone) if phone else None
        if not customer:
            flash('לא נמצא לקוח עם מספר הטלפון הזה')
            return redirect(back)
        atts = MessageAttachment.query.filter_by(inbox_message_id=imsg_id).order_by(MessageAttachment.id).all()
    else:
        flash('בקשה לא תקינה')
        return redirect(url_for('admin.dashboard'))
    if not atts:
        flash('אין בהודעה הזו קבצים')
        return redirect(back)
    try:
        page, note = create_manuscript_page(customer, [(a.filename, a.file_data) for a in atts])
        for a in atts:
            a.manuscript_page_id = page.id
        db.session.commit()
        flash(f'הקבצים הועברו להקראה של {customer.name or customer.phone} (דף #{page.id}): {note}')
    except ValueError as e:
        flash(f'לא הועבר להקראה: {e}')
    return redirect(back)


# ===================== מוסדות (ניהול מוסד - super-admin) =====================

@admin_bp.route('/institutions')
@login_required
def institutions():
    from models import Institution
    insts = Institution.query.order_by(Institution.created_at.desc()).all()
    return render_template('admin/institutions.html', institutions=insts)


@admin_bp.route('/institutions/create', methods=['POST'])
@login_required
def create_institution():
    import random, string
    from models import Institution
    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip() or None
    phone = (request.form.get('phone') or '').strip() or None
    if not name or not (email or phone):
        flash('יש להזין שם, ולפחות מייל או טלפון')
        return redirect(url_for('admin.institutions'))

    inst = Institution(
        name=name, email=email, phone=phone,
        login_code=__import__('services.security', fromlist=['new_login_code']).new_login_code(),
    )
    db.session.add(inst)
    db.session.commit()
    flash(f'המוסד "{name}" נוצר - קוד כניסה ראשוני: {inst.login_code}')
    return redirect(url_for('admin.institutions'))


@admin_bp.route('/institutions/<int:inst_id>')
@login_required
def institution_detail(inst_id):
    from models import Institution, Customer, InstitutionChargeLog
    inst = Institution.query.get_or_404(inst_id)
    students = Customer.query.filter_by(institution_id=inst.id).all()
    charges = InstitutionChargeLog.query.filter_by(institution_id=inst.id).order_by(InstitutionChargeLog.created_at.desc()).limit(20).all()
    return render_template('admin/institution_detail.html', inst=inst, students=students, charges=charges)


@admin_bp.route('/institutions/<int:inst_id>/adjust-balance', methods=['POST'])
@login_required
def adjust_institution_balance(inst_id):
    from models import Institution
    inst = Institution.query.get_or_404(inst_id)
    try:
        amount = float(request.form.get('amount'))
    except (TypeError, ValueError):
        flash('סכום לא תקין')
        return redirect(url_for('admin.institution_detail', inst_id=inst.id))
    inst.balance = (inst.balance or 0) + amount
    db.session.commit()
    if amount > 0:
        try:
            from routes.dictate import trigger_pending_manuscripts
            trigger_pending_manuscripts(institution_id=inst.id)
        except Exception:
            pass
    flash('היתרה עודכנה')
    return redirect(url_for('admin.institution_detail', inst_id=inst.id))


@admin_bp.route('/institutions/<int:inst_id>/toggle-block', methods=['POST'])
@login_required
def toggle_institution_block(inst_id):
    from models import Institution
    inst = Institution.query.get_or_404(inst_id)
    inst.is_blocked = not inst.is_blocked
    db.session.commit()
    flash('המוסד נחסם' if inst.is_blocked else 'החסימה בוטלה')
    return redirect(url_for('admin.institution_detail', inst_id=inst.id))


@admin_bp.route('/institutions/<int:inst_id>/delete', methods=['POST'])
@login_required
def delete_institution(inst_id):
    """מחיקת מוסד. התלמידים לא נמחקים - הם מנותקים מהמוסד והופכים ללקוחות רגילים
    (בדיוק כמו "הורדת תלמיד" בממשק המוסד), עם היתרה וההיסטוריה שלהם. נמחקים:
    המוסד, יומן החיובים שלו, התמלולים שהעלה בעצמו ובקשות קישור תלמידים.
    יתרת המוסד עצמו (Institution.balance) אובדת - לכן נדרש להקליד את שם המוסד."""
    from models import Institution, InstitutionChargeLog, InstitutionUpload, StudentLinkRequest
    inst = Institution.query.get_or_404(inst_id)
    if (request.form.get('confirm_name') or '').strip() != (inst.name or '').strip():
        flash('המוסד לא נמחק - שם המוסד שהוקלד לא תואם')
        return redirect(url_for('admin.institution_detail', inst_id=inst.id))
    name, balance = inst.name, float(inst.balance or 0)
    students = Customer.query.filter_by(institution_id=inst.id).all()
    for st in students:
        st.institution_id = None
        st.student_number = None
        st.self_funded = 0.0
        st.is_institution_self = False
    InstitutionChargeLog.query.filter_by(institution_id=inst.id).delete()
    InstitutionUpload.query.filter_by(institution_id=inst.id).delete()
    StudentLinkRequest.query.filter_by(institution_id=inst.id).delete()
    db.session.delete(inst)
    db.session.commit()
    logging.getLogger(__name__).warning(
        f'institution {inst_id} "{name}" deleted by {getattr(current_user, "username", "?")}: '
        f'{len(students)} students unlinked, institution balance {balance:.2f} discarded')
    flash(f'המוסד "{name}" נמחק. {len(students)} תלמידים הפכו ללקוחות רגילים (עם היתרה שלהם)'
          + (f'. יתרת המוסד (₪{balance:.2f}) נמחקה' if balance else ''))
    return redirect(url_for('admin.institutions'))


@admin_bp.route('/institutions/<int:inst_id>/regenerate-code', methods=['POST'])
@login_required
def regenerate_institution_code(inst_id):
    import random, string
    from models import Institution
    inst = Institution.query.get_or_404(inst_id)
    inst.login_code = __import__('services.security', fromlist=['new_login_code']).new_login_code()
    inst.password_hash = None  # חוזר למצב "כניסה ראשונה" עם הקוד החדש
    db.session.commit()
    flash(f'קוד כניסה חדש למוסד: {inst.login_code}')
    return redirect(url_for('admin.institution_detail', inst_id=inst.id))


# ==================== בקשות פיתוח + הסברים מוכנים על פיתוחים קיימים ====================
# עמוד אחד עם שתי לשוניות: (1) בקשות שלקוחות ביקשו / רעיונות לפיתוח עתידי, עם סטטוס;
# (2) הסברים קצרים מוכנים על השירותים הקיימים - להעתקה ושליחה ללקוחות (גם להודעה התפוצתית).

DEV_STATUSES = [('new', 'חדש'), ('planned', 'מתוכנן'), ('in_progress', 'בעבודה'), ('done', 'בוצע'), ('rejected', 'לא יבוצע')]
DEV_PRIORITIES = [('high', 'דחוף'), ('normal', 'רגיל'), ('low', 'נמוך')]

_DEFAULT_FEATURES = [
    ('⚡ מערכת מהירה', 'המעבר בין השלוחות בטלפון מהיר, בלי המתנות מיותרות.'),
    ('🎯 תמלול מדויק', 'מנוע התמלול כותב שמות של רבנים, ספרים, מוסדות ומקומות בדיוק כפי שנאמרו.'),
    ('📂 שלוחה 8 - המסמכים שלכם', 'בשלוחה 8 שומעים את רשימת המסמכים הקודמים (עד 200 אחרונים): תמלולים, כתבי יד וקלדנות דיגיטלית. '
     'כל מסמך אפשר לקבל שוב - במייל ללא עלות, או בפקס.'),
    ('🎙️ שליטה בהקלטה', 'בסיום ההקלטה אפשר לבחור: לשלוח לתמלול, להקליט מחדש, או להמשיך להקליט מהמקום שבו עצרתם.'),
    ('🆓 הקלטה שנקטעה - בלי חיוב', 'הקלטה קצרה מ-6 שניות (למשל לחיצה בטעות) לא נשלחת לתמלול ולא מחויבת.'),
    ('📧 שליחת קבצים במייל', 'אפשר לשלוח הקלטות גדולות במיוחד, קישור מ-Google Drive או מג\'מבו מייל במקום קובץ מצורף, '
     'ולקלדנות דיגיטלית - כמה תמונות/PDF יחד או תיקייה דחוסה (ZIP), שיאוחדו למסמך אחד לפי סדר שמות הקבצים.'),
    ('✉️ מיילים ברורים', 'עיצוב קריא לכל המיילים, קישור ישיר לטעינת הארנק בכל מייל, הסבר מיידי אם נשלח מייל בלי מספר טלפון בנושא, '
     'ואפשר להשיב ישירות למיילים שלנו - התשובה מגיעה אלינו.'),
    ('💳 עדכון מייל בטעינת ארנק', 'בטעינת ארנק בנדרים פלוס, כתבו בשדה ההערות "עדכון מייל" - והכתובת שהזנתם בתשלום תעודכן אצלנו אוטומטית.'),
    ('📑 דוח עלויות', 'אפשר לבקש דוח עלויות: קובץ מסודר עם סיכום לפי סוג שירות ופירוט של כל התנועות לתקופה שתבחרו.'),
]


def _dev_seed_features():
    """פעם אחת בלבד (גם אם אחר כך מוחקים הכל) - ממלא את ההסברים ההתחלתיים."""
    from models import FeatureNote
    if get_setting('feature_notes_seeded', '') == '1':
        return
    if FeatureNote.query.count() == 0:
        for i, (t, b) in enumerate(_DEFAULT_FEATURES):
            db.session.add(FeatureNote(title=t, body=b, position=i))
        db.session.commit()
    set_setting('feature_notes_seeded', '1')


@admin_bp.route('/dev-requests')
@login_required
def dev_requests():
    from models import DevRequest, FeatureNote
    _dev_seed_features()
    tab = request.args.get('tab', 'requests')
    status = request.args.get('status', 'open')
    q = DevRequest.query
    if status == 'open':
        q = q.filter(DevRequest.status.in_(('new', 'planned', 'in_progress')))
    elif status in dict(DEV_STATUSES):
        q = q.filter(DevRequest.status == status)
    from sqlalchemy import case
    prio_order = case({'high': 0, 'normal': 1, 'low': 2}, value=DevRequest.priority, else_=1)
    reqs = q.order_by(prio_order, DevRequest.created_at.desc()).all()
    counts = dict(db.session.query(DevRequest.status, func.count(DevRequest.id)).group_by(DevRequest.status).all())
    counts['open'] = sum(counts.get(k, 0) for k in ('new', 'planned', 'in_progress'))
    features = FeatureNote.query.order_by(FeatureNote.position, FeatureNote.id).all()
    return render_template('admin/dev_requests.html', tab=tab, status=status, reqs=reqs, counts=counts,
                           features=features, statuses=DEV_STATUSES, priorities=DEV_PRIORITIES)


@admin_bp.route('/dev-requests/save', methods=['POST'])
@login_required
def dev_request_save():
    from models import DevRequest
    rid = request.form.get('id', type=int)
    r = DevRequest.query.get_or_404(rid) if rid else DevRequest()
    title = (request.form.get('title') or '').strip()
    if not title:
        flash('יש לכתוב כותרת לבקשה')
        return redirect(url_for('admin.dev_requests'))
    r.title = title[:300]
    r.details = (request.form.get('details') or '').strip()
    r.requested_by = (request.form.get('requested_by') or '').strip()[:200]
    r.priority = request.form.get('priority') if request.form.get('priority') in dict(DEV_PRIORITIES) else 'normal'
    new_status = request.form.get('status') or r.status or 'new'
    if new_status in dict(DEV_STATUSES):
        if new_status == 'done' and r.status != 'done':
            r.done_at = datetime.utcnow()
        r.status = new_status
    r.updated_at = datetime.utcnow()
    if not rid:
        db.session.add(r)
    db.session.commit()
    flash('הבקשה נשמרה' if rid else 'הבקשה נוספה')
    return redirect(url_for('admin.dev_requests', status=request.form.get('back_status', 'open')))


@admin_bp.route('/dev-requests/<int:rid>/status', methods=['POST'])
@login_required
def dev_request_status(rid):
    from models import DevRequest
    r = DevRequest.query.get_or_404(rid)
    st = request.form.get('status')
    if st in dict(DEV_STATUSES):
        if st == 'done' and r.status != 'done':
            r.done_at = datetime.utcnow()
        r.status = st
        r.updated_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('admin.dev_requests', status=request.form.get('back_status', 'open')))


@admin_bp.route('/dev-requests/<int:rid>/delete', methods=['POST'])
@login_required
def dev_request_delete(rid):
    from models import DevRequest
    db.session.delete(DevRequest.query.get_or_404(rid))
    db.session.commit()
    flash('הבקשה נמחקה')
    return redirect(url_for('admin.dev_requests', status=request.form.get('back_status', 'open')))


@admin_bp.route('/dev-requests/<int:rid>/to-feature', methods=['POST'])
@login_required
def dev_request_to_feature(rid):
    """בקשה שבוצעה -> הסבר מוכן ללקוחות (טיוטה מהכותרת והפירוט, לעריכה בלשונית ההסברים)."""
    from models import DevRequest, FeatureNote
    r = DevRequest.query.get_or_404(rid)
    pos = (db.session.query(func.max(FeatureNote.position)).scalar() or 0) + 1
    db.session.add(FeatureNote(title=r.title, body=r.details or '', position=pos))
    if r.status != 'done':
        r.status, r.done_at = 'done', datetime.utcnow()
    db.session.commit()
    flash('נוסף הסבר חדש בלשונית "פיתוחים קיימים" - כדאי לעבור על הניסוח לפני ששולחים ללקוחות')
    return redirect(url_for('admin.dev_requests', tab='features'))


@admin_bp.route('/dev-features/save', methods=['POST'])
@login_required
def dev_feature_save():
    from models import FeatureNote
    fid = request.form.get('id', type=int)
    title = (request.form.get('title') or '').strip()
    if not title:
        flash('יש לכתוב כותרת')
        return redirect(url_for('admin.dev_requests', tab='features'))
    f = FeatureNote.query.get_or_404(fid) if fid else FeatureNote(
        position=(db.session.query(func.max(FeatureNote.position)).scalar() or 0) + 1)
    f.title = title[:200]
    f.body = (request.form.get('body') or '').strip()
    f.updated_at = datetime.utcnow()
    if not fid:
        db.session.add(f)
    db.session.commit()
    flash('ההסבר נשמר')
    return redirect(url_for('admin.dev_requests', tab='features'))


@admin_bp.route('/dev-features/<int:fid>/move', methods=['POST'])
@login_required
def dev_feature_move(fid):
    from models import FeatureNote
    items = FeatureNote.query.order_by(FeatureNote.position, FeatureNote.id).all()
    idx = next((i for i, x in enumerate(items) if x.id == fid), None)
    j = idx + (-1 if request.form.get('dir') == 'up' else 1) if idx is not None else None
    if idx is not None and 0 <= j < len(items):
        items[idx], items[j] = items[j], items[idx]
        for i, x in enumerate(items):
            x.position = i
        db.session.commit()
    return redirect(url_for('admin.dev_requests', tab='features'))


@admin_bp.route('/dev-features/<int:fid>/delete', methods=['POST'])
@login_required
def dev_feature_delete(fid):
    from models import FeatureNote
    db.session.delete(FeatureNote.query.get_or_404(fid))
    db.session.commit()
    flash('ההסבר נמחק')
    return redirect(url_for('admin.dev_requests', tab='features'))
