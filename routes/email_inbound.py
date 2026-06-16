"""
routes/email_inbound.py
מקבל מיילים נכנסים מ-SendGrid Inbound Parse עם קובץ הקלטה מצורף.
מתמלל ושולח את התמלול חזרה לאותה כתובת מייל.

פורמט שורת הנושא (Subject):
    <מספר טלפון> [רגיל|מקצועי] [שפת קלט] [שפת פלט]

כל הפרמטרים מעבר למספר הטלפון אופציונליים, בכל סדר.
שפות אפשריות: עברית / יידיש / אנגלית
ברירות מחדל: רגיל (Gemini), עברית->עברית

דוגמאות:
    "0501234567"                              -> רגיל, he->he
    "0501234567 מקצועי"                       -> מקצועי, he->he
    "0501234567 רגיל יידיש עברית"             -> רגיל, yi->he
    "0501234567 יידיש"                        -> רגיל, yi->he (פלט עברית כברירת מחדל)

חיוב: לפי מספר הטלפון שבנושא (לא לפי כתובת המייל - כך אפשר לאותה כתובת
מייל לשלוח בשם כמה מספרים, וכל אחד מחויב מהארנק שלו).

תנאי לעיבוד:
    - קיים לקוח רשום עם מספר הטלפון הזה
    - הלקוח לא חסום
    - כתובת המייל השולחת == כתובת המייל הרשומה ללקוח זה
    - יתרת הלקוח > 0

אם תנאי לא מתקיים - נשלח מייל הדרכה לכתובת השולחת (לא מתבצע תמלול).
"""

import os
import re
import time
import uuid
import logging
import threading
from urllib.parse import quote

from flask import Blueprint, request, jsonify, send_from_directory

from services.transcribe import transcribe_async

log = logging.getLogger(__name__)


def _normalize_israeli_phone(raw):
    """מנקה ומנרמל מספר טלפון ישראלי לפורמט מקומי (05XXXXXXXX / 0XXXXXXXXX).
    מוגדרת כאן מקומית (במקום import) כדי לא להיות תלויה במבנה הפנימי
    של services/transcribe_service.py."""
    phone = (raw or '').strip().replace('-', '').replace(' ', '')
    if phone.startswith('+972'):
        phone = '0' + phone[4:]
    elif phone.startswith('972'):
        phone = '0' + phone[3:]
    return phone

email_bp = Blueprint('email_inbound', __name__)

# כתובת המייל הציבורית שאליה לקוחות שולחים הקלטות לתמלול
TRANSCRIBE_INBOUND_EMAIL = os.environ.get('TRANSCRIBE_INBOUND_EMAIL', '033131795@sheasystem.com')

# תיקייה לשמירת קבצי אודיו שהתקבלו במייל (משם הם מוגשים חזרה כ-rec_url)
RECORDINGS_EMAIL_DIR = os.environ.get('RECORDINGS_EMAIL_DIR', 'recordings_email')
os.makedirs(RECORDINGS_EMAIL_DIR, exist_ok=True)

# מחיקה אוטומטית של קבצי הקלטה שהתקבלו במייל - לאחר כמה ימים נחשבים "ישנים"
RECORDINGS_EMAIL_MAX_AGE_DAYS = float(os.environ.get('RECORDINGS_EMAIL_MAX_AGE_DAYS', '2'))
# בדיקת ניקוי מתבצעת לכל היותר פעם בכמה שעות (לא בכל בקשה)
_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
_last_cleanup_time = 0
_cleanup_lock = threading.Lock()


def _cleanup_old_email_recordings():
    """מוחק קבצים ב-RECORDINGS_EMAIL_DIR שעברו את גיל המקסימום.
    רץ ברקע (thread נפרד) כדי לא לעכב את תגובת ה-webhook."""
    try:
        now = time.time()
        max_age_seconds = RECORDINGS_EMAIL_MAX_AGE_DAYS * 24 * 60 * 60
        removed = 0
        for fname in os.listdir(RECORDINGS_EMAIL_DIR):
            fpath = os.path.join(RECORDINGS_EMAIL_DIR, fname)
            try:
                if not os.path.isfile(fpath):
                    continue
                if now - os.path.getmtime(fpath) > max_age_seconds:
                    os.remove(fpath)
                    removed += 1
            except OSError as e:
                log.warning(f"email-inbound cleanup: failed to remove {fpath}: {e}")
        if removed:
            log.info(f"email-inbound cleanup: removed {removed} old recording file(s)")
    except Exception as e:
        log.warning(f"email-inbound cleanup error: {e}")


def _maybe_run_cleanup():
    """מריץ ניקוי לכל היותר פעם ב-_CLEANUP_INTERVAL_SECONDS, ברקע."""
    global _last_cleanup_time
    now = time.time()
    with _cleanup_lock:
        if now - _last_cleanup_time < _CLEANUP_INTERVAL_SECONDS:
            return
        _last_cleanup_time = now
    threading.Thread(target=_cleanup_old_email_recordings, daemon=True).start()


TIER_MAP = {
    'רגיל': 'gemini',
    'בסיסי': 'gemini',
    'מקצועי': 'premium',
    'פרימיום': 'premium',
}

LANG_MAP = {
    'עברית': 'he',
    'יידיש': 'yi',
    'אנגלית': 'en',
}

# מיפוי content-type נפוץ -> סיומת קובץ
AUDIO_EXT_MAP = {
    'audio/mpeg': 'mp3',
    'audio/mp3': 'mp3',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'audio/wave': 'wav',
    'audio/mp4': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/aac': 'm4a',
    'audio/ogg': 'ogg',
    'audio/amr': 'amr',
}

# תווי כיווניות נסתרים שמופיעים לפעמים בכותרות מייל בעברית
_DIRECTION_MARKS_RE = re.compile(r'[\u200e\u200f\u202a-\u202e]')


def _clean_text(s):
    return _DIRECTION_MARKS_RE.sub('', s or '').strip()


def _parse_subject(subject):
    """מפענח את שורת הנושא לפי הפורמט שמתואר למעלה. מחזיר dict או None אם אין מספר טלפון."""
    tokens = _clean_text(subject).split()
    if not tokens:
        return None

    phone = _normalize_israeli_phone(tokens[0])
    if not phone or not phone.isdigit():
        return None

    tier = 'gemini'
    lang_tokens = []

    for tok in tokens[1:]:
        tok_clean = tok.strip()
        if tok_clean in TIER_MAP:
            tier = TIER_MAP[tok_clean]
        elif tok_clean in LANG_MAP:
            lang_tokens.append(LANG_MAP[tok_clean])

    language = lang_tokens[0] if len(lang_tokens) >= 1 else 'he'
    output_language = lang_tokens[1] if len(lang_tokens) >= 2 else 'he'

    return {
        'phone': phone,
        'tier': tier,
        'language': language,
        'output_language': output_language,
    }


def _extract_sender_email(from_header):
    """מתוך 'שם <email@domain.com>' או 'email@domain.com' מחזיר את כתובת המייל בלבד, lowercase."""
    from_header = (from_header or '').strip()
    m = re.search(r'<(.+?)>', from_header)
    addr = m.group(1) if m else from_header
    return addr.strip().lower()


_STYLE_SCRIPT_RE = re.compile(r'<(style|script)[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_WHITESPACE_RE = re.compile(r'\s+')


def _strip_html(html):
    """מסיר <style>/<script> ותגי HTML, ומחזיר טקסט נקי - שימושי ללוגים/דיבאג."""
    if not html:
        return ''
    text = _STYLE_SCRIPT_RE.sub(' ', html)
    text = _TAG_RE.sub(' ', text)
    text = _WHITESPACE_RE.sub(' ', text)
    return text.strip()


# ביטוי רגולרי לזיהוי קישורי Google Drive (קובץ ותיקייה)
_GDRIVE_RE = re.compile(
    r'https://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=|uc\?.*?id=)([\w-]+)',
    re.IGNORECASE
)


def _extract_gdrive_file_id(text):
    """מחפש קישור Google Drive בטקסט ומחזיר את ה-file ID, או None אם לא נמצא."""
    if not text:
        return None
    m = _GDRIVE_RE.search(text)
    return m.group(1) if m else None


def _download_gdrive_file(file_id, dest_dir):
    """
    מוריד קובץ מ-Google Drive לפי file_id לתיקיית dest_dir.
    מניח שהקובץ משותף כ-"כל מי שיש לו קישור יכול לצפות".
    מחזיר (filepath, original_filename) או (None, None) בכשלון.
    """
    import urllib.parse

    # URL להורדה ישירה ללא אימות (עובד לקבצים ציבוריים / anyone with link)
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

    try:
        session = requests.Session()
        # קריאה ראשונה - עשויה להחזיר דף אישור לקבצים גדולים
        r = session.get(download_url, timeout=60, stream=True, allow_redirects=True)
        r.raise_for_status()

        content_type = r.headers.get('Content-Type', '')

        # אם קיבלנו HTML - כנראה דף אישור של Drive לקבצים גדולים
        if 'text/html' in content_type:
            # נחפש את קישור האישור בתוכן
            html_text = r.text
            confirm_match = re.search(r'confirm=([0-9A-Za-z_-]+)', html_text)
            uuid_match = re.search(r'uuid=([0-9A-Za-z_-]+)', html_text)
            if confirm_match or uuid_match:
                params = {'export': 'download', 'id': file_id, 'confirm': 't'}
                if uuid_match:
                    params['uuid'] = uuid_match.group(1)
                r = session.get(
                    'https://drive.google.com/uc',
                    params=params,
                    timeout=300,
                    stream=True
                )
                r.raise_for_status()
                content_type = r.headers.get('Content-Type', '')

        # קבע סיומת לפי Content-Type
        ct_to_ext = {
            'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
            'audio/wav': 'wav', 'audio/x-wav': 'wav',
            'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a',
            'audio/ogg': 'ogg', 'audio/flac': 'flac',
            'audio/aac': 'aac', 'audio/opus': 'opus',
            'video/mp4': 'mp4', 'video/quicktime': 'mov',
            'video/x-msvideo': 'avi', 'video/x-matroska': 'mkv',
            'video/3gpp': '3gp', 'application/octet-stream': 'mp3',
        }
        ct_base = content_type.split(';')[0].strip().lower()
        ext = ct_to_ext.get(ct_base, 'mp3')

        # נסה לחלץ שם קובץ מ-Content-Disposition
        original_filename = None
        cd = r.headers.get('Content-Disposition', '')
        fn_match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
        if fn_match:
            original_filename = urllib.parse.unquote(fn_match.group(1).strip())
        if not original_filename:
            original_filename = f"gdrive_{file_id}.{ext}"

        # שמור לקובץ
        dest_filename = f"{uuid.uuid4().hex}.{ext}"
        dest_path = os.path.join(dest_dir, dest_filename)
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        size = os.path.getsize(dest_path)
        log.info(f"Google Drive download: file_id={file_id}, size={size}, ext={ext}, saved={dest_filename}")
        return dest_path, original_filename, dest_filename

    except Exception as e:
        log.error(f"Google Drive download failed for file_id={file_id}: {e}")
        return None, None, None


def _estimate_duration_seconds(filepath):
    """מנסה לחשב משך אודיו (בשניות) ללא תלות בפורמט, לצורך חיוב התחלתי."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(filepath)
        if audio is not None and audio.info and audio.info.length:
            return int(audio.info.length)
    except Exception as e:
        log.warning(f"duration estimate failed for {filepath}: {e}")
    return 0


def _pick_audio_file():
    """מאתר בין קבצי ה-attachments את קובץ האודיו. מעדיף content-type שמתחיל ב-audio."""
    if not request.files:
        return None

    for key in request.files:
        f = request.files[key]
        if f and f.mimetype and f.mimetype.startswith('audio'):
            return f

    # fallback - אם אין mimetype תקין, קח את הקובץ הראשון שאינו ריק
    for key in request.files:
        f = request.files[key]
        if f and f.filename:
            return f

    return None


@email_bp.route('/email-inbound', methods=['POST'])
def email_inbound():
    from app import app, db
    from models import Customer, Recording

    _maybe_run_cleanup()

    sender_email = _extract_sender_email(request.form.get('from', ''))
    subject = request.form.get('subject', '')

    parsed = _parse_subject(subject)
    if not parsed:
        raw_text = request.form.get('text') or ''
        raw_html = request.form.get('html') or ''
        body_text = raw_text.strip() or _strip_html(raw_html)
        log.warning(f"email-inbound: שורת נושא לא תקינה '{subject}' מאת {sender_email}")
        if body_text:
            log.warning(f"email-inbound: תוכן ההודעה: {body_text[:2000]}")
        # אין למי לענות (אין מספר טלפון תקין) - רק לוג, בלי תגובה
        return jsonify({'status': 'ignored', 'reason': 'invalid_subject'}), 200

    phone = parsed['phone']

    with app.app_context():
        customer = Customer.query.filter_by(phone=phone).first()

        if not customer:
            _send_guidance_email(sender_email, 'not_registered', phone=phone)
            return jsonify({'status': 'rejected', 'reason': 'customer_not_found'}), 200

        if getattr(customer, 'is_blocked', False):
            _send_guidance_email(sender_email, 'blocked', phone=phone)
            return jsonify({'status': 'rejected', 'reason': 'blocked'}), 200

        registered_email = (customer.email or '').strip().lower()
        if not registered_email or registered_email != sender_email:
            _send_guidance_email(sender_email, 'email_mismatch', phone=phone)
            return jsonify({'status': 'rejected', 'reason': 'email_mismatch'}), 200

        if customer.balance <= 0:
            _send_guidance_email(sender_email, 'low_balance', phone=phone)
            return jsonify({'status': 'rejected', 'reason': 'low_balance'}), 200

        audio_file = _pick_audio_file()
        gdrive_filepath = None  # אם הורדנו מ-Drive - לניקוי בשגיאה

        if audio_file is None:
            # אין קובץ מצורף - נבדוק אם יש קישור Google Drive בגוף המייל
            body_text = request.form.get('text', '') or _strip_html(request.form.get('html', ''))
            file_id = _extract_gdrive_file_id(body_text)

            if not file_id:
                _send_guidance_email(sender_email, 'no_attachment', phone=phone)
                return jsonify({'status': 'rejected', 'reason': 'no_attachment'}), 200

            log.info(f"email-inbound: לא נמצא קובץ מצורף, מנסה Google Drive file_id={file_id}")
            filepath, original_filename, filename = _download_gdrive_file(file_id, RECORDINGS_EMAIL_DIR)

            if not filepath:
                _send_guidance_email(sender_email, 'gdrive_download_failed', phone=phone)
                return jsonify({'status': 'rejected', 'reason': 'gdrive_download_failed'}), 200

            gdrive_filepath = filepath
            base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
            rec_url = f"{base_url}/api/recordings-email/{filename}"
            duration_seconds = _estimate_duration_seconds(filepath)

        else:
            # --- שמירת קובץ האודיו המצורף ---
            ext = AUDIO_EXT_MAP.get(
                (audio_file.mimetype or '').lower(),
                (os.path.splitext(audio_file.filename or '')[1].lstrip('.') or 'mp3').lower()
            )
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(RECORDINGS_EMAIL_DIR, filename)
            audio_file.save(filepath)

            base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
            rec_url = f"{base_url}/api/recordings-email/{filename}"
            duration_seconds = _estimate_duration_seconds(filepath)
            original_filename = audio_file.filename or filename

        call_id = f"email-{uuid.uuid4().hex}"
        rec = Recording(
            customer_id=customer.id,
            call_id=call_id,
            duration_seconds=duration_seconds,
            status='recording',
            delivery_method='email',
            delivered_to=customer.email,
            rec_url=rec_url,
            source_filename=original_filename,
        )
        db.session.add(rec)
        db.session.commit()

        log.info(
            f"email-inbound: call_id={call_id} phone={phone} "
            f"tier={parsed['tier']} lang={parsed['language']}->{parsed['output_language']} "
            f"file={filename} duration~{duration_seconds}s"
        )

        transcribe_async(
            call_id=call_id,
            rec_url=rec_url,
            customer_id=customer.id,
            delivery_method='email',
            delivered_to=customer.email,
            duration_seconds=duration_seconds,
            transcription_tier=parsed['tier'],
            language=parsed['language'],
            output_language=parsed['output_language'],
        )

    return jsonify({'status': 'processing', 'call_id': call_id}), 200


@email_bp.route('/recordings-email/<filename>')
def serve_email_recording(filename):
    return send_from_directory(RECORDINGS_EMAIL_DIR, filename)


_GUIDANCE_MESSAGES = {
    'not_registered': lambda phone: f'''
לא נמצא לקוח רשום עם מספר הטלפון <b>{phone}</b> במערכת תמלולפון.<br><br>
כדי להשתמש בשירות תמלול דרך המייל, יש להירשם תחילה במערכת:<br>
התקשרו למספר המערכת <b>מהטלפון שאת/ה רוצה לשייך לחיוב</b>, ועקבו אחרי ההנחיות
הקוליות לעדכון כתובת מייל וטעינת ארנק.<br><br>
לאחר ההרשמה ועדכון המייל, ניתן לשלוח מייל עם קובץ הקלטה ולקבל את התמלול חזרה
לאותה כתובת מייל.
''',
    'blocked': lambda phone: f'''
חשבון הלקוח עם מספר הטלפון <b>{phone}</b> חסום במערכת.<br><br>
לפרטים יש לפנות לשירות הלקוחות.
''',
    'email_mismatch': lambda phone: f'''
כתובת המייל ששלחה את ההקלטה הזו אינה תואמת לכתובת המייל הרשומה למספר
הטלפון <b>{phone}</b> במערכת.<br><br>
תמלול דרך מייל מתאפשר רק מכתובת המייל המעודכנת ברישום אותו מספר טלפון.
אם זו לא כתובת המייל המעודכנת שלך - התקשרו למערכת מהטלפון הרלוונטי ועדכנו
את כתובת המייל בתפריט "עדכון פרטים".
''',
    'low_balance': lambda phone: f'''
היתרה במערכת תמלולפון למספר הטלפון <b>{phone}</b> אינה מספיקה לביצוע תמלול.<br><br>
כדי לטעון את הארנק, התקשרו למספר המערכת מהטלפון הזה ועקבו אחרי ההנחיות הקוליות
לטעינת ארנק.<br><br>
לאחר הטעינה ניתן לשלוח מחדש מייל עם קובץ ההקלטה.
''',
    'no_attachment': lambda phone: f'''
לא נמצא קובץ הקלטת אודיו מצורף למייל שנשלח עם הנושא <b>{phone}</b>.<br><br>
יש לצרף את קובץ ההקלטה למייל, או לשלוח קישור Google Drive לקובץ משותף.<br><br>
אם הקובץ גדול מ-25MB, ניתן להעלות אותו ל-Google Drive, לשתף אותו
("כל מי שיש לו קישור יכול לצפות"), ולשלוח את הקישור בגוף המייל (ללא קובץ מצורף).
''',
    'gdrive_download_failed': lambda phone: f'''
לא הצלחנו להוריד את הקובץ מהקישור Google Drive שנשלח במייל עם הנושא <b>{phone}</b>.<br><br>
אנא ודאו שהקובץ ב-Google Drive משותף כ-<b>"כל מי שיש לו קישור יכול לצפות"</b> ונסו שוב.<br><br>
<b>כיצד לשתף ב-Google Drive:</b><br>
1. לחצו על הקובץ ב-Drive → שתף → שנה ל"כל אחד עם הקישור"<br>
2. העתיקו את הקישור ושלחו אותו בגוף המייל<br>
3. שורת הנושא: <b>{phone}</b> (כרגיל)
''',
}


def _send_guidance_email(to_email, reason, phone=''):
    if not to_email:
        log.warning(f"_send_guidance_email: אין כתובת מייל לשלוח אליה (reason={reason}, phone={phone})")
        return

    body_fn = _GUIDANCE_MESSAGES.get(reason)
    body_html = body_fn(phone) if body_fn else 'אירעה שגיאה בעיבוד הבקשה.'

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#b91c1c">לא ניתן היה לעבד את הבקשה</h2>
<div style="background:#fef2f2;border-right:4px solid #ef4444;padding:16px;margin:16px 0;border-radius:8px;line-height:1.8">
{body_html}
</div>
<p style="color:#6b7280;font-size:13px">מערכת תמלולפון 03-3131795</p>
</div>'''

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to_email,
            subject='תמלולפון - לא ניתן לעבד את הבקשה',
            html_content=html,
        )
        sg.send(message)
        log.info(f"Guidance email sent to {to_email} (reason: {reason})")
    except Exception as e:
        log.error(f"Failed to send guidance email to {to_email}: {e}")


@email_bp.route('/send-email-instructions', methods=['POST'])
def send_email_instructions():
    """
    נקרא מה-IVR (שלוחה 5, מקש 1). שולח לכתובת המייל הרשומה של הלקוח
    הוראות מפורטות ומעוצבות + קישורי mailto מוכנים לשליחת הקלטה לתמלול במייל.
    """
    from app import app
    from models import Customer

    data = request.get_json(silent=True) or {}
    phone = _normalize_israeli_phone(data.get('phone', ''))

    if not phone:
        return jsonify({'status': 'error', 'reason': 'missing_phone'}), 400

    with app.app_context():
        customer = Customer.query.filter_by(phone=phone).first()

        if not customer or not (customer.email or '').strip():
            log.info(f"send-email-instructions: אין כתובת מייל רשומה ל-{phone}")
            return jsonify({'status': 'no_email'}), 200

        _send_instructions_email(customer.email, customer.phone, customer.name)
        return jsonify({'status': 'sent'}), 200


def _mailto_link(phone, extra=''):
    subject = phone if not extra else f'{phone} {extra}'
    return f"mailto:{TRANSCRIBE_INBOUND_EMAIL}?subject={quote(subject)}"


def _send_instructions_email(to_email, phone, name=''):
    options = [
        ('תמלול רגיל, עברית', ''),
        ('תמלול רגיל, יידיש → עברית', 'רגיל יידיש עברית'),
        ('תמלול רגיל, יידיש → יידיש', 'רגיל יידיש יידיש'),
        ('תמלול רגיל, אנגלית → עברית', 'רגיל אנגלית עברית'),
        ('תמלול מקצועי, עברית', 'מקצועי'),
    ]

    rows_html = ''
    for label, extra in options:
        subject_display = phone if not extra else f'{phone} {extra}'
        link = _mailto_link(phone, extra)
        rows_html += f'''
<tr>
<td style="padding:10px;border:1px solid #e5e7eb">{label}</td>
<td style="padding:10px;border:1px solid #e5e7eb;font-family:monospace">{subject_display}</td>
<td style="padding:10px;border:1px solid #e5e7eb;text-align:center">
<a href="{link}" style="background:#2563eb;color:#fff;text-decoration:none;padding:8px 16px;border-radius:6px;font-weight:600;display:inline-block">פתח מייל מוכן</a>
</td>
</tr>'''

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#111827">
<h2 style="color:#1d4ed8">שליחת הקלטה לתמלול במייל</h2>
<p>שלום {name or ''},</p>
<p style="line-height:1.8">
ניתן לשלוח הקלטה לתמלול גם באמצעות מייל, בלי להתקשר למערכת.
פשוט שולחים מייל עם <b>קובץ ההקלטה מצורף</b> לכתובת:
</p>
<div style="background:#eff6ff;border-right:4px solid #2563eb;padding:14px;margin:14px 0;border-radius:8px;font-size:18px;font-weight:700;text-align:center;direction:ltr">
{TRANSCRIBE_INBOUND_EMAIL}
</div>
<p style="line-height:1.8">
ב<b>שורת הנושא</b> (Subject) של המייל כותבים את מספר הטלפון שלך - <b dir="ltr" style="font-family:monospace">{phone}</b>.
אפשר גם להוסיף אחרי המספר, מופרד ברווחים, את סוג התמלול (רגיל / מקצועי) ואת שפת ההקלטה ושפת הפלט הרצויה (עברית / יידיש / אנגלית).
</p>
<p style="line-height:1.8">
התמלול יישלח בחזרה <b>לאותה כתובת מייל</b> שממנה נשלחה ההקלטה. שימוש זה מתאפשר רק מכתובת המייל הרשומה במערכת
(<span dir="ltr" style="font-family:monospace">{to_email}</span>), ובתנאי שיש יתרה בארנק - החיוב מתבצע לפי מספר הטלפון שצוין בנושא.
</p>

<h3 style="color:#1d4ed8;margin-top:24px">דוגמאות מוכנות לשימוש</h3>
<p style="line-height:1.8">
הלחצנים הבאים פותחים טיוטת מייל חדשה עם הכתובת ושורת הנושא ממולאות מראש - צריך רק <b>לצרף את קובץ ההקלטה</b> ולשלוח.
</p>
<table style="width:100%;border-collapse:collapse;margin:14px 0;font-size:14px">
<thead>
<tr style="background:#f3f4f6">
<th style="padding:10px;border:1px solid #e5e7eb;text-align:right">סוג תמלול</th>
<th style="padding:10px;border:1px solid #e5e7eb;text-align:right">שורת הנושא</th>
<th style="padding:10px;border:1px solid #e5e7eb;text-align:center">פתיחת מייל</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

<p style="color:#6b7280;font-size:13px;line-height:1.8">
שים לב: לחיצה על "פתח מייל מוכן" תפתח את תוכנת המייל המוגדרת כברירת מחדל במכשיר שלך (Gmail, Outlook וכו'),
עם הכתובת ושורת הנושא ממולאות. יש לצרף את קובץ ההקלטה באופן רגיל ולשלוח.
</p>

<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:14px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 8px;font-weight:700;color:#065f46">סוגי קבצים נתמכים לצירוף:</p>
<p style="margin:0;line-height:2;color:#111827">
🎵 <b>אודיו:</b> MP3, WAV, M4A, OGG, FLAC, AAC, OPUS, WEBM<br>
🎬 <b>וידאו:</b> MP4, MOV, AVI, MKV, 3GP<br>
</p>
<p style="margin:8px 0 0;font-size:13px;color:#6b7280">ניתן לצרף קובץ אחד בלבד לכל מייל. גודל מקסימלי מומלץ: 25MB.</p>
</div>

<div style="background:#eff6ff;border-right:4px solid #3b82f6;padding:14px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 8px;font-weight:700;color:#1e40af">📁 קובץ גדול מ-25MB? שלחו קישור Google Drive</p>
<p style="margin:0;line-height:1.8;color:#111827;font-size:14px">
אם הקובץ גדול מדי לצירוף רגיל, העלו אותו ל-Google Drive ושלחו את הקישור בגוף המייל (ללא קובץ מצורף).<br>
<b>חשוב:</b> הקובץ ב-Drive חייב להיות משותף כ-"כל מי שיש לו קישור יכול לצפות".<br>
שורת הנושא נשארת זהה (מספר הטלפון + סוג תמלול כרגיל).
</p>
</div>

<p style="color:#6b7280;font-size:13px;margin-top:24px">מערכת תמלולפון 03-3131795</p>
</div>'''

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to_email,
            subject='תמלולפון - הוראות לשליחת הקלטה לתמלול במייל',
            html_content=html,
        )
        sg.send(message)
        log.info(f"Instructions email sent to {to_email}")
    except Exception as e:
        log.error(f"Failed to send instructions email to {to_email}: {e}")
