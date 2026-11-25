"""
לשונית 'יצירת תמלול' עבור מנהל המוסד - העלאת קובץ, תמלול ברקע (thread,
לא חוסם את הדפדפן), והורדת התוצאה כקובץ Word. נפרד מהזרימה הרגילה של
תלמידים (routes/ivr.py + services/transcribe.py) - זהו כלי עבודה אישי
של המוסד עצמו, לא נצרך מיתרת תלמיד, אבל כן נצרך מיתרת המוסד עצמו
(לפי מחירון המנהל הראשי, בדיוק כמו תמלול תלמיד).
"""
import os
import uuid
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
    """קובץ שהועלה, הקלטה מהדפדפן (source=record), או קישור Drive / ג'מבו מייל (link)."""
    from services import portal_media as PM
    file = request.files.get('audio_file')
    link = (request.form.get('link') or '').strip()
    if (not file or not file.filename) and not link:
        return jsonify({'error': 'יש לבחור קובץ, להקליט או להדביק קישור'}), 400
    if link and not PM.link_kind(link):
        return jsonify({'error': PM.LINK_HELP}), 400

    # אין בדיקת יתרה לפני ההעלאה (כמו לקוח רגיל): אם בסוף התמלול היתרה לא מספיקה - התמלול נשמר
    # "ממתין לתשלום", ומשתחרר אוטומטית (כולל מייל) אחרי טעינת יתרה. היתרה לא יורדת למינוס.
    tier = 'premium' if request.form.get('tier') == 'premium' else 'gemini'
    language = request.form.get('language') if request.form.get('language') in ('he', 'yi', 'en') else 'he'
    output_language = 'original' if request.form.get('output_language') == 'original' else 'he'

    dest_path = None
    if file and file.filename:
        os.makedirs(STATIC_TMP_DIR, exist_ok=True)
        from services.security import safe_audio_ext
        dest_path = os.path.join(STATIC_TMP_DIR, f"inst_{uuid.uuid4().hex}{safe_audio_ext(file.filename)}")
        file.save(dest_path)
        source = 'record' if request.form.get('source') == 'record' else 'file'
        name = file.filename
    else:
        source, name = 'link', PM.link_name(link)

    record = InstitutionUpload(
        institution_id=current_user.id,
        original_filename=(request.form.get('title') or '').strip()[:200] or name,
        tier=tier, status='processing', source=source,
        send_to=(current_user.notify_email or current_user.email) if request.form.get('send_email') else None,
    )
    db.session.add(record)
    db.session.commit()

    from app import app as flask_app
    threading.Thread(
        target=_process_upload,
        args=(flask_app, record.id, dest_path, link or None, tier, language, output_language, record.send_to),
        daemon=True,
    ).start()
    return jsonify({'uploadId': record.id, 'name': record.original_filename})


def _public_url(path):
    base_url = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    return f"{base_url}/static/fax_tmp/{os.path.basename(path)}"


def _process_upload(flask_app, upload_id, local_path, link, tier, language, output_language, send_to=None):
    from services.job_tracker import tracked
    from services import portal_media as PM
    with flask_app.app_context(), tracked('institution_upload', label=f'העלאה {upload_id}'):
        record = InstitutionUpload.query.get(upload_id)
        try:
            if link:                     # קישור: מורידים קודם (Drive / ג'מבו)
                try:
                    local_path, real_name = PM.fetch_link(link, STATIC_TMP_DIR)
                except ValueError as ve:
                    record.status = 'error'
                    record.error_message = str(ve)
                    db.session.commit()
                    return
                if record.original_filename in PM.LINK_NAMES.values() and real_name:
                    record.original_filename = real_name[:200]
                    db.session.commit()
            if PM.needs_conversion(local_path):      # הקלטה מהדפדפן (webm) -> mp3
                local_path = PM.convert_in_place(local_path)
            rec_url = _public_url(local_path)

            is_video = False
            if tier == 'premium':      # תמלול מקצועי - אלף בוט, כמו בתמלול מקצועי של לקוח
                from services.transcribe import alefbot_transcribe_wait
                transcript, duration = alefbot_transcribe_wait(rec_url, f'inst-{upload_id}',
                                                               translate_to_hebrew=(output_language == 'he'))
            else:
                from services.transcribe import _gemini_from_url
                from services import transcribe_v3 as _TV3
                with _TV3.ctx_for_ids(institution_id=record.institution_id):
                    transcript, duration, is_video = _gemini_from_url(rec_url, language, output_language)
            if not transcript:
                record.status = 'error'
                record.error_message = 'התמלול נכשל - לא התקבל טקסט'
                db.session.commit()
                return

            # השמע לנגן באזור המוסד - לימות (נטפרי חוסמת שמע מהאתר שלנו), והעותק אצלנו נמחק
            play = PM.push_to_yemot(local_path, f'u{upload_id}_{uuid.uuid4().hex[:8]}')
            if play:
                record.play_url = play
                PM.remove_quietly(local_path)

            # חיוב יתרת המוסד לפי המחירון - כמו לקוח רגיל: סוג השירות (רגיל / מקצועי / וידאו) ויחידות
            # של 20 או 5 דקות (לפי הגדרת המוסד). אין מספיק יתרה = ממתין לתשלום (לא יורדים למינוס,
            # ולא "מוחקים" את ההפרש כמו פעם) - משתחרר אחרי טעינה (release_pending).
            from services import pricing as P
            from models import Institution
            institution = Institution.query.filter_by(id=record.institution_id).populate_existing().with_for_update().first()
            cost = P.cost_for(institution, duration or 0, P.kind_for_tier(tier, is_video))

            record.transcript = transcript
            record.duration_seconds = duration
            record.cost = cost
            if institution and (institution.balance or 0) + 1e-9 >= cost:
                institution.balance = round((institution.balance or 0) - cost, 2)
                record.charged = True
                record.status = 'done'
            else:
                record.status = 'pending_payment'
            db.session.commit()
            if record.status == 'pending_payment':
                log.info(f'Institution upload {upload_id}: ממתין לתשלום (עלות {cost}, יתרה {institution.balance if institution else 0})')
                try:
                    _email_pending(institution, record)
                except Exception:
                    log.exception(f'Institution upload {upload_id}: מייל "ממתין לתשלום" נכשל')
                return
            if send_to:
                try:
                    _email_docx(send_to, institution, record)
                except Exception:
                    log.exception(f'Institution upload {upload_id}: שליחת המייל נכשלה (התמלול עצמו הצליח)')
        except Exception as e:
            log.exception(f'Institution upload {upload_id} failed')
            record.status = 'error'
            record.error_message = str(e)[:500]
            db.session.commit()


def release_pending(institution_id):
    """אחרי טעינת יתרה למוסד: משחרר תמלולים שחיכו לתשלום (מהישן לחדש) כל עוד היתרה מספיקה -
    מחייב, מסמן 'מוכן', ושולח במייל אם ביקשו. בטוח לקריאה כפולה (נעילת שורה על המוסד)."""
    from models import Institution
    released = 0
    pending = InstitutionUpload.query.filter_by(institution_id=institution_id, status='pending_payment').order_by(
        InstitutionUpload.created_at.asc()).all()
    for rec in pending:
        inst = Institution.query.filter_by(id=institution_id).populate_existing().with_for_update().first()
        db.session.refresh(rec)
        if rec.status != 'pending_payment' or not inst:
            db.session.rollback()
            continue
        cost = rec.cost or 0
        if (inst.balance or 0) + 1e-9 < cost:
            db.session.rollback()
            break                      # לפי הסדר - לא מדלגים לקובץ זול יותר
        inst.balance = round((inst.balance or 0) - cost, 2)
        rec.charged = True
        rec.status = 'done'
        db.session.commit()
        released += 1
        if rec.send_to and rec.transcript:
            try:
                _email_docx(rec.send_to, inst, rec)
            except Exception:
                log.exception(f'release_pending: מייל לתמלול {rec.id} נכשל')
    if released:
        log.info(f'institution {institution_id}: {released} תמלולים שוחררו אחרי טעינת יתרה')
    return released


def release_pending_async(institution_id):
    from app import app as flask_app

    def _run():
        with flask_app.app_context():
            try:
                release_pending(institution_id)
            except Exception:
                log.exception('release_pending failed')
    threading.Thread(target=_run, daemon=True).start()


def _email_pending(institution, record):
    """מייל למוסד: התמלול מוכן אבל ממתין לטעינת יתרה (פעם אחת לכל קובץ)."""
    to = institution and (institution.notify_email or institution.email)
    if not to:
        return
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email
    from services import email_layout as L
    name = record.original_filename or 'הקלטה'
    base = os.environ.get('APP_URL', os.environ.get('APP_BASE_URL', '')).rstrip('/')
    card = (L.title('התמלול מוכן - ממתין לטעינת יתרה')
            + L.text_block(f'הקובץ "{name}" תומלל ({max(1, round((record.duration_seconds or 0) / 60))} דקות, '
                           f'עלות ₪{(record.cost or 0):.2f}), אבל יתרת המוסד (₪{(institution.balance or 0):.2f}) לא מספיקה.\n\n'
                           'מיד אחרי טעינת יתרה בלשונית "הגדרות חיוב" התמלול ישוחרר אוטומטית'
                           + (' וגם יישלח למייל' if record.send_to else '') + '.')
            + (L.button(f'{base}/institution/billing', 'לטעינת יתרה') if base else '')
            + L.signature(can_reply=True))
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=f'התמלול ממתין לטעינת יתרה: {record.original_filename or "הקלטה"}',
               html_content=L.wrap(card, show_circles=False))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)


def upload_docx_bytes(institution, record):
    """קובץ ה-Word (עם מיתוג המוסד) - נבנה מהתמלול השמור, כולל עריכות."""
    from services import portal_docx as PD
    meta = ' · '.join(x for x in [
        record.created_at.strftime('%d/%m/%Y') if record.created_at else '',
        PD.minutes_label(record.duration_seconds) if record.duration_seconds else '',
        'תמלול מקצועי' if record.tier == 'premium' else 'תמלול רגיל'] if x)
    return PD.single(institution, record.original_filename or 'תמלול', record.transcript or '', meta)


def _email_docx(to, institution, record):
    """התמלול של המוסד גם במייל (כמו ללקוח רגיל), עם קובץ ה-Word מצורף."""
    import base64
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email, Attachment, FileContent, FileName, FileType, Disposition
    from services import email_layout as L
    data = upload_docx_bytes(institution, record)
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
        'viewUrl': f'/institution/t/u/{record.id}' if record.status == 'done' else None,
        'name': record.original_filename,
        'durationSeconds': record.duration_seconds,
        'cost': record.cost,
    })


@institution_transcribe_bp.route('/institution/transcribe/download/<int:upload_id>')
@institution_login_required
def download(upload_id):
    record = InstitutionUpload.query.filter_by(id=upload_id, institution_id=current_user.id).first()
    if not record or not record.transcript:
        return 'לא נמצא', 404
    if record.status != 'done':
        return 'התמלול ממתין לטעינת יתרה - אחרי הטעינה הוא ישוחרר אוטומטית', 402
    from routes.download_utils import render_data_uri_download_page
    from services.portal_docx import safe_name
    hebrew_name = f"תמלול - {safe_name(record.original_filename or 'הקלטה')}.docx"
    return render_data_uri_download_page(
        upload_docx_bytes(current_user, record), hebrew_name,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
