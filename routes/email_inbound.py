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
import uuid
import logging

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

# תיקייה לשמירת קבצי אודיו שהתקבלו במייל (משם הם מוגשים חזרה כ-rec_url)
RECORDINGS_EMAIL_DIR = os.environ.get('RECORDINGS_EMAIL_DIR', 'recordings_email')
os.makedirs(RECORDINGS_EMAIL_DIR, exist_ok=True)

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
        if audio_file is None:
            _send_guidance_email(sender_email, 'no_attachment', phone=phone)
            return jsonify({'status': 'rejected', 'reason': 'no_attachment'}), 200

        # --- שמירת קובץ האודיו ---
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

        call_id = f"email-{uuid.uuid4().hex}"
        rec = Recording(
            customer_id=customer.id,
            call_id=call_id,
            duration_seconds=duration_seconds,
            status='recording',
            delivery_method='email',
            delivered_to=customer.email,
            rec_url=rec_url,
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
יש לשלוח מייל חדש עם קובץ אודיו (mp3 / wav / m4a / ogg) מצורף, ובשורת הנושא
לציין את מספר הטלפון (ואופציונלית: סוג תמלול ושפות, למשל
"0501234567 רגיל יידיש עברית").
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
