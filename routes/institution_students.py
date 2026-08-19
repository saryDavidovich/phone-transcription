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
    students = Customer.query.filter_by(institution_id=current_user.id).order_by(Customer.created_at.desc()).all()
    return render_template('institution/students.html', students=students)


@institution_students_bp.route('/institution/students/add', methods=['POST'])
@institution_login_required
def add_student():
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip() or None
    email = (request.form.get('email') or '').strip() or None
    fax = (request.form.get('fax') or '').strip() or None
    initial_balance = float(request.form.get('initial_balance') or 0)

    if phone and Customer.query.filter_by(phone=phone).first():
        flash('מספר הטלפון הזה כבר קיים במערכת')
        return redirect(url_for('institution_students.students_tab'))

    student = Customer(
        name=name, phone=phone, email=email, fax=fax,
        balance=initial_balance,
        institution_id=current_user.id,
        student_number=_generate_student_number(),
        student_display_name=name,
    )
    db.session.add(student)
    db.session.commit()

    if initial_balance:
        db.session.add(Transaction(customer_id=student.id, amount=initial_balance, type='credit', description='זיכוי פתיחה ע"י המוסד'))
        db.session.commit()

    flash(f'התלמיד נוסף בהצלחה - מספר תלמיד: {student.student_number}')
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/<int:student_id>/credit', methods=['POST'])
@institution_login_required
def credit_student(student_id):
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    try:
        amount = float(request.form.get('amount'))
    except (TypeError, ValueError):
        flash('סכום לא תקין')
        return redirect(url_for('institution_students.students_tab'))

    student.balance = (student.balance or 0) + amount
    db.session.add(Transaction(
        customer_id=student.id, amount=amount,
        type='credit' if amount >= 0 else 'debit',
        description='זיכוי ע"י המוסד' if amount >= 0 else 'חיוב ע"י המוסד',
    ))
    db.session.commit()
    flash('היתרה עודכנה')
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/<int:student_id>/block', methods=['POST'])
@institution_login_required
def toggle_block(student_id):
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    student.is_blocked = not student.is_blocked
    db.session.commit()
    flash('התלמיד נחסם' if student.is_blocked else 'החסימה בוטלה')
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/<int:student_id>/remove', methods=['POST'])
@institution_login_required
def remove_student(student_id):
    """'הורדת תלמיד' - מנתק אותו מהמוסד (לא מוחק פיזית) כדי לא לשבור
    היסטוריית הקלטות/תנועות שכבר קיימת עבורו."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    student.institution_id = None
    student.student_number = None
    db.session.commit()
    flash('התלמיד הוסר מהמוסד')
    return redirect(url_for('institution_students.students_tab'))


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

    idx_name = col('name', 'שם')
    idx_phone = col('phone', 'טלפון')
    idx_email = col('email', 'מייל', 'אימייל')
    idx_balance = col('balance', 'יתרה')

    added = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or all(v is None for v in row):
            continue
        name = str(row[idx_name]).strip() if idx_name is not None and row[idx_name] else ''
        phone = str(row[idx_phone]).strip() if idx_phone is not None and row[idx_phone] else None
        email = str(row[idx_email]).strip() if idx_email is not None and row[idx_email] else None
        balance = float(row[idx_balance]) if idx_balance is not None and row[idx_balance] else 0.0

        if phone and Customer.query.filter_by(phone=phone).first():
            continue  # דילוג על טלפון כפול, לא עוצר את כל הייבוא

        student = Customer(
            name=name, phone=phone, email=email, balance=balance,
            institution_id=current_user.id,
            student_number=_generate_student_number(),
            student_display_name=name,
        )
        db.session.add(student)
        added += 1

    db.session.commit()
    flash(f'יובאו {added} תלמידים בהצלחה')
    return redirect(url_for('institution_students.students_tab'))


@institution_students_bp.route('/institution/students/export-excel')
@institution_login_required
def export_excel():
    import openpyxl
    students = Customer.query.filter_by(institution_id=current_user.id).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['מספר תלמיד', 'שם', 'טלפון', 'מייל', 'יתרה', 'סה"כ הקלטות', 'חסום'])
    for s in students:
        recording_count = Recording.query.filter_by(customer_id=s.id).count()
        ws.append([s.student_number or '', s.name or '', s.phone or '', s.email or '',
                   s.balance or 0, recording_count, 'כן' if s.is_blocked else 'לא'])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='תלמידים.xlsx',
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@institution_students_bp.route('/institution/students/<int:student_id>')
@institution_login_required
def student_detail(student_id):
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    recordings = Recording.query.filter_by(customer_id=student.id).order_by(Recording.created_at.desc()).all()
    transactions = Transaction.query.filter_by(customer_id=student.id).order_by(Transaction.created_at.desc()).limit(30).all()
    return render_template('institution/student_detail.html', student=student, recordings=recordings, transactions=transactions)


@institution_students_bp.route('/institution/students/<int:student_id>/upload', methods=['POST'])
@institution_login_required
def upload_for_student(student_id):
    """העלאת קובץ נוסף לתמלול עבור תלמיד ספציפי - זהה בעקרון ללשונית
    'יצירת תמלול' הכללית, רק שהתוצאה משויכת לתלמיד (Recording אמיתי,
    כולל חיוב יתרה) ולא רק קובץ Word חד-פעמי למוסד."""
    student = Customer.query.filter_by(id=student_id, institution_id=current_user.id).first_or_404()
    file = request.files.get('audio_file')
    if not file or not file.filename:
        return jsonify({'error': 'יש לבחור קובץ'}), 400

    import uuid
    static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
    os.makedirs(static_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1] or '.wav'
    filename = f"student_{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(static_dir, filename))

    base_url = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    rec_url = f"{base_url}/static/fax_tmp/{filename}"

    recording = Recording(
        customer_id=student.id, call_id=f"inst_{uuid.uuid4().hex[:12]}",
        status='processing', source_filename=file.filename, rec_url=rec_url,
    )
    db.session.add(recording)
    db.session.commit()

    from app import app as flask_app
    t = threading.Thread(target=_process_student_upload, args=(flask_app, recording.id, rec_url), daemon=True)
    t.start()

    return jsonify({'recordingId': recording.id})


def _process_student_upload(flask_app, recording_id, rec_url):
    with flask_app.app_context():
        recording = Recording.query.get(recording_id)
        student = Customer.query.get(recording.customer_id)
        try:
            from services.transcribe import _gemini_from_url, _send_email, _send_fax
            transcript, duration, _ = _gemini_from_url(rec_url)
            if not transcript:
                recording.status = 'error'
                db.session.commit()
                return

            price_per_30min = flask_app.config.get('PRICE_PER_30MIN', 5.0)
            cost = round((duration or 0) / 1800 * price_per_30min, 2)

            recording.transcript = transcript
            recording.duration_seconds = duration
            recording.cost = cost
            recording.status = 'completed'
            student.balance = (student.balance or 0) - cost
            db.session.add(Transaction(customer_id=student.id, amount=-cost, type='charge',
                                        description='תמלול הקלטה', recording_id=recording.id))
            db.session.commit()

            institution = student.institution
            target_email = student.email or (institution.notify_email if institution else None)
            target_fax = student.fax or (institution.notify_fax if institution else None)
            try:
                if target_email:
                    _send_email(target_email, transcript, student, rec_url, duration)
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
    return jsonify({'status': recording.status})
