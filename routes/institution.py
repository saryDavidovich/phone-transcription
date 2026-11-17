"""
ממשק ניהול מוסד - נפרד לגמרי מממשק מנהל-העל (routes/admin.py).
מוסד מתחבר לפי מייל או טלפון + קוד שיצר מנהל-העל. בכניסה ראשונה הוא
מתבקש להגדיר סיסמה קבועה (מחליפה את הקוד), ויכול לחבר גם כניסה עם גוגל.

כניסה עם גוגל (routes /auth/google/start ו-/auth/google/callback) דורשת
שני משתני סביבה: GOOGLE_OAUTH_CLIENT_ID ו-GOOGLE_OAUTH_CLIENT_SECRET,
שיוצרים ב-Google Cloud Console (OAuth 2.0 Client ID, מסוג Web application),
עם Authorized redirect URI שמצביע ל-<APP_URL>/institution/auth/google/callback.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from datetime import datetime
from app import db
from models import Institution, Customer, Recording, Transaction
import logging
import os

log = logging.getLogger(__name__)

institution_bp = Blueprint('institution', __name__, template_folder='../templates/institution')


def student_usage_limit_error(customer):
    """בודק אם תלמיד חרג ממגבלת השימוש היומית/שבועית שהמוסד שלו קבע
    (Institution.max_minutes_per_period + limit_period). מחזיר הודעת שגיאה
    אם חרג, אחרת None. משמש גם בהעלאה ידנית ע"י המוסד (institution_students.py)
    וגם בזרימת התמלול הרגילה מה-IVR (routes/api.py /api/transcribe) - שם
    זה קורה בפועל לרוב התלמידים."""
    if not customer or not customer.institution_id:
        return None
    inst = customer.institution
    if not inst or not inst.max_minutes_per_period or not inst.limit_period:
        return None

    from datetime import timedelta
    from sqlalchemy import func
    since = datetime.utcnow() - (timedelta(days=1) if inst.limit_period == 'day' else timedelta(days=7))
    used_seconds = db.session.query(func.coalesce(func.sum(Recording.duration_seconds), 0)).filter(
        Recording.customer_id == customer.id, Recording.created_at >= since
    ).scalar() or 0
    used_minutes = used_seconds / 60
    if used_minutes >= inst.max_minutes_per_period:
        period_label = 'ביום' if inst.limit_period == 'day' else 'בשבוע'
        return f'עברת את מגבלת השימוש שהמוסד קבע ({inst.max_minutes_per_period:.0f} דקות {period_label}). נסה שוב מאוחר יותר.'
    return None


def institution_login_required(view):
    """כמו login_required, אבל מוודא שההתחברות היא של מוסד ולא מנהל-על,
    ומפנה לעמוד ההתחברות הנכון (login_manager.login_view גלובלי מצביע
    לעמוד ההתחברות של מנהל-העל, לא מתאים כאן)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, Institution):
            return redirect(url_for('institution.login'))
        from services import security as sec
        if current_user.is_blocked or not sec.inst_session_valid(current_user):
            # הסיסמה הוחלפה/אופסה (או המוסד נחסם) אחרי שהסשן הזה נפתח - מנתקים
            logout_user()
            session.pop('inst_fp', None)
            flash('יש להתחבר מחדש')
            return redirect(url_for('institution.login'))
        return view(*args, **kwargs)
    return wrapped


_GH_ICONS = 'https://raw.githubusercontent.com/saryDavidovich/phone-transcription/main/static/pwa/'


@institution_bp.route('/manifest.webmanifest')
def pwa_manifest():
    """התקנת אזור המוסד כתוכנה במחשב/בטלפון - סמל משלו (מסמך עם גלי קול, בצבעי אזור המוסד).
    הסמלים נטענים מגיטהאב (נטפרי מאשרים תמונות לפי כתובת), והציור הווקטורי - גיבוי."""
    import json
    from urllib.parse import quote
    from flask import make_response
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'pwa', 'inst-icon.svg')
    with open(path, encoding='utf-8') as f:
        svg = 'data:image/svg+xml,' + quote(f.read(), safe='')
    # השם מתחיל כמו כותרת העמוד ("מוסד - שם המוסד - ...") - כך החלון של התוכנה המותקנת מציג רק את
    # הכותרת, בלי להוסיף את שם התוכנה לפניה פעם נוספת
    inst_name = current_user.name if (current_user.is_authenticated and isinstance(current_user, Institution)) else ''
    data = {
        'name': f'מוסד - {inst_name}' if inst_name else 'מוסד', 'short_name': (inst_name or 'מוסד')[:24],
        'description': 'ניהול תלמידים, תמלולים וחיובים של המוסד',
        'lang': 'he', 'dir': 'rtl',
        'id': '/institution/', 'start_url': '/institution/', 'scope': '/institution/',
        'display': 'standalone', 'background_color': '#f3f5fa', 'theme_color': '#10b981',
        'icons': [
            {'src': _GH_ICONS + 'inst-icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': _GH_ICONS + 'inst-icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': _GH_ICONS + 'inst-icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': svg, 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'},
        ],
    }
    resp = make_response(json.dumps(data, ensure_ascii=False).replace('"data:', '"data\\u003a'))
    resp.mimetype = 'application/json'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@institution_bp.route('/sw.js')
def pwa_service_worker():
    from flask import send_from_directory
    resp = send_from_directory(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'pwa'),
                               'inst-sw.js', mimetype='application/javascript', max_age=0)
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/institution/'
    return resp


@institution_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = (request.form.get('identifier') or '').strip()
        code = (request.form.get('code') or '').strip()

        inst = Institution.query.filter(
            (Institution.email == identifier) | (Institution.phone == identifier)
        ).first() if identifier else None

        if not inst and identifier:
            # בדיקה אם הזהות שהוקלדה היא אחת ממורשי הכניסה הנוספים של מוסד
            # (ראה הגדרות מוסד > מורשי כניסה) - משתמשים באותה סיסמה/קוד
            # של המוסד עצמו, רק עם זהות התחברות שונה (מייל או טלפון).
            candidates = Institution.query.filter(Institution.authorized_logins.isnot(None)).all()
            for c in candidates:
                idents = [a.get('email') for a in (c.authorized_logins or []) if isinstance(a, dict) and a.get('email')]
                idents += [a.get('phone') for a in (c.authorized_logins or []) if isinstance(a, dict) and a.get('phone')]
                if identifier in idents:
                    inst = c
                    break

        from services import security as sec
        # הגנה מפני ניחוש סיסמאות/קודים: מגבילים לפי כתובת IP, וגם לפי החשבון עצמו
        # (קוד זמני הוא רק 6 ספרות - בלי הגבלה אפשר לנחש אותו).
        ip_key = 'ip:' + sec.client_ip()
        acct_key = f'inst:{inst.id}' if inst else None
        if sec.hits_blocked(ip_key, 8, 15 * 60) or (acct_key and sec.hits_blocked(acct_key, 10, 15 * 60)):
            flash('יותר מדי ניסיונות כניסה. נסו שוב בעוד כרבע שעה.')
            return render_template('institution/login.html')

        if not inst or inst.is_blocked:
            sec.hits_add(ip_key)
            flash('פרטי התחברות שגויים')
            return render_template('institution/login.html')

        if sec.inst_locked(inst):
            # נעילה בבסיס הנתונים (משותפת לכל השרתים ושורדת עדכון גרסה) - 10 ניסיונות שגויים = 15 דקות
            sec.hits_add(ip_key)
            flash('החשבון ננעל זמנית בגלל ניסיונות כניסה שגויים רבים. נסו שוב בעוד כרבע שעה, או השתמשו ב"שכחתי סיסמה".')
            return render_template('institution/login.html')

        ok = False
        via_code = False
        if inst.password_hash and check_password_hash(inst.password_hash, code):
            ok = True    # הסיסמה הקבועה
        elif sec.login_code_ok(inst, code):
            if not inst.password_hash:
                ok, via_code = True, True    # כניסה ראשונה עם הקוד שנתן מנהל-העל
            elif inst.reset_requested_at and (datetime.utcnow() - inst.reset_requested_at).total_seconds() < 30 * 60:
                ok, via_code = True, True    # "שכחתי סיסמה" - הקוד שנשלח למייל, תקף 30 דקות

        if not ok:
            sec.hits_add(ip_key)
            if acct_key:
                sec.hits_add(acct_key)
            sec.inst_register_fail(inst)
            db.session.commit()
            sec.log_inst_login(inst, 'code' if not inst.password_hash else 'password', False)
            flash('פרטי התחברות שגויים')
            return render_template('institution/login.html')

        if acct_key:
            sec.hits_clear(acct_key)
        sec.inst_clear_fails(inst)
        if not via_code and inst.reset_requested_at:
            inst.reset_requested_at = None   # נכנס עם הסיסמה הרגילה - בקשת האיפוס מתבטלת
        db.session.commit()
        session.clear()                      # סשן חדש בכל כניסה (מונע קיבוע סשן)
        login_user(inst)
        sec.inst_mark_session(inst)
        sec.log_inst_login(inst, 'code' if via_code else 'password', True)
        if via_code or not inst.password_hash:
            session['must_set_password'] = True
            return redirect(url_for('institution.set_password'))
        return redirect(url_for('institution.dashboard'))

    return render_template('institution/login.html')


@institution_bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    """נקרא מהמודל "שכחתי סיסמה" בדף הכניסה (fetch, בלי רענון עמוד).
    אם המייל תואם מוסד קיים - יוצר קוד כניסה זמני חדש, מאפס את הסיסמה
    הקבועה (כך שבכניסה הבאה עם הקוד יתבקש להגדיר סיסמה חדשה), ושולח את
    הקוד למייל. מטעמי פרטיות מחזירים תמיד הודעת הצלחה גנרית - גם אם המייל
    לא נמצא - כדי לא לחשוף אילו כתובות רשומות במערכת."""
    email = (request.get_json(silent=True) or {}).get('email', '').strip()
    generic_msg = 'אם המייל הזה רשום במערכת, נשלח אליו קוד כניסה זמני חדש.'
    if not email:
        return jsonify({'error': 'נא להזין כתובת מייל'}), 400

    from services import security as sec
    # הגבלת קצב: מונע הצפת תיבת הדואר של מוסד (ושל חשבון השליחה שלנו) בהודעות
    ip_key, mail_key = 'fp-ip:' + sec.client_ip(), 'fp-mail:' + email.lower()
    if sec.hits_blocked(ip_key, 5, 3600) or sec.hits_blocked(mail_key, 3, 3600):
        return jsonify({'ok': True, 'message': generic_msg})
    sec.hits_add(ip_key)
    sec.hits_add(mail_key)

    inst = Institution.query.filter_by(email=email).first()
    if inst and not inst.is_blocked:
        # הסיסמה הקיימת *לא* נמחקת - אחרת כל אדם זר יכול היה לנעול מוסד מחוץ לחשבון שלו
        # רק בהקלדת המייל שלו. הקוד החדש תקף 30 דקות ומאפשר להגדיר סיסמה חדשה.
        code = sec.issue_login_code(inst)
        inst.reset_requested_at = datetime.utcnow()
        db.session.commit()
        try:
            _send_login_code_email(inst, code)
        except Exception:
            log.exception('forgot_password: failed to send email')

    return jsonify({'ok': True, 'message': generic_msg})


def _send_login_code_email(inst, code):
    import sendgrid
    from services.email_layout import wrap_existing
    from sendgrid.helpers.mail import Mail, Email

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#10b981">קוד כניסה זמני חדש</h2>
<p style="line-height:1.8">שלום {inst.name},</p>
<p style="line-height:1.8">התקבלה בקשה לאיפוס הגישה לחשבון המוסד שלכם במערכת התמלול. הקוד הזמני החדש שלכם הוא:</p>
<div style="background:#f4f6fb;border-radius:10px;padding:16px;text-align:center;font-size:26px;font-weight:700;letter-spacing:2px;margin:16px 0">{code}</div>
<p style="line-height:1.8">היכנסו עם המייל/טלפון שלכם והקוד הזה, ותתבקשו להגדיר סיסמה קבועה חדשה.</p>
<p style="color:#6b7280;font-size:13px;line-height:1.6">אם לא ביקשתם זאת, ניתן להתעלם מהודעה זו.</p>
</div>'''

    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    message = Mail(
        from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
        to_emails=inst.email,
        subject='קוד כניסה זמני חדש - מערכת תמלול',
        html_content=wrap_existing(html, show_circles=False),
    )
    sg.send(message)


# ---------------------------------------------------------------------
# כניסה עם Google - ראו הערה בראש הקובץ לגבי משתני הסביבה הנדרשים
# ---------------------------------------------------------------------
GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


@institution_bp.route('/auth/google/start')
def google_auth_start():
    import secrets
    client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
    if not client_id:
        flash('כניסה עם Google לא הוגדרה עדיין בצד השרת')
        return redirect(url_for('institution.login'))

    state = secrets.token_urlsafe(24)
    session['google_oauth_state'] = state
    redirect_uri = url_for('institution.google_auth_callback', _external=True)
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'prompt': 'select_account',
    }
    import urllib.parse
    return redirect(f'{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}')


@institution_bp.route('/auth/google/callback')
def google_auth_callback():
    import requests as req

    if request.args.get('state') != session.pop('google_oauth_state', None):
        flash('אירעה שגיאת אימות (state), נסה שוב')
        return redirect(url_for('institution.login'))

    code = request.args.get('code')
    if not code:
        flash('ההתחברות עם Google בוטלה')
        return redirect(url_for('institution.login'))

    client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')
    redirect_uri = url_for('institution.google_auth_callback', _external=True)

    try:
        token_res = req.post(GOOGLE_TOKEN_URL, data={
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }, timeout=15)
        token_res.raise_for_status()
        access_token = token_res.json().get('access_token')

        userinfo_res = req.get(GOOGLE_USERINFO_URL, headers={'Authorization': f'Bearer {access_token}'}, timeout=15)
        userinfo_res.raise_for_status()
        userinfo = userinfo_res.json()
    except Exception:
        log.exception('google_auth_callback: token/userinfo exchange failed')
        flash('שגיאה בהתחברות עם Google, נסה שוב')
        return redirect(url_for('institution.login'))

    google_id = userinfo.get('sub')
    email = (userinfo.get('email') or '').strip()
    if not userinfo.get('email_verified'):
        # בלי אימות מייל אצל גוגל אסור לקשר לפי כתובת - אחרת אפשר להתחזות למוסד
        email = ''

    inst = Institution.query.filter_by(google_id=google_id).first() if google_id else None
    if not inst and email:
        # התחברות ראשונה עם גוגל - מקשרים לפי מייל תואם אם אין עדיין קישור
        inst = Institution.query.filter_by(email=email).first()
        if inst:
            inst.google_id = google_id
            db.session.commit()

    if not inst or inst.is_blocked:
        flash('לא נמצא מוסד עם חשבון הגוגל הזה. יש להתחבר קודם עם מייל/סיסמה כדי לקשר את החשבון.')
        return redirect(url_for('institution.login'))

    from services import security as sec
    session.clear()
    login_user(inst)
    sec.inst_mark_session(inst)
    sec.log_inst_login(inst, 'google', True)
    return redirect(url_for('institution.dashboard'))


def password_error(pw1, pw2):
    import re as _re
    if len(pw1) < 8 or not _re.search(r'\d', pw1) or not _re.search(r'[^\d]', pw1):
        return 'הסיסמה חייבת להיות באורך 8 תווים לפחות, ולכלול גם ספרות וגם אותיות'
    if len(set(pw1)) < 4:
        return 'הסיסמה פשוטה מדי - בחרו סיסמה מגוונת יותר'
    if pw1 != pw2:
        return 'הסיסמאות אינן תואמות'
    return None


@institution_bp.route('/set-password', methods=['GET', 'POST'])
@institution_login_required
def set_password():
    """מוצג בכניסה ראשונה בלבד (עוד אין password_hash) - מחליף את הקוד
    הזמני בסיסמה קבועה שהמוסד בוחר בעצמו."""
    if current_user.password_hash and not session.get('must_set_password'):
        # כבר יש סיסמה - החלפה רק דרך ההגדרות, עם הסיסמה הנוכחית
        return redirect(url_for('institution.settings'))
    if request.method == 'POST':
        pw1 = request.form.get('password') or ''
        pw2 = request.form.get('password2') or ''
        err = password_error(pw1, pw2)
        if err:
            flash(err)
        else:
            current_user.password_hash = generate_password_hash(pw1)
            current_user.reset_requested_at = None
            from services import security as sec
            sec.issue_login_code(current_user)   # הקוד הישן כבר לא תקף (את החדש לא מציגים לאף אחד)
            session.pop('must_set_password', None)
            db.session.commit()
            sec.inst_mark_session(current_user)  # הסשן הנוכחי נשאר מחובר; כל שאר המחוברים מתנתקים
            flash('הסיסמה הוגדרה בהצלחה')
            return redirect(url_for('institution.dashboard'))
    return render_template('institution/set_password.html')


@institution_bp.route('/logout')
@institution_login_required
def logout():
    logout_user()
    return redirect(url_for('institution.login'))


def ensure_institution_self_customer(institution):
    """מחזיר (ויוצר אם צריך, עצלנית - כמו routes/dictate.py._ensure_customer_fax_code)
    את "לקוח-הדמה" היחיד של המוסד הזה, המייצג את המוסד עצמו ולא תלמיד
    ספציפי - ראה models.Customer.is_institution_self. נועד להעלאת כתבי-יד
    כלליים של המוסד (למשל טופס שהמזכירות ממלאת, לא שייך לתלמיד בודד) דרך
    בדיוק אותם מסכים/routes של תלמיד רגיל (student_detail וכל שאר הפונקציות
    ב-routes/institution_students.py, ללא צורך בקוד כפול) - פשוט מפנים
    אליו לפי ה-id שלו כאילו הוא "תלמיד". אין לו טלפון/מספר תלמיד (שניהם
    None, מותר לפי models.Customer - כמה שורות עם NULL לא מתנגשות ב-unique)."""
    self_customer = Customer.query.filter_by(institution_id=institution.id, is_institution_self=True).first()
    if not self_customer:
        self_customer = Customer(
            institution_id=institution.id,
            is_institution_self=True,
            student_display_name=f'קלדנות דיגיטלית - {institution.name}',
            name=institution.name,
            balance=0.0,
        )
        db.session.add(self_customer)
        db.session.commit()
    return self_customer


@institution_bp.route('/manuscripts')
@institution_login_required
def self_manuscripts():
    """נקודת כניסה להעלאת/צפייה בכתבי-יד כלליים של המוסד עצמו (לא שייכים
    לתלמיד ספציפי) - ראה ensure_institution_self_customer למעלה. פשוט
    מפנה למסך תיק "תלמיד" הרגיל (student_detail) עם לקוח-הדמה של המוסד,
    שכבר תומך בדיוק במה שצריך (העלאת כתב-יד, צפייה, הורדה, הגהה נוספת)."""
    self_customer = ensure_institution_self_customer(current_user)
    return redirect(url_for('institution_students.student_detail', student_id=self_customer.id))


@institution_bp.route('/')
@institution_bp.route('/dashboard')
@institution_login_required
def dashboard():
    inst = current_user
    # מוציאים במפורש את לקוח-הדמה של המוסד עצמו (is_institution_self) -
    # הוא לא תלמיד אמיתי, אסור שיספר במספר התלמידים/סכום היתרות. משתמשים
    # ב-isnot(True) ולא ב-==False כדי לכלול גם שורות ישנות עם NULL (ברירת
    # המחדל בפועל לפני שהעמודה הזו נוספה).
    students = Customer.query.filter_by(institution_id=inst.id).filter(Customer.is_institution_self.isnot(True)).all()
    total_recordings = Recording.query.join(Customer).filter(Customer.institution_id == inst.id).count()
    students_balance_sum = sum(s.balance or 0 for s in students)
    from services import institution_students_ops as ISO
    from models import InstitutionUpload
    th = ISO.low_threshold(inst)
    usage = ISO.month_usage([s.id for s in students])
    pending = InstitutionUpload.query.filter_by(institution_id=inst.id, status='pending_payment').count()

    return render_template(
        'institution/dashboard.html',
        inst=inst,
        student_count=len(students),
        active_count=sum(1 for s in students if not s.is_blocked),
        low_count=sum(1 for s in students if not s.is_blocked and (s.balance or 0) < th),
        low_threshold=th,
        month_cost=round(sum(u['cost'] for u in usage.values()), 2),
        month_minutes=sum(u['minutes'] for u in usage.values()),
        pending_uploads=pending,
        students_balance_sum=students_balance_sum,
        total_recordings=total_recordings,
    )


# ---------------------------------------------------------------------
# הגדרות מוסד
# ---------------------------------------------------------------------
@institution_bp.route('/settings', methods=['GET', 'POST'])
@institution_login_required
def settings():
    inst = current_user
    if request.method == 'POST' and request.form.get('section') == 'automation':
        # זיכוי חודשי אוטומטי + התראת יתרה נמוכה
        from models import StudentGroup
        def _num(key):
            try:
                v = float(request.form.get(key) or 0)
                return round(v, 2) if v > 0 else None
            except ValueError:
                return None
        inst.monthly_credit_amount = _num('monthly_credit_amount') if request.form.get('monthly_on') else None
        gid = request.form.get('monthly_credit_group_id', type=int)
        inst.monthly_credit_group_id = gid if gid and StudentGroup.query.filter_by(id=gid, institution_id=inst.id).first() else None
        if inst.monthly_credit_amount and not inst.monthly_credit_last:
            # הפעלה ראשונה: החודש הנוכחי נחשב "בוצע" - הזיכוי הראשון יהיה בתחילת החודש הבא
            from services.institution_students_ops import IL
            inst.monthly_credit_last = datetime.now(IL).strftime('%Y-%m')
        inst.low_balance_threshold = _num('low_balance_threshold')
        inst.low_balance_email = bool(request.form.get('low_balance_email'))
        db.session.commit()
        flash('ההגדרות נשמרו')
        return redirect(url_for('institution.settings'))
    if request.method == 'POST':
        max_minutes = request.form.get('max_minutes_per_period')
        inst.max_minutes_per_period = float(max_minutes) if max_minutes else None
        inst.limit_period = request.form.get('limit_period') or None if inst.max_minutes_per_period else None
        inst.allowed_hours_start = request.form.get('allowed_hours_start') or None
        inst.allowed_hours_end = request.form.get('allowed_hours_end') or None
        inst.notify_email = request.form.get('notify_email') or None
        inst.notify_fax = request.form.get('notify_fax') or None
        from services.student_links import global_self_topup_enabled
        if global_self_topup_enabled():
            # תיבת הסימון קיימת בטופס רק כשהמתג המרכזי דלוק
            inst.allow_student_topup = (request.form.get('allow_student_topup') == '1')
        db.session.commit()
        flash('ההגדרות נשמרו')
        return redirect(url_for('institution.settings'))
    from services.student_links import global_self_topup_enabled
    from models import StudentGroup
    from services.institution_students_ops import DEFAULT_LOW, active_students
    groups = StudentGroup.query.filter_by(institution_id=inst.id).order_by(StudentGroup.name).all()
    monthly_count = len(active_students(inst, inst.monthly_credit_group_id)) if inst.monthly_credit_amount else 0
    return render_template('institution/settings.html', inst=inst, topup_global_on=global_self_topup_enabled(),
                           groups=groups, default_low=DEFAULT_LOW, monthly_count=monthly_count)


@institution_bp.route('/settings/add-authorized', methods=['POST'])
@institution_login_required
def add_authorized():
    inst = current_user
    email = (request.form.get('email') or '').strip()
    phone = (request.form.get('auth_phone') or '').strip()
    name = (request.form.get('auth_name') or '').strip()
    if email or phone:
        current = inst.authorized_logins or []
        current.append({'email': email, 'phone': phone, 'name': name})
        inst.authorized_logins = current
        db.session.commit()
        flash('מורשה כניסה נוסף')
    else:
        flash('יש למלא מייל או טלפון')
    return redirect(url_for('institution.settings'))


@institution_bp.route('/settings/remove-authorized', methods=['POST'])
@institution_login_required
def remove_authorized():
    inst = current_user
    email = request.form.get('email')
    phone = request.form.get('phone')
    inst.authorized_logins = [
        a for a in (inst.authorized_logins or [])
        if not ((email and a.get('email') == email) or (phone and a.get('phone') == phone))
    ]
    db.session.commit()
    flash('מורשה כניסה הוסר')
    return redirect(url_for('institution.settings'))


@institution_bp.route('/settings/change-password', methods=['POST'])
@institution_login_required
def change_password():
    """החלפת סיסמה מתוך ההגדרות - חובה להזין את הסיסמה הנוכחית. אחרי ההחלפה כל שאר
    המחוברים לחשבון מתנתקים (הסשן הנוכחי נשאר מחובר)."""
    from services import security as sec
    inst = current_user
    cur = request.form.get('current_password') or ''
    pw1 = request.form.get('password') or ''
    pw2 = request.form.get('password2') or ''
    ip_key = 'chpw:' + sec.client_ip()
    if sec.hits_blocked(ip_key, 5, 15 * 60) or sec.inst_locked(inst):
        flash('יותר מדי ניסיונות. נסו שוב בעוד כרבע שעה.')
        return redirect(url_for('institution.settings'))
    if inst.password_hash and not check_password_hash(inst.password_hash, cur):
        sec.hits_add(ip_key)
        sec.inst_register_fail(inst)
        db.session.commit()
        flash('הסיסמה הנוכחית שגויה')
        return redirect(url_for('institution.settings'))
    err = password_error(pw1, pw2)
    if not err and inst.password_hash and check_password_hash(inst.password_hash, pw1):
        err = 'הסיסמה החדשה זהה לנוכחית'
    if err:
        flash(err)
        return redirect(url_for('institution.settings'))
    inst.password_hash = generate_password_hash(pw1)
    inst.reset_requested_at = None
    sec.issue_login_code(inst)         # קוד זמני ישן (אם היה) כבר לא תקף
    sec.inst_clear_fails(inst)
    db.session.commit()
    sec.inst_mark_session(inst)
    sec.log_inst_login(inst, 'pw_change', True)
    flash('הסיסמה הוחלפה. שאר המכשירים שהיו מחוברים לחשבון נותקו.')
    return redirect(url_for('institution.settings'))


# ---------------------------------------------------------------------
# צור קשר - נשלח כ-ManagerMessage רגיל, מופיע במסך "הודעות למנהל" הקיים
# ---------------------------------------------------------------------
@institution_bp.route('/contact', methods=['GET', 'POST'])
@institution_login_required
def contact():
    if request.method == 'POST':
        from models import ManagerMessage
        msg = ManagerMessage(
            phone=current_user.phone or f'inst-{current_user.id}',
            name=f'מוסד: {current_user.name}',
            email=current_user.email,
            transcript=request.form.get('message', ''),
            status='new',
            source='institution_contact',
        )
        db.session.add(msg)
        db.session.commit()
        flash('ההודעה נשלחה להנהלת המערכת')
        return redirect(url_for('institution.contact'))
    return render_template('institution/contact.html')
