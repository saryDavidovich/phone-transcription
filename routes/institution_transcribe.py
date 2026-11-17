"""
לשונית 'יצירת תמלול' עבור מנהל המוסד - העלאת קובץ, תמלול ברקע (thread,
לא חוסם את הדפדפן), והורדת התוצאה כקובץ Word. נפרד מהזרימה הרגילה של
תלמידים (routes/ivr.py + services/transcribe.py) - זהו כלי עבודה אישי
של המוסד עצמו, לא נצרך מיתרת תלמיד, אבל כן נצרך מיתרת המוסד עצמו
(לפי מחירון המנהל הראשי, בדיוק כמו תמלול תלמיד).
"""
import os
import uuid
import shutil
import tempfile
import threading
import logging
from flask import Blueprint, render_template, request, jsonify, send_file
from flask_login import current_user
from app import db
from models import InstitutionUpload
from routes.institution import institution_login_required

log = logging.getLogger(__name__)

institution_transcribe_bp = Blueprint('institution_transcribe', __name__)

STATIC_TMP_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')


@institution_transcribe_bp.route('/institution/transcribe')
@institution_login_required
def transcribe_tab():
    uploads = InstitutionUpload.query.filter_by(institution_id=current_user.id).order_by(
        InstitutionUpload.created_at.desc()
    ).limit(30).all()
    from services import pricing as P
    return render_template('institution/transcribe.html', uploads=uploads, unit=P.unit_minutes(current_user),
                           price_basic='%.2f' % P.unit_price(current_user, 'basic'),
                           price_premium='%.2f' % P.unit_price(current_user, 'premium'))


@institution_transcribe_bp.route('/institution/transcribe/upload', methods=['POST'])
@institution_login_required
def upload():
    file = request.files.get('audio_file')
    if not file or not file.filename:
        return jsonify({'error': 'יש לבחור קובץ'}), 400

    if (current_user.balance or 0) <= 0:
        return jsonify({'error': 'אין למוסד יתרה מספיקה לביצוע תמלול. נא לטעון יתרה בלשונית "הגדרות חיוב".'}), 402

    tier = 'premium' if request.form.get('tier') == 'premium' else 'gemini'
    if tier == 'premium':
        from services import pricing as P
        if (current_user.balance or 0) < P.unit_price(current_user, 'premium'):
            return jsonify({'error': 'אין למוסד יתרה מספיקה לתמלול מקצועי. נא לטעון יתרה בלשונית "הגדרות חיוב".'}), 402
    language = request.form.get('language', 'he')
    output_language = 'original' if request.form.get('output_language') == 'original' else 'he'

    os.makedirs(STATIC_TMP_DIR, exist_ok=True)
    from services.security import safe_audio_ext
    ext = safe_audio_ext(file.filename)
    filename = f"inst_{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(STATIC_TMP_DIR, filename)
    file.save(dest_path)

    record = InstitutionUpload(
        institution_id=current_user.id,
        original_filename=file.filename,
        tier=tier,
        status='processing',
    )
    db.session.add(record)
    db.session.commit()

    base_url = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    rec_url = f"{base_url}/static/fax_tmp/{filename}"

    from app import app as flask_app
    t = threading.Thread(
        target=_process_upload,
        args=(flask_app, record.id, rec_url, tier, language, output_language,
              (current_user.notify_email or current_user.email) if request.form.get('send_email') else None),
        daemon=True,
    )
    t.start()

    return jsonify({'uploadId': record.id})


def _process_upload(flask_app, upload_id, rec_url, tier, language, output_language, send_to=None):
    from services.job_tracker import tracked
    with flask_app.app_context(), tracked('institution_upload', label=f'העלאה {upload_id}'):
        record = InstitutionUpload.query.get(upload_id)
        try:
            is_video = False
            if tier == 'premium':      # תמלול מקצועי - אלף בוט, כמו בתמלול מקצועי של לקוח
                from services.transcribe import alefbot_transcribe_wait
                transcript, duration = alefbot_transcribe_wait(rec_url, f'inst-{upload_id}',
                                                               translate_to_hebrew=(output_language == 'he'))
            else:
                from services.transcribe import _gemini_from_url
                transcript, duration, is_video = _gemini_from_url(rec_url, language, output_language)
            if not transcript:
                record.status = 'error'
                record.error_message = 'התמלול נכשל - לא התקבל טקסט'
                db.session.commit()
                return

            # חיוב יתרת המוסד לפי המחירון - כמו לקוח רגיל: סוג השירות (רגיל / מקצועי / וידאו) ויחידות
            # של 20 או 5 דקות (לפי הגדרת המוסד). בכוונה לא חוסמים כאן אם התוצאה חורגת מעט מהיתרה
            # (העבודה כבר בוצעה) - החסימה היא לפני ההעלאה.
            from services import pricing as P
            from models import Institution
            institution = Institution.query.get(record.institution_id)
            cost = P.cost_for(institution, duration or 0, P.kind_for_tier(tier, is_video))
            if institution:
                institution.balance = max(0, (institution.balance or 0) - cost)

            docx_filename = _make_docx(transcript, record.original_filename)
            record.transcript = transcript
            record.duration_seconds = duration
            record.cost = cost
            record.docx_filename = docx_filename
            record.status = 'done'
            db.session.commit()
            if send_to:
                try:
                    _email_docx(send_to, institution, record, docx_filename)
                except Exception:
                    log.exception(f'Institution upload {upload_id}: שליחת המייל נכשלה (התמלול עצמו הצליח)')
        except Exception as e:
            log.exception(f'Institution upload {upload_id} failed')
            record.status = 'error'
            record.error_message = str(e)[:500]
            db.session.commit()


def _email_docx(to, institution, record, docx_filename):
    """התמלול של המוסד גם במייל (כמו ללקוח רגיל), עם קובץ ה-Word מצורף."""
    import base64
    import sendgrid
    from html import escape
    from sendgrid.helpers.mail import Mail, Email, Attachment, FileContent, FileName, FileType, Disposition
    from services import email_layout as L
    with open(os.path.join(STATIC_TMP_DIR, docx_filename), 'rb') as fh:
        data = fh.read()
    name = record.original_filename or 'הקלטה'
    card = (L.title('התמלול מוכן')
            + L.text_block(f'הקובץ "{name}" תומלל ({"מקצועי" if record.tier == "premium" else "רגיל"}, '
                           f'{max(1, round((record.duration_seconds or 0) / 60))} דקות). קובץ ה-Word מצורף, '
                           f'והוא זמין גם בלשונית "יצירת תמלול" באזור המוסד.')
            + L.signature(can_reply=True))
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=f'תמלול: {name}', html_content=L.wrap(card, show_circles=False))
    msg.attachment = Attachment(FileContent(base64.b64encode(data).decode()), FileName(f'תמלול - {name}.docx'),
                                FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                                Disposition('attachment'))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
    log.info(f'Institution upload {record.id}: נשלח במייל ל-{to}')


def _make_docx(transcript, original_filename):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    title = doc.add_heading(f'תמלול: {original_filename or ""}', level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    for line in transcript.split('\n'):
        p = doc.add_paragraph(line)
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    os.makedirs(STATIC_TMP_DIR, exist_ok=True)
    filename = f"transcript_{uuid.uuid4().hex}.docx"
    doc.save(os.path.join(STATIC_TMP_DIR, filename))
    return filename


@institution_transcribe_bp.route('/institution/transcribe/status/<int:upload_id>')
@institution_login_required
def status(upload_id):
    record = InstitutionUpload.query.filter_by(id=upload_id, institution_id=current_user.id).first()
    if not record:
        return jsonify({'error': 'לא נמצא'}), 404
    return jsonify({
        'status': record.status,
        'error': record.error_message,
        'downloadUrl': f'/institution/transcribe/download/{record.id}' if record.status == 'done' else None,
        'durationSeconds': record.duration_seconds,
        'cost': record.cost,
    })


@institution_transcribe_bp.route('/institution/transcribe/download/<int:upload_id>')
@institution_login_required
def download(upload_id):
    record = InstitutionUpload.query.filter_by(id=upload_id, institution_id=current_user.id).first()
    if not record or not record.docx_filename:
        return 'לא נמצא', 404
    path = os.path.join(STATIC_TMP_DIR, record.docx_filename)
    if not os.path.exists(path):
        return 'הקובץ אינו זמין', 404
    from routes.download_utils import render_data_uri_download_page
    hebrew_name = f"תמלול - {record.original_filename or 'הקלטה'}.docx"
    with open(path, 'rb') as f:
        file_bytes = f.read()
    return render_data_uri_download_page(
        file_bytes, hebrew_name,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
