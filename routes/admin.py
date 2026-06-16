from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, make_response
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from app import db, login_manager
from models import Customer, Recording, Transaction, Settings, AdminUser, ManagerMessage
from datetime import datetime, timedelta
from sqlalchemy import func
import io

admin_bp = Blueprint('admin', __name__)

@login_manager.user_loader
def load_user(user_id):
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

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = AdminUser.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('admin.dashboard'))
        flash('שם משתמש או סיסמה שגויים')
    return render_template('admin/login.html')

@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('admin.login'))

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def dashboard():
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)

    stats = {
        'total_customers': Customer.query.count(),
        'active_customers': Customer.query.filter_by(is_blocked=False).count(),
        'blocked_customers': Customer.query.filter_by(is_blocked=True).count(),
        'total_recordings': Recording.query.count(),
        'today_recordings': Recording.query.filter(
            func.date(Recording.created_at) == today
        ).count(),
        'month_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge',
            Transaction.created_at >= month_start
        ).scalar() or 0,
        'total_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge'
        ).scalar() or 0,
        'total_balance': db.session.query(func.sum(Customer.balance)).scalar() or 0,
    }

    recent_recordings = Recording.query.order_by(Recording.created_at.desc()).limit(10).all()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()

    return render_template('admin/dashboard.html',
        stats=stats,
        recent_recordings=recent_recordings,
        recent_transactions=recent_transactions
    )

@admin_bp.route('/customers')
@login_required
def customers():
    search = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    query = Customer.query
    if search:
        query = query.filter(
            Customer.phone.contains(search) |
            Customer.name.contains(search) |
            Customer.email.contains(search)
        )
    customers = query.order_by(Customer.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/customers.html', customers=customers, search=search)

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

        existing = Customer.query.filter_by(phone=phone).first()
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

@admin_bp.route('/customers/<int:id>')
@login_required
def customer_detail(id):
    customer = Customer.query.get_or_404(id)
    recordings = Recording.query.filter_by(customer_id=id).order_by(Recording.created_at.desc()).all()
    transactions = Transaction.query.filter_by(customer_id=id).order_by(Transaction.created_at.desc()).all()
    return render_template('admin/customer_detail.html',
        customer=customer, recordings=recordings, transactions=transactions,
        timedelta=timedelta)

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
    customer = Customer.query.get_or_404(id)
    Transaction.query.filter_by(customer_id=id).delete()
    Recording.query.filter_by(customer_id=id).delete()
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
        customer.balance += amount
        txn = Transaction(
            customer_id=id,
            amount=amount,
            type='credit',
            description=reason
        )
        db.session.add(txn)
        db.session.commit()
        flash(f'לקוח זוכה ב-{amount:.2f} ₪')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/charge', methods=['POST'])
@login_required
def charge_customer(id):
    customer = Customer.query.get_or_404(id)
    amount = float(request.form.get('amount', 0))
    reason = request.form.get('reason', 'חיוב ידני')
    if amount > 0:
        customer.balance -= amount
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
    customer.name = request.form.get('name', customer.name)
    customer.email = request.form.get('email', customer.email)
    customer.fax = request.form.get('fax', customer.fax)
    customer.delivery_method = request.form.get('delivery_method', customer.delivery_method)
    db.session.commit()
    flash('פרטי לקוח עודכנו')
    return redirect(url_for('admin.customer_detail', id=id))

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
            rec_url = f'https://www.call2all.co.il/ym/api/DownloadFile?token={os.environ.get("YEMOT_TOKEN","")}&path=ivr2:/recordings/{rec.call_id}.wav'
            if send_method == 'email':
                _send_email(send_to, rec.transcript, customer, rec_url, rec.duration_seconds, source_filename=rec.source_filename)
            else:
                _send_fax(send_to, rec.transcript, customer, rec.duration_seconds)
            sent += 1
        except Exception as e:
            flash(f'שגיאה בשליחת הקלטה {rec_id}: {e}')

    flash(f'נשלחו {sent} הקלטות בהצלחה')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/recordings')
@login_required
def recordings():
    page = request.args.get('page', 1, type=int)
    recordings = Recording.query.order_by(Recording.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/recordings.html', recordings=recordings, timedelta=timedelta)

@admin_bp.route('/recordings/<int:id>')
@login_required
def recording_detail(id):
    recording = Recording.query.get_or_404(id)
    return render_template('admin/recording_detail.html', recording=recording)

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
        r = req.get(rec_url, timeout=60)
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
        r = req.get(rec_url, timeout=60)
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
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    recording = Recording.query.get_or_404(id)
    customer = Customer.query.get(recording.customer_id)

    doc = Document()

    title = doc.add_heading('תמלול שיחה', 0)
    title.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.add_paragraph(f'לקוח: {customer.name or customer.phone if customer else ""}').alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph(f'תאריך: {recording.created_at.strftime("%d/%m/%Y %H:%M") if recording.created_at else ""}').alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph(f'משך: {recording.duration_seconds // 60} דקות').alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph('─' * 50)

    if recording.summary:
        doc.add_heading('סיכום', level=1).alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p = doc.add_paragraph(recording.summary)
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.add_heading('תמלול מלא', level=1).alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(recording.transcript or 'אין תמלול')
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    output = io.BytesIO()
    doc.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=f'transcript_{id}.docx'
    )

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

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        set_setting('price_per_20min_basic', request.form.get('price_per_20min_basic', '0.90'))
        set_setting('price_per_20min_premium', request.form.get('price_per_20min_premium', '1.90'))
        set_setting('price_per_20min_video', request.form.get('price_per_20min_video', '1.50'))
        set_setting('price_per_1000_chars_ocr', request.form.get('price_per_1000_chars_ocr', '0.10'))
        set_setting('min_balance', request.form.get('min_balance', '5'))
        set_setting('max_recording_seconds', request.form.get('max_recording_seconds', '1800'))
        set_setting('welcome_new', request.form.get('welcome_new', ''))
        set_setting('welcome_returning', request.form.get('welcome_returning', ''))
        set_setting('system_explanation', request.form.get('system_explanation', ''))
        flash('הגדרות נשמרו בהצלחה')
        return redirect(url_for('admin.settings'))

    current_settings = {
        'price_per_20min_basic': get_setting('price_per_20min_basic', '0.90'),
        'price_per_20min_premium': get_setting('price_per_20min_premium', '1.90'),
        'price_per_20min_video': get_setting('price_per_20min_video', '1.50'),
        'price_per_1000_chars_ocr': get_setting('price_per_1000_chars_ocr', '0.10'),
        'min_balance': get_setting('min_balance', '5'),
        'max_recording_seconds': get_setting('max_recording_seconds', '1800'),
        'welcome_new': get_setting('welcome_new', 'שלום וברוכים הבאים למערכת התמלול.'),
        'welcome_returning': get_setting('welcome_returning', 'שלום וברוכים השבים.'),
        'system_explanation': get_setting('system_explanation', 'מערכת התמלול מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס.'),
    }
    return render_template('admin/settings.html', settings=current_settings)

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
    query = ManagerMessage.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    messages = query.order_by(ManagerMessage.created_at.desc()).all()
    return render_template('admin/manager_messages.html', messages=messages, status_filter=status_filter, timedelta=timedelta)


@admin_bp.route('/messages/<int:id>/play')
@login_required
def play_manager_message(id):
    import requests as req, io
    msg = ManagerMessage.query.get_or_404(id)
    if not msg.rec_url:
        return jsonify({'error': 'אין הקלטה'}), 404
    try:
        r = req.get(msg.rec_url, timeout=120, stream=False)
        r.raise_for_status()
        return send_file(
            io.BytesIO(r.content),
            mimetype='application/octet-stream',
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
    return redirect(url_for('admin.manager_messages'))


@admin_bp.route('/messages/<int:id>/delete', methods=['POST'])
@login_required
def delete_manager_message(id):
    msg = ManagerMessage.query.get_or_404(id)
    db.session.delete(msg)
    db.session.commit()
    flash('הפניה נמחקה')
    return redirect(url_for('admin.manager_messages'))
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
