"""
לשונית 'ניהול תלמידים'. תלמיד הוא בפועל שורת Customer עם institution_id
ממולא (ראה models.py) - כך שכל צנרת ההקלטה/תמלול/חיוב הקיימת עובדת עליו
בלי כפילות קוד.
"""
import os
import io
import random
import string
import threading
import logging
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, send_file, flash, redirect, url_for
from flask_login import current_user
from app import db
from models import Customer, Recording, Transaction, InstitutionUpload
from routes.institution import institution_login_required

log = logging.getLogger(__name__)

institution_students_bp = Blueprint('institution_students', __name__)


def _generate_student_number():
    for _ in range(20):
        candidate = ''.join(random.choices(string.digits, k=6))
        if not Customer.query.filter_by(student_number=candidate).first():
            return candidate
    raise RuntimeError('לא ניתן היה ליצור מספר תלמיד פנוי')


@institution_students_bp.route('/institution/students')
@institution_login_required
def students_tab():
    # לקוח-הדמה של המוסד עצמו (is_institution_self, ראה routes/institution.py
    # ensure_institution_self_customer) לא תלמיד אמיתי - לא מוצג ברשימה הזו.
    students = (Customer.query.filter_by(institution_id=current_user.id)
                .filter(Customer.is_institution_self.isnot(True))
                .order_by(Customer.created_at.desc()).all())
    from sqlalchemy import func
    from models import StudentLinkRequest
    # טעינות עצמיות של התלמידים (סך הכל לכל תלמיד) - המוסד רואה כמה התלמיד טען בעצמו
    self_topups = dict(
        db.session.query(Transaction.customer_id, func.coalesce(func.sum(Transaction.amount), 0))
        .filter(Transaction.customer_id.in_([s.id for s in students]),
                Transaction.type == 'charge', Transaction.amount > 0,
                Transaction.description.like('טעינה עצמית%'))
        .group_by(Transaction.customer_id).all()
    ) if students else {}
    link_requests = (StudentLinkRequest.query.filter_by(institution_id=current_user.id)
                     .filter(StudentLinkRequest.status.in_(('pending', 'declined', 'expired')))
                     .order_by(StudentLinkRequest.created_at.desc()).limit(30).all())
    from services.student_links import global_self_topup_enabled, institution_self_topup_on
    from services import institution_students_ops as ISO
    from models import StudentGroup
    groups = StudentGroup.query.filter_by(institution_id=current_user.id).order_by(StudentGroup.name).all()
    group_counts = {}
    for st in students:
        group_counts[st.student_group_id] = group_counts.get(st.student_group_id, 0) + 1
    return render_template('institution/students.html', students=students, self_topups=self_topups,
                           usage=ISO.month_usage([st.id for st in students]), groups=groups,
                           group_names={g.id: g.name for g in groups}, group_counts=group_counts,
                           low_threshold=ISO.low_threshold(current_user),
                           month_label=datetime.now(ISO.IL).strftime('%m/%Y'),
                           link_requests=link_requests, now=datetime.utcnow(),
                           topup_global_on=global_self_topup_enabled(),
                           topup_inst_on=institution_self_topup_on(current_user))


@institution_students_bp.route('/institution/students/add', methods=['POST'])
@institution_login_required
def add_student():
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip() or None
    email = (request.form.get('email') or '').strip() or None
    fax = (request.form.get('fax') or '').strip() or None
    initial_balance = float(request.form.get('initial_balance') or 0)
    inst = current_user
    group_id = _own_group_id(request.form.get('group_id'))

    existing = __import__('services.customer_phones', fromlist=['x']).find_customer_by_phone(phone) if phone else None
    if existing:
        # לקוח שכבר קיים במערכת - לא נוצר כרטיס חדש, אלא נשלחת בקשת שיוך באישור הלקוח
        from services.student_links import request_link
        code, msg, _req = request_link(inst, existing, display_name=name)
        if code in ('sent', 'needs_admin') and initial_balance > 0:
            msg += ' (זיכוי פתיחה לא ניתן בשלב הבקשה - אפשר לזכות אותו אחרי שיאשר)'
        flash(msg)
        return redirect(url_for('institution_students.students_tab'))

    if initial_balance > 0 and (inst.balance or 0) < initial_balance:
        flash(f'אין למוסד מספיק יתרה לזיכוי הפתיחה שביקשת (יתרת המוסד: {inst.balance or 0:.2f} ₪). נא לטעון יתרה בלשונית "הגדרות חיוב", או להוסיף את התלמיד בלי זיכוי פתיחה.')
        return redirect(url_for('institution_students.students_tab'))

    student = Customer(
        name=name, phone=phone, email=email, fax=fax,
        balance=initial_balance,
        institution_id=current_user.id,
        student_number=_generate_student_number(),
        student_display_name=name,
        student_group_id=group_id,
    )
    db.session.add(student)
    db.session.commit()

    if initial_balance:
        if initial_balance > 0:
            inst.balance = (inst.balance or 0) - initial_balance
        db.session.add(Transaction(customer_id=student.id, amount=initial_balance, type='credit', description='זיכוי פתיחה ע"י המוסד'))
        db.session.commit()

    flash(f'התלמיד נוסף בהצלחה - מספר תלמיד: {student.student_number}')
    return redirect(url_for('institution_students.students_tab'))


def _back():
    """חזרה לרשימת התלמידים עם אותו חיפוש/סינון שהיה (נשלח בטופס כ-back)."""
    back = request.form.get('back') or ''
    if back.startswith('/institution/students') and '//' not in back:
        return back
    return url_for('institution_students.students_tab')


def _own_group_id(raw):
    from models import StudentGroup
    try:
        gid = int(raw or 0)
    except ValueError:
        return None
    if not gid:
        return None
    g = StudentGroup.query.filter_by(id=gid, institution_id=current_user.id).first()
    return g.id if g else None


@institution_students_bp.route('/institution/students/<int:student_id>/edit', methods=['POST'])
@institution_login_required
def edit_student(student_id):
    """עריכת פרטי תלמיד מתוך הרשימה: שם, טלפון, מייל, פקס, קבוצה."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip().replace('-', '').replace(' ', '') or None
    email = (request.form.get('email') or '').strip() or None
    fax = (request.form.get('fax') or '').strip() or None
    if not name:
        flash('יש להזין שם')
        return redirect(_back())
    if phone and phone != student.phone:
        from services.customer_phones import find_customer_by_phone
        other = find_customer_by_phone(phone)
        if other and other.id != student.id:
            flash(f'המספר {phone} כבר רשום במערכת אצל לקוח אחר - אי אפשר לשייך אותו לתלמיד הזה')
            return redirect(_back())
    if not student.name or student.name == student.student_display_name:
        student.name = name           # תלמיד שהמוסד יצר - השם שלו הוא השם שהמוסד נתן
    student.student_display_name = name
    student.phone = phone
    student.email = email
    student.fax = fax
    student.student_group_id = _own_group_id(request.form.get('group_id'))
    db.session.commit()
    flash('פרטי התלמיד עודכנו')
    return redirect(_back())


@institution_students_bp.route('/institution/students/bulk', methods=['POST'])
@institution_login_required
def bulk_action():
    """פעולה על כמה תלמידים יחד: זיכוי/הורדה, שיוך לקבוצה, חסימה, שחרור, הסרה."""
    from services import institution_students_ops as ISO
    ids = [int(x) for x in request.form.getlist('ids') if str(x).isdigit()]
    action = request.form.get('action')
    students = (Customer.query.filter(Customer.id.in_(ids), Customer.institution_id == current_user.id)
                .filter(Customer.is_institution_self.isnot(True)).all()) if ids else []
    if not students:
        flash('לא נבחרו תלמידים')
        return redirect(_back())
    n = len(students)
    if action == 'credit':
        try:
            amount = round(float(request.form.get('amount')), 2)
        except (TypeError, ValueError):
            flash('סכום לא תקין')
            return redirect(_back())
        if not amount:
            flash('סכום לא תקין')
            return redirect(_back())
        inst = current_user
        if amount > 0 and (inst.balance or 0) + 1e-9 < amount * n:
            flash(f'אין למוסד מספיק יתרה: {n} תלמידים × {amount:.2f} ₪ = {amount * n:.2f} ₪, '
                  f'ויתרת המוסד {inst.balance or 0:.2f} ₪. נא לטעון יתרה בלשונית "הגדרות חיוב".')
            return redirect(_back())
        errors, done = [], []
        for st in students:
            err = ISO.change_balance(inst, st, amount)
            if err:
                errors.append(err)
            else:
                done.append(st.id)
        db.session.commit()
        if amount > 0:
            ISO.after_credit(done)
        msg = f'{"זוכו" if amount > 0 else "הורדה יתרה ל-"} {len(done)} תלמידים ({abs(amount):.2f} ₪ לכל אחד)'
        if errors:
            msg += f' · {len(errors)} דולגו: ' + '; '.join(errors[:3]) + (' ...' if len(errors) > 3 else '')
        flash(msg)
    elif action == 'group':
        gid = _own_group_id(request.form.get('group_id'))
        for st in students:
            st.student_group_id = gid
        db.session.commit()
        flash(f'{n} תלמידים שויכו לקבוצה' if gid else f'{n} תלמידים הוצאו מהקבוצה')
    elif action in ('block', 'unblock'):
        for st in students:
            st.is_blocked = (action == 'block')
        db.session.commit()
        flash(f'{n} תלמידים {"נחסמו" if action == "block" else "שוחררו מחסימה"}')
    elif action == 'remove':
        for st in students:
            st.institution_id = None
            st.student_number = None
            st.self_funded = 0.0
            st.student_group_id = None
        db.session.commit()
        flash(f'{n} תלמידים הוסרו מהמוסד')
    else:
        flash('פעולה לא מוכרת')
    return redirect(_back())


@institution_students_bp.route('/institution/students/groups', methods=['POST'])
@institution_login_required
def groups_action():
    """ניהול קבוצות: הוספה, שינוי שם, מחיקה (התלמידים נשארים - רק בלי קבוצה)."""
    from models import StudentGroup
    action = request.form.get('action')
    name = (request.form.get('name') or '').strip()[:80]
    if action == 'add':
        if not name:
            flash('יש להזין שם לקבוצה')
        elif StudentGroup.query.filter_by(institution_id=current_user.id, name=name).first():
            flash('כבר יש קבוצה בשם הזה')
        else:
            db.session.add(StudentGroup(institution_id=current_user.id, name=name))
            db.session.commit()
            flash(f'הקבוצה "{name}" נוספה')
    else:
        g = StudentGroup.query.filter_by(id=request.form.get('group_id', type=int), institution_id=current_user.id).first()
        if not g:
            flash('הקבוצה לא נמצאה')
        elif action == 'rename' and name:
            g.name = name
            db.session.commit()
            flash('שם הקבוצה עודכן')
        elif action == 'delete':
            Customer.query.filter_by(institution_id=current_user.id, student_group_id=g.id).update(
                {Customer.student_group_id: None}, synchronize_session=False)
            if current_user.monthly_credit_group_id == g.id:
                current_user.monthly_credit_group_id = None
            db.session.delete(g)
            db.session.commit()
            flash('הקבוצה נמחקה (התלמידים נשארו, בלי קבוצה)')
    return redirect(_back())


@institution_students_bp.route('/institution/students/<int:student_id>/credit', methods=['POST'])
@institution_login_required
def credit_student(student_id):
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    inst = current_user
    try:
        amount = float(request.form.get('amount'))
    except (TypeError, ValueError):
        flash('סכום לא תקין')
        return redirect(url_for('institution_students.students_tab'))

    # זיכוי (חיובי) יורד מיתרת המוסד; הורדה (שלילי) חוזרת למוסד - רק מחלק המוסד ביתרת התלמיד
    from services import institution_students_ops as ISO
    err = ISO.change_balance(inst, student, amount)
    if err:
        flash(err)
        return redirect(_back())
    db.session.commit()
    if amount > 0:
        ISO.after_credit([student.id])
    flash('היתרה עודכנה')
    return redirect(_back())


@institution_students_bp.route('/institution/students/<int:student_id>/block', methods=['POST'])
@institution_login_required
def toggle_block(student_id):
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    student.is_blocked = not student.is_blocked
    db.session.commit()
    flash('התלמיד נחסם' if student.is_blocked else 'החסימה בוטלה')
    return redirect(_back())


@institution_students_bp.route('/institution/students/<int:student_id>/remove', methods=['POST'])
@institution_login_required
def remove_student(student_id):
    """'הורדת תלמיד' - מנתק אותו מהמוסד (לא מוחק פיזית) כדי לא לשבור
    היסטוריית הקלטות/תנועות שכבר קיימת עבורו."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    student.institution_id = None
    student.student_number = None
    student.self_funded = 0.0
    student.student_group_id = None
    db.session.commit()
    flash('התלמיד הוסר מהמוסד')
    return redirect(_back())


def _style_sheet(ws, widths, text_cols=(), money_cols=()):
    """גיליון בעברית: מימין לשמאל, כותרת מודגשת וצבועה, רוחב עמודות, טלפונים כטקסט (שלא יאבד ה-0)."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    ws.sheet_view.rightToLeft = True
    thin = Side(style='thin', color='D5DCE8')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='10B981')
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = border
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.border = border
            c.alignment = Alignment(horizontal='right')
            if c.column in text_cols:
                c.number_format = '@'
                if c.value not in (None, ''):
                    c.value = str(c.value)
            if c.column in money_cols:
                c.number_format = '#,##0.00'
    # גם שורות ריקות שימולאו בהמשך: טלפון כטקסט
    for col in text_cols:
        for r in range(ws.max_row + 1, ws.max_row + 300):
            ws.cell(row=r, column=col).number_format = '@'


@institution_students_bp.route('/institution/students/excel-template')
@institution_login_required
def download_excel_template():
    """קובץ אקסל לדוגמה עם העמודות הנכונות - נלחץ מתוך המודל 'ייבוא מאקסל'
    בעמוד ניהול תלמידים, כדי שהמוסד ידע בדיוק לפי איזה פורמט למלא."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'תלמידים'
    # בעברית ומימין לשמאל. הייבוא מזהה גם כותרות בעברית וגם באנגלית (קבצים ישנים)
    ws.append(['שם', 'טלפון', 'מייל', 'יתרה'])
    ws.append(['ישראל ישראלי', '0501234567', 'israel@example.com', 20])
    ws.append(['דוגמה בלי טלפון', '', '', 0])
    _style_sheet(ws, [24, 16, 28, 10], text_cols=(2,))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from routes.download_utils import render_data_uri_download_page
    return render_data_uri_download_page(
        buf.getvalue(), 'תבנית_ייבוא_תלמידים.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@institution_students_bp.route('/institution/students/upload-excel', methods=['POST'])
@institution_login_required
def upload_excel():
    file = request.files.get('excel_file')
    if not file:
        flash('יש לבחור קובץ')
        return redirect(url_for('institution_students.students_tab'))

    import openpyxl
    wb = openpyxl.load_workbook(file, data_only=True)
    ws = wb.active
    headers = [str(c.value or '').strip().lower() for c in ws[1]]

    def col(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return None

    idx_name = col('name', 'שם', 'שם מלא', 'שם התלמיד')
    idx_phone = col('phone', 'טלפון', 'פלאפון', 'נייד')
    idx_email = col('email', 'מייל', 'אימייל', 'דוא"ל', 'דואל')
    idx_balance = col('balance', 'יתרה')

    inst = current_user
    remaining_inst_balance = inst.balance or 0
    added = 0
    skipped_balance = 0
    link_requests_sent = 0
    link_conflicts = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or all(v is None for v in row):
            continue
        name = str(row[idx_name]).strip() if idx_name is not None and row[idx_name] else ''
        phone = str(row[idx_phone]).strip() if idx_phone is not None and row[idx_phone] else None
        if phone:
            # אקסל שומר טלפון כמספר ומוחק את ה-0 בהתחלה (501234567 / 501234567.0) - מחזירים אותו
            phone = phone.replace('-', '').replace(' ', '')
            if phone.endswith('.0'):
                phone = phone[:-2]
            if phone.isdigit() and len(phone) in (8, 9) and not phone.startswith('0'):
                phone = '0' + phone
        email = str(row[idx_email]).strip() if idx_email is not None and row[idx_email] else None
        requested_balance = float(row[idx_balance]) if idx_balance is not None and row[idx_balance] else 0.0

        existing = __import__('services.customer_phones', fromlist=['x']).find_customer_by_phone(phone) if phone else None
        if existing:
            # לקוח קיים - בקשת שיוך באישור הלקוח במקום דילוג שקט (לא עוצר את הייבוא)
            from services.student_links import request_link
            code, _msg, _req = request_link(inst, existing, display_name=name)
            if code in ('sent', 'needs_admin'):
                link_requests_sent += 1
            else:
                link_conflicts += 1
            continue

        # לא נותנים לתלמיד יתרת פתיחה שהמוסד בפועל לא מכסה - התלמיד עדיין
        # מיובא, רק בלי הזיכוי (או עם חלק ממנו אם היתרה הצטמצמה באמצע הקובץ).
        balance = 0.0
        if requested_balance > 0:
            if remaining_inst_balance >= requested_balance:
                balance = requested_balance
                remaining_inst_balance -= requested_balance
            else:
                skipped_balance += 1

        student = Customer(
            name=name, phone=phone, email=email, balance=balance,
            institution_id=current_user.id,
            student_number=_generate_student_number(),
            student_display_name=name,
        )
        db.session.add(student)
        added += 1

    inst.balance = remaining_inst_balance
    db.session.commit()
    msg = f'יובאו {added} תלמידים בהצלחה'
    if skipped_balance:
        msg += f' - שימו לב: ל-{skipped_balance} מהם לא ניתנה יתרת הפתיחה שביקשתם כי יתרת המוסד לא הספיקה'
    if link_requests_sent:
        msg += f' · {link_requests_sent} מספרים כבר קיימים במערכת כלקוחות - נשלחה להם בקשת שיוך לאישור'
    if link_conflicts:
        msg += f' · {link_conflicts} מספרים דולגו (כבר אצלכם / משויכים למוסד אחר / בקשה כבר ממתינה)'
    flash(msg)
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/link-request/<int:req_id>/<action>', methods=['POST'])
@institution_login_required
def link_request_action(req_id, action):
    """ביטול בקשת שיוך שממתינה, או שליחה חוזרת של המייל ללקוח."""
    from models import StudentLinkRequest
    from services.student_links import send_link_email
    req = StudentLinkRequest.query.filter_by(id=req_id, institution_id=current_user.id).first_or_404()
    if action == 'cancel' and req.status == 'pending':
        req.status = 'cancelled'
        db.session.commit()
        flash('הבקשה בוטלה')
    elif action == 'resend' and req.status == 'pending' and not req.needs_admin:
        flash('המייל נשלח שוב ללקוח' if send_link_email(req) else 'שליחת המייל נכשלה')
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/export-excel')
@institution_login_required
def export_excel():
    import openpyxl
    from sqlalchemy import func
    students = (Customer.query.filter_by(institution_id=current_user.id)
                .filter(Customer.is_institution_self.isnot(True)).all())

    # שאילתת ספירה מרוכזת אחת (GROUP BY) במקום COUNT() נפרד לכל תלמיד
    # בלולאה - עם הרבה תלמידים זה היה N+1 שאילתות מיותר לגמרי.
    counts_by_customer = dict(
        db.session.query(Recording.customer_id, func.count(Recording.id))
        .filter(Recording.customer_id.in_([s.id for s in students]))
        .group_by(Recording.customer_id).all()
    ) if students else {}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'תלמידים'
    ws.append(['מספר תלמיד', 'שם', 'טלפון', 'מייל', 'יתרה', 'סה"כ הקלטות', 'חסום'])
    for s in students:
        recording_count = counts_by_customer.get(s.id, 0)
        ws.append([s.student_number or '', s.student_display_name or s.name or '', s.phone or '', s.email or '',
                   round(s.balance or 0, 2), recording_count, 'כן' if s.is_blocked else 'לא'])
    _style_sheet(ws, [12, 24, 16, 28, 11, 12, 8], text_cols=(1, 3), money_cols=(5,))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from routes.download_utils import render_data_uri_download_page
    return render_data_uri_download_page(
        buf.getvalue(), 'תלמידים.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@institution_students_bp.route('/institution/students/<int:student_id>')
@institution_login_required
def student_detail(student_id):
    from models import ManuscriptPage, ProofingRound
    from sqlalchemy.orm import defer, joinedload
    from routes.dictate import _defer_manuscript_page_blobs
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    # defer(Recording.file_data)/_defer_manuscript_page_blobs: הדף הזה מציג
    # רק שם קובץ/סטטוס/תאריך לכל שורה - בלי defer מפורש, SQLAlchemy עושה
    # SELECT * כברירת מחדל ומביא גם עמודות LargeBinary (קובץ אודיו/כתב יד
    # שלם) לכל שורה ברשימה, מה שמאט מאוד דף עם היסטוריה - בדיוק אותו באג
    # שכבר תוקן בתור הצוות (routes/dictate.py, queue()/fax_assign()).
    recordings = (Recording.query.filter_by(customer_id=student.id)
                  .options(defer(Recording.file_data))
                  .order_by(Recording.created_at.desc()).all())
    transactions = Transaction.query.filter_by(customer_id=student.id).order_by(Transaction.created_at.desc()).limit(30).all()
    # כתבי יד להקראה (ראה routes/dictate.py) שהמוסד העלה/שהתקבלו עבור התלמיד -
    # אותה טבלה בדיוק שכל לקוח רגיל משתמש בה (ManuscriptPage.customer_id),
    # כולל סבבי ההגהה שלהם (page.proofing_rounds - ראה models.ProofingRound) -
    # נטענות מראש (joinedload) כדי למנוע שאילתת N+1 נפרדת לכל כתב-יד בלולאת
    # התבנית, וגם הן מדולגות מעמודות ה-blob הכבדות שלהן.
    manuscript_pages = (
        _defer_manuscript_page_blobs(ManuscriptPage.query, ManuscriptPage)
        .filter_by(customer_id=student.id)
        .options(
            joinedload(ManuscriptPage.proofing_rounds)
            .defer(ProofingRound.customer_file_data)
            .defer(ProofingRound.final_file_data)
        )
        .order_by(ManuscriptPage.created_at.desc()).all()
    )
    # מחירון להצגה ללקוח - נגזר ישירות מהגדרות המנהל (אותם ערכים בדיוק שלפיהם
    # מחויבים בפועל ב-routes/dictate.py send()/proof_complete), כך שלעולם לא
    # יופיע מחיר שונה ממה שבאמת יחויב.
    from routes.dictate import _manuscript_pricing, _manuscript_proofing_price
    char_unit_size, char_unit_price = _manuscript_pricing()
    from models import StudentGroup
    from services import institution_students_ops as ISO
    groups = StudentGroup.query.filter_by(institution_id=current_user.id).order_by(StudentGroup.name).all()
    usage = ISO.month_usage([student.id]).get(student.id, {'cost': 0, 'minutes': 0})
    total_spent = round(-sum((t.amount or 0) for t in Transaction.query.filter(
        Transaction.customer_id == student.id, Transaction.amount < 0,
        ~Transaction.description.like('הורדת יתרה ע"י המוסד%')).all()), 2)
    return render_template('institution/student_detail.html', student=student, recordings=recordings,
                            groups=groups, group_name=next((g.name for g in groups if g.id == student.student_group_id), None),
                            usage=usage, total_spent=total_spent, low_threshold=ISO.low_threshold(current_user),
                            transactions=transactions, manuscript_pages=manuscript_pages,
                            char_unit_size=char_unit_size, char_unit_price=char_unit_price,
                            proofing_price_per_minute=_manuscript_proofing_price())


def recording_title(rec, student):
    """כותרת לתמלול של תלמיד (בדף התמלול, ב-Word ובהורדה המרוכזת)."""
    if rec.source_filename:
        return rec.source_filename
    return f"{student.name or 'תלמיד'} - הקלטה מהטלפון"


def recording_meta(rec, student=None):
    parts = []
    if student is not None and student.name:
        parts.append(student.name)
    if rec.created_at:
        from services.institution_students_ops import IL
        from datetime import timezone
        parts.append(rec.created_at.replace(tzinfo=timezone.utc).astimezone(IL).strftime('%d/%m/%Y %H:%M'))
    if rec.duration_seconds:
        from services.portal_docx import minutes_label
        parts.append(minutes_label(rec.duration_seconds))
    parts.append('תמלול מקצועי' if rec.transcription_tier == 'premium' else 'תמלול רגיל')
    return ' · '.join(parts)


@institution_students_bp.route('/institution/students/<int:student_id>/recording/<int:recording_id>/download')
@institution_login_required
def download_student_recording(student_id, recording_id):
    """הורדת התמלול של הקלטה בודדת של תלמיד כקובץ Word - מחליף את כפתור
    "צפייה" הקודם (שהיה מציג alert() עם הטקסט, ולא באמת עבד)."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    recording = Recording.query.filter_by(id=recording_id, customer_id=student.id).first_or_404()
    if not recording.transcript:
        return 'אין עדיין תמלול להקלטה הזו', 404

    from services import portal_docx as PD
    data = PD.single(current_user, recording_title(recording, student), recording.transcript, recording_meta(recording))
    from routes.download_utils import render_data_uri_download_page
    hebrew_name = f"תמלול - {student.name or 'תלמיד'} - {recording.created_at.strftime('%d-%m-%Y') if recording.created_at else recording.id}.docx"
    return render_data_uri_download_page(
        data, hebrew_name,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


# ==========================================================================
# כתבי יד להקראה + הגהה - בתוך פרופיל תלמיד אצל מוסד. אותה תשתית בדיוק
# שלקוח רגיל משתמש בה (models.ManuscriptPage/ProofingRound, routes/dictate.py) -
# רק שהמוסד מעלה/מוריד ישירות דרך הפורטל שלו במקום דרך מייל/פקס. ברגע
# שכתב-היד/סבב ההגהה נוצר הוא נכנס בדיוק לאותו תור שצוות ההקראה כבר עובד
# מולו ("בתור"/"ממתין להגהה" ב-/admin/dictate) - שום שינוי בצד הצוות. גם
# התמחור/החיוב זהה לחלוטין ללקוח רגיל (student.balance הוא Customer.balance
# רגיל - ראה models.Customer, ההערה "לקוח רגיל...תלמיד...אותה טבלה בדיוק").
# ==========================================================================

MANUSCRIPT_UPLOAD_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif', 'pdf'}


@institution_students_bp.route('/institution/students/<int:student_id>/manuscript/upload', methods=['POST'])
@institution_login_required
def upload_student_manuscript(student_id):
    """המוסד מעלה תמונה/PDF של דף כתב יד עבור התלמיד - נכנס לתור ההקראה
    הרגיל בדיוק כמו כתב יד שמתקבל במייל (ראה email_inbound._capture_manuscript_page)."""
    from models import ManuscriptPage
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    file = request.files.get('manuscript_file')
    if not file or not file.filename:
        return jsonify({'error': 'יש לבחור קובץ'}), 400
    ext = os.path.splitext(file.filename)[1].lstrip('.').lower()
    if ext not in MANUSCRIPT_UPLOAD_EXTS:
        return jsonify({'error': 'יש להעלות תמונה (jpg/png/וכו׳) או PDF בלבד'}), 400

    page = ManuscriptPage(
        customer_id=student.id,
        original_filename=file.filename,
        file_data=file.read(),
        status='pending',
    )
    db.session.add(page)
    db.session.commit()
    log.info(f"institution upload_student_manuscript: כתב יד חדש (page={page.id}) הועלה ע\"י מוסד {current_user.id} עבור תלמיד {student.id}")
    return jsonify({'status': 'ok', 'pageId': page.id})


@institution_students_bp.route('/institution/students/<int:student_id>/manuscript/<int:page_id>/download')
@institution_login_required
def download_student_manuscript(student_id, page_id):
    """הורדת קובץ ה-Word המוכן של כתב יד שהושלם - זהה לחלוטין למה שנשלח
    ללקוח רגיל במייל (ראה routes/dictate.py._build_manuscript_docx)."""
    from models import ManuscriptPage
    from routes.dictate import _build_manuscript_docx
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    page = ManuscriptPage.query.filter_by(id=page_id, customer_id=student.id).first_or_404()
    if not page.content:
        return 'כתב היד הזה עדיין לא הוקרא - אין תוכן מוכן להורדה', 404

    docx_bytes = _build_manuscript_docx(student.name or '', page.original_filename, page.content)
    from routes.download_utils import render_data_uri_download_page
    safe_name = os.path.splitext(page.original_filename or 'כתב_יד')[0][:40]
    return render_data_uri_download_page(
        docx_bytes, f'כתב_יד_{safe_name}.docx',
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


@institution_students_bp.route('/institution/students/<int:student_id>/manuscript/<int:page_id>/proof-upload', methods=['POST'])
@institution_login_required
def upload_student_proof(student_id, page_id):
    """המוסד מעלה קובץ תיקוני הגהה (Word מוקלד, או תמונה/PDF של דף מודפס
    שתוקן בעט) עבור כתב יד שכבר הושלם - יוצר סבב הגהה חדש (ProofingRound),
    בדיוק כמו תגובת הגהה שמגיעה במייל/פקס מלקוח רגיל. נכנס לתור "ממתין
    להגהה" הרגיל - הצוות ממשיך משם כרגיל (כולל חיוב לפי דקת עבודה)."""
    from models import ManuscriptPage, ProofingRound
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    page = ManuscriptPage.query.filter_by(id=page_id, customer_id=student.id).first_or_404()
    if page.status != 'done':
        return jsonify({'error': 'אפשר להעלות תיקוני הגהה רק על כתב יד שכבר הושלם'}), 400
    file = request.files.get('correction_file')
    if not file or not file.filename:
        return jsonify({'error': 'יש לבחור קובץ'}), 400

    round_ = ProofingRound(
        manuscript_page_id=page.id,
        status='pending',
        customer_file_data=file.read(),
        customer_file_filename=file.filename,
        requested_at=datetime.utcnow(),
    )
    db.session.add(round_)
    db.session.commit()
    log.info(f"institution upload_student_proof: סבב הגהה חדש (round={round_.id}) הועלה ע\"י מוסד {current_user.id} על כתב יד {page.id}")
    return jsonify({'status': 'ok', 'roundId': round_.id})


@institution_students_bp.route('/institution/students/<int:student_id>/manuscript/<int:page_id>/proof/<int:round_id>/download')
@institution_login_required
def download_student_proof(student_id, page_id, round_id):
    """הורדת קובץ ה-Word הסופי של סבב הגהה שהושלם וחויב."""
    from models import ManuscriptPage, ProofingRound
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    page = ManuscriptPage.query.filter_by(id=page_id, customer_id=student.id).first_or_404()
    round_ = ProofingRound.query.filter_by(id=round_id, manuscript_page_id=page.id).first_or_404()
    if not round_.final_file_data:
        return 'ההגהה הזו עדיין לא הושלמה - אין קובץ סופי מוכן להורדה', 404

    from routes.download_utils import render_data_uri_download_page
    return render_data_uri_download_page(
        round_.final_file_data, round_.final_filename or f'מתוקן_{round_id}.docx',
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


@institution_students_bp.route('/institution/students/<int:student_id>/upload', methods=['POST'])
@institution_login_required
def upload_for_student(student_id):
    """העלאת קובץ נוסף לתמלול עבור תלמיד ספציפי - זהה בעקרון ללשונית
    'יצירת תמלול' הכללית, רק שהתוצאה משויכת לתלמיד (Recording אמיתי,
    כולל חיוב יתרה) ולא רק קובץ Word חד-פעמי למוסד."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    from services import portal_media as PM
    file = request.files.get('audio_file')
    link = (request.form.get('link') or '').strip()
    if (not file or not file.filename) and not link:
        return jsonify({'error': 'יש לבחור קובץ, להקליט או להדביק קישור'}), 400
    if link and not PM.link_kind(link):
        return jsonify({'error': PM.LINK_HELP}), 400

    from routes.institution import student_usage_limit_error
    limit_error = student_usage_limit_error(student)
    if limit_error:
        return jsonify({'error': limit_error}), 403

    import uuid
    static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
    os.makedirs(static_dir, exist_ok=True)
    local_path, name = None, None
    if file and file.filename:
        from services.security import safe_audio_ext
        local_path = os.path.join(static_dir, f"student_{uuid.uuid4().hex}{safe_audio_ext(file.filename)}")
        file.save(local_path)
        name = file.filename
    else:
        name = 'קישור Drive' if PM.link_kind(link) == 'drive' else "קישור ג'מבו מייל"
    name = (request.form.get('title') or '').strip()[:200] or name

    tier = 'premium' if request.form.get('tier') == 'premium' else 'gemini'
    language = request.form.get('language') if request.form.get('language') in ('he', 'yi', 'en') else 'he'
    output_language = 'original' if request.form.get('output_language') == 'original' else 'he'
    recording = Recording(
        customer_id=student.id, call_id=f"inst_{uuid.uuid4().hex[:12]}",
        status='processing', source_filename=name, rec_url=None,
        transcription_tier=tier, language=language, output_language=output_language,
    )
    db.session.add(recording)
    db.session.commit()

    from app import app as flask_app
    threading.Thread(target=_process_student_upload, args=(flask_app, recording.id, local_path, link or None),
                     daemon=True).start()
    return jsonify({'recordingId': recording.id, 'name': name})


def _process_student_upload(flask_app, recording_id, local_path, link=None, send=True):
    from services.job_tracker import tracked
    from services import portal_media as PM
    with flask_app.app_context(), tracked('institution_student', label=f'הקלטה {recording_id}'):
        recording = Recording.query.get(recording_id)
        student = Customer.query.get(recording.customer_id)
        try:
            static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
            if link:                      # קישור Drive / ג'מבו - מורידים קודם
                try:
                    local_path, real_name = PM.fetch_link(link, static_dir)
                except ValueError as ve:
                    recording.status = 'error'
                    db.session.commit()
                    log.warning(f'Student upload {recording_id}: {ve}')
                    return
                if recording.source_filename in ('קישור Drive', "קישור ג'מבו מייל") and real_name:
                    recording.source_filename = real_name[:200]
            if PM.needs_conversion(local_path):       # הקלטה מהדפדפן (webm) -> mp3
                local_path = PM.convert_in_place(local_path)
            base_url = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
            rec_url = f"{base_url}/static/fax_tmp/{os.path.basename(local_path)}"
            recording.rec_url = rec_url
            db.session.commit()
            from services.transcribe import _gemini_from_url, _send_email, _send_fax, alefbot_transcribe_wait
            tier = recording.transcription_tier or 'gemini'
            is_video = False
            if tier == 'premium':     # תמלול מקצועי - אלף בוט, כמו ללקוח רגיל
                transcript, duration = alefbot_transcribe_wait(rec_url, recording.call_id,
                                                               translate_to_hebrew=(recording.output_language != 'original'))
            else:
                transcript, duration, is_video = _gemini_from_url(rec_url, recording.language or 'he',
                                                                  recording.output_language or 'he')
            if not transcript:
                recording.status = 'error'
                db.session.commit()
                return

            # השמע לנגן באזור המוסד - בימות (נטפרי חוסמת שמע מהאתר שלנו); העותק אצלנו נמחק
            play = PM.push_to_yemot(local_path, f'r{recording_id}_{uuid.uuid4().hex[:8]}')
            if play:
                recording.play_url = play
                # כמו הקלטה מהטלפון: rec_url בימות (המנהל מאזין משם; שליחה חוזרת לתמלול מורידה משם)
                recording.rec_url = f"{PM.YEMOT_API}/DownloadFile?token={PM.upload_token()}&path={play}"
                PM.remove_quietly(local_path)
                db.session.commit()

            # מחיר כמו ללקוח רגיל: סוג השירות (רגיל / מקצועי / וידאו) ויחידות של 20 או 5 דקות -
            # לפי המחירון האישי של התלמיד, ואם אין לו - לפי המוסד (services/pricing.unit_minutes)
            from services import pricing as P
            kind = P.kind_for_tier(tier, is_video)
            cost = P.cost_for(student, duration or 0, kind)

            institution = student.institution
            target_email = student.email or (institution.notify_email if institution else None)
            target_fax = student.fax or (institution.notify_fax if institution else None)

            if (student.balance or 0) + 1e-9 < cost:
                # אין לתלמיד מספיק יתרה - כמו לקוח רגיל: ממתין לתשלום (לא יורד למינוס), נשמר עם
                # התמלול, ונשלח אוטומטית אחרי זיכוי/טעינה (process_pending_recordings)
                from services.transcribe import _save_pending_payment
                _save_pending_payment(recording, student, int(duration or 0), P.unit_price(student, kind),
                                      'email' if target_email else ('fax' if target_fax else 'email'),
                                      target_email or target_fax, tier, recording.language or 'he',
                                      recording.output_language or 'he', transcript=transcript)
                return

            recording.transcript = transcript
            recording.duration_seconds = duration
            recording.cost = cost
            recording.status = 'completed'
            student.balance = round((student.balance or 0) - cost, 2)
            db.session.add(Transaction(customer_id=student.id, amount=-cost, type='debit',
                                        description='תמלול הקלטה', recording_id=recording.id))
            db.session.commit()

            try:
                if not send:
                    pass          # לקוח פרטי שבחר לא לקבל במייל (התמלול זמין באזור האישי)
                elif target_email:
                    _send_email(target_email, transcript, student, rec_url, duration, is_premium=(tier == 'premium'))
                elif target_fax:
                    _send_fax(target_fax, transcript, student, duration)
            except Exception:
                log.exception('שליחת תמלול תלמיד נכשלה (התמלול עצמו הצליח)')
        except Exception as e:
            log.exception(f'Student upload {recording_id} failed')
            recording.status = 'error'
            db.session.commit()


@institution_students_bp.route('/institution/students/recording-status/<int:recording_id>')
@institution_login_required
def recording_status(recording_id):
    recording = Recording.query.get_or_404(recording_id)
    student = Customer.query.get(recording.customer_id)
    if not student or student.institution_id != current_user.id:
        return jsonify({'error': 'אין הרשאה'}), 403
    done = recording.status == 'completed'
    return jsonify({'status': recording.status, 'name': recording.source_filename,
                    'viewUrl': f'/institution/t/r/{recording.id}' if done else None})
