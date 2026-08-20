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
        return view(*args, **kwargs)
    return wrapped


@institution_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = (request.form.get('identifier') or '').strip()
        code = (request.form.get('code') or '').strip()

        inst = Institution.query.filter(
            (Institution.email == identifier) | (Institution.phone == identifier)
        ).first()

        if not inst:
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

        if not inst or inst.is_blocked:
            flash('פרטי התחברות שגויים')
            return render_template('institution/login.html')

        ok = False
        if inst.password_hash:
            # כבר הגדיר סיסמה - הקוד שהוקלד הוא הסיסמה
            ok = check_password_hash(inst.password_hash, code)
        else:
            # כניסה ראשונה - עדיין רק קוד זמני ממנהל-העל
            ok = bool(code) and code == inst.login_code

        if not ok:
            flash('פרטי התחברות שגויים')
            return render_template('institution/login.html')

        login_user(inst)
        if not inst.password_hash:
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

    inst = Institution.query.filter_by(email=email).first()
    if inst and not inst.is_blocked:
        import random, string
        inst.login_code = ''.join(random.choices(string.digits, k=6))
        inst.password_hash = None  # מאפסים את הסיסמה הקבועה - הכניסה הבאה עם הקוד תוביל שוב ל"הגדרת סיסמה"
        db.session.commit()
        try:
            _send_login_code_email(inst)
        except Exception:
            log.exception('forgot_password: failed to send email')

    return jsonify({'ok': True, 'message': generic_msg})


def _send_login_code_email(inst):
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#10b981">קוד כניסה זמני חדש</h2>
<p style="line-height:1.8">שלום {inst.name},</p>
<p style="line-height:1.8">התקבלה בקשה לאיפוס הגישה לחשבון המוסד שלכם במערכת התמלול. הקוד הזמני החדש שלכם הוא:</p>
<div style="background:#f4f6fb;border-radius:10px;padding:16px;text-align:center;font-size:26px;font-weight:700;letter-spacing:2px;margin:16px 0">{inst.login_code}</div>
<p style="line-height:1.8">היכנסו עם המייל/טלפון שלכם והקוד הזה, ותתבקשו להגדיר סיסמה קבועה חדשה.</p>
<p style="color:#6b7280;font-size:13px;line-height:1.6">אם לא ביקשתם זאת, ניתן להתעלם מהודעה זו.</p>
</div>'''

    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    message = Mail(
        from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
        to_emails=inst.email,
        subject='קוד כניסה זמני חדש - מערכת תמלול',
        html_content=html,
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

    inst = Institution.query.filter_by(google_id=google_id).first()
    if not inst and email:
        # התחברות ראשונה עם גוגל - מקשרים לפי מייל תואם אם אין עדיין קישור
        inst = Institution.query.filter_by(email=email).first()
        if inst:
            inst.google_id = google_id
            db.session.commit()

    if not inst or inst.is_blocked:
        flash('לא נמצא מוסד עם חשבון הגוגל הזה. יש להתחבר קודם עם מייל/סיסמה כדי לקשר את החשבון.')
        return redirect(url_for('institution.login'))

    login_user(inst)
    return redirect(url_for('institution.dashboard'))


@institution_bp.route('/set-password', methods=['GET', 'POST'])
@institution_login_required
def set_password():
    """מוצג בכניסה ראשונה בלבד (עוד אין password_hash) - מחליף את הקוד
    הזמני בסיסמה קבועה שהמוסד בוחר בעצמו."""
    if request.method == 'POST':
        pw1 = request.form.get('password') or ''
        pw2 = request.form.get('password2') or ''
        if len(pw1) < 6:
            flash('הסיסמה חייבת להיות באורך 6 תווים לפחות')
        elif pw1 != pw2:
            flash('הסיסמאות אינן תואמות')
        else:
            current_user.password_hash = generate_password_hash(pw1)
            db.session.commit()
            flash('הסיסמה הוגדרה בהצלחה')
            return redirect(url_for('institution.dashboard'))
    return render_template('institution/set_password.html')


@institution_bp.route('/logout')
@institution_login_required
def logout():
    logout_user()
    return redirect(url_for('institution.login'))


@institution_bp.route('/')
@institution_bp.route('/dashboard')
@institution_login_required
def dashboard():
    inst = current_user
    students = Customer.query.filter_by(institution_id=inst.id).all()
    total_recordings = Recording.query.join(Customer).filter(Customer.institution_id == inst.id).count()
    students_balance_sum = sum(s.balance or 0 for s in students)

    return render_template(
        'institution/dashboard.html',
        inst=inst,
        student_count=len(students),
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
    if request.method == 'POST':
        max_minutes = request.form.get('max_minutes_per_period')
        inst.max_minutes_per_period = float(max_minutes) if max_minutes else None
        inst.limit_period = request.form.get('limit_period') or None if inst.max_minutes_per_period else None
        inst.allowed_hours_start = request.form.get('allowed_hours_start') or None
        inst.allowed_hours_end = request.form.get('allowed_hours_end') or None
        inst.notify_email = request.form.get('notify_email') or None
        inst.notify_fax = request.form.get('notify_fax') or None
        db.session.commit()
        flash('ההגדרות נשמרו')
        return redirect(url_for('institution.settings'))
    return render_template('institution/settings.html', inst=inst)


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


@institution_bp.route('/settings/regenerate-code', methods=['POST'])
@institution_login_required
def regenerate_code():
    """יצירת קוד כניסה זמני חדש - שימושי כדי לתת אותו למורשה כניסה נוסף
    להתחברות ראשונה, או אם רוצים לאפס גישה. מאפס גם את הסיסמה הקבועה
    הקיימת (המשותפת לכל מורשי הכניסה) - אחרת הקוד החדש לא באמת עוזר,
    כי הכניסה תמיד מעדיפה סיסמה קבועה קיימת אם יש כזו."""
    import random, string
    inst = current_user
    inst.login_code = ''.join(random.choices(string.digits, k=6))
    inst.password_hash = None
    db.session.commit()
    flash(f'קוד כניסה חדש: {inst.login_code} (הסיסמה הקבועה הקודמת אופסה - הכניסה הבאה תבקש להגדיר סיסמה חדשה)')
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
