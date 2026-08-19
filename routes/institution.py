"""
ממשק ניהול מוסד - נפרד לגמרי מממשק מנהל-העל (routes/admin.py).
מוסד מתחבר לפי מייל או טלפון + קוד שיצר מנהל-העל. בכניסה ראשונה הוא
מתבקש להגדיר סיסמה קבועה (מחליפה את הקוד), ויכול לחבר גם כניסה עם גוגל.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from datetime import datetime
from app import db
from models import Institution, Customer, Recording, Transaction
import logging

log = logging.getLogger(__name__)

institution_bp = Blueprint('institution', __name__, template_folder='../templates/institution')


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
            # של המוסד עצמו, רק עם זהות התחברות שונה.
            candidates = Institution.query.filter(Institution.authorized_logins.isnot(None)).all()
            for c in candidates:
                emails = [a.get('email') for a in (c.authorized_logins or []) if isinstance(a, dict)]
                if identifier in emails:
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
        inst.max_usage_per_student = float(request.form.get('max_usage_per_student')) if request.form.get('max_usage_per_student') else None
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
    name = (request.form.get('auth_name') or '').strip()
    if email:
        current = inst.authorized_logins or []
        current.append({'email': email, 'name': name})
        inst.authorized_logins = current
        db.session.commit()
        flash('מורשה כניסה נוסף')
    return redirect(url_for('institution.settings'))


@institution_bp.route('/settings/remove-authorized', methods=['POST'])
@institution_login_required
def remove_authorized():
    inst = current_user
    email = request.form.get('email')
    inst.authorized_logins = [a for a in (inst.authorized_logins or []) if a.get('email') != email]
    db.session.commit()
    flash('מורשה כניסה הוסר')
    return redirect(url_for('institution.settings'))


@institution_bp.route('/settings/regenerate-code', methods=['POST'])
@institution_login_required
def regenerate_code():
    """יצירת קוד כניסה זמני חדש - שימושי כדי לתת אותו למורשה כניסה נוסף
    להתחברות ראשונה, או אם רוצים לאפס גישה."""
    import random, string
    inst = current_user
    inst.login_code = ''.join(random.choices(string.digits, k=6))
    db.session.commit()
    flash(f'קוד כניסה חדש: {inst.login_code}')
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
        )
        db.session.add(msg)
        db.session.commit()
        flash('ההודעה נשלחה להנהלת המערכת')
        return redirect(url_for('institution.contact'))
    return render_template('institution/contact.html')
