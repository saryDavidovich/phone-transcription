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

# הגדל מגבלת PIL — עמודים בזום 450% חורגים מהמגבלה הדיפולטית
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 300_000_000
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


# ביטוי רגולרי לזיהוי קישורי Google Drive - ישירים או עטופים ב-Google redirect
_GDRIVE_RE = re.compile(
    r'https://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=|uc\?.*?id=)([\w-]+)',
    re.IGNORECASE
)
# ביטוי לזיהוי Google redirect שמכיל קישור Drive בתוכו
_GOOGLE_REDIRECT_RE = re.compile(
    r'https://www\.google\.com/url\?[^\s<>"]*?q=(https://(?:drive|docs)\.google\.com/[^\s<>&"]+)',
    re.IGNORECASE
)


def _extract_gdrive_file_id(text):
    """מחפש קישור Google Drive בטקסט ומחזיר את ה-file ID, או None אם לא נמצא."""
    if not text:
        return None

    import urllib.parse

    # נסה קודם לחלץ מ-Google redirect (כשGmail עוטף קישורים)
    redirect_match = _GOOGLE_REDIRECT_RE.search(text)
    if redirect_match:
        inner_url = urllib.parse.unquote(redirect_match.group(1))
        m = _GDRIVE_RE.search(inner_url)
        if m:
            return m.group(1)

    # נסה ישירות
    m = _GDRIVE_RE.search(text)
    return m.group(1) if m else None


def _download_gdrive_file(file_id, dest_dir):
    """
    מוריד קובץ מ-Google Drive לפי file_id לתיקיית dest_dir.
    מניח שהקובץ משותף כ-"כל מי שיש לו קישור יכול לצפות".
    מחזיר (filepath, original_filename, dest_filename) או (None, None, None) בכשלון.
    תומך ב-MP4 ווידאו דרך drive.usercontent.google.com
    """
    import urllib.parse
    import requests

    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
    }

    # רשימת URLים לנסות בסדר — ה-usercontent הוא הכי אמין ל-MP4 וקבצים גדולים
    download_urls = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
    ]

    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        r = None
        content_type = ''

        for url in download_urls:
            r = session.get(url, timeout=300, stream=True, allow_redirects=True)
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', '')
            log.info(f"Drive download attempt url={url[:60]} status={r.status_code} content_type={content_type} size_header={r.headers.get('Content-Length','?')}")

            if 'text/html' not in content_type:
                break  # קיבלנו קובץ אמיתי

            # עדיין HTML — נסה לחלץ uuid/confirm מתוכן
            html_text = r.text
            uuid_match = re.search(r'uuid=([0-9A-Za-z_-]+)', html_text)
            confirm_match = re.search(r'confirm=([0-9A-Za-z_-]+)', html_text)
            if uuid_match or confirm_match:
                params = {'export': 'download', 'id': file_id, 'confirm': 't'}
                if uuid_match:
                    params['uuid'] = uuid_match.group(1)
                if confirm_match:
                    params['confirm'] = confirm_match.group(1)
                r = session.get(
                    'https://drive.usercontent.google.com/download',
                    params=params,
                    timeout=300,
                    stream=True
                )
                r.raise_for_status()
                content_type = r.headers.get('Content-Type', '')
                if 'text/html' not in content_type:
                    break

            log.warning(f"Drive URL {url[:60]} returned HTML, trying next...")

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


# סוגי קבצי תמונה/PDF הנתמכים ל-OCR
IMAGE_MIME_TYPES = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/tiff': 'tiff',
    'image/bmp': 'bmp',
    'application/pdf': 'pdf',
}

IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'tiff', 'tif', 'bmp', 'pdf'}
AUDIO_VIDEO_EXTENSIONS = {'mp3', 'wav', 'm4a', 'ogg', 'flac', 'aac', 'opus', 'webm',
                           'mp4', 'mov', 'avi', 'mkv', '3gp', 'amr'}


def _pick_file():
    """מאתר בין קבצי ה-attachments את הקובץ הרלוונטי.
    מחזיר (file_object, file_type) כאשר file_type הוא 'audio' או 'image'.
    מעדיף אודיו/וידאו על פני תמונה/PDF אם שניהם קיימים."""
    if not request.files:
        return None, None

    audio_candidate = None
    image_candidate = None

    for key in request.files:
        f = request.files[key]
        if not f or not f.filename:
            continue

        mime = (f.mimetype or '').lower()
        ext = os.path.splitext(f.filename or '')[1].lstrip('.').lower()

        # זיהוי אודיו/וידאו
        if mime.startswith('audio') or mime.startswith('video'):
            audio_candidate = f
            continue
        if ext in AUDIO_VIDEO_EXTENSIONS and audio_candidate is None:
            audio_candidate = f
            continue

        # זיהוי תמונה/PDF
        if mime in IMAGE_MIME_TYPES or ext in IMAGE_EXTENSIONS:
            image_candidate = f
            continue

    # העדפה: אודיו > תמונה
    if audio_candidate:
        return audio_candidate, 'audio'
    if image_candidate:
        return image_candidate, 'image'

    # fallback - קובץ ראשון שיש
    for key in request.files:
        f = request.files[key]
        if f and f.filename:
            return f, 'audio'  # ברירת מחדל - תמלול

    return None, None


def _process_ocr_email(filepath, original_filename, customer, sender_email, phone, db):
    """
    מעבד קובץ תמונה/PDF ב-OCR דרך Gemini ושולח את הטקסט חזרה במייל + Word.
    נכנס לתור המשותף עם תמלולים (מקסימום 12 במקביל).
    """
    from services.transcribe import ocr_async
    ocr_async(_ocr_worker, filepath, original_filename, customer.id, customer.email, phone)


def _ocr_worker(filepath, original_filename, customer_id, customer_email, phone):
    """Worker thread שמבצע OCR ושולח תשובה."""
    from app import app, db
    from models import Customer, Transaction
    from routes.admin import get_setting

    log.info(f"OCR worker started: {original_filename}, customer={customer_id}")

    try:
        with app.app_context():
            # בחירת מנוע OCR לפי הגדרות
            ocr_engine = get_setting('ocr_engine', 'gemini')
            price_per_1000 = float(get_setting('price_per_1000_chars_ocr', '0.10'))

        log.info(f"OCR engine: {ocr_engine}")

        if ocr_engine == 'claude':
            ocr_text = _claude_ocr(filepath, original_filename)
        elif ocr_engine == 'gpt4o':
            ocr_text = _gpt4o_ocr(filepath, original_filename)
        else:
            ocr_text = _gemini_ocr(filepath, original_filename)

        if not ocr_text:
            log.error(f"OCR failed for {original_filename}")
            _send_ocr_result_email(
                to=customer_email,
                original_filename=original_filename,
                ocr_text=None,
                char_count=0,
                cost=0,
            )
            return

        char_count = len(ocr_text)
        log.info(f"OCR completed: {char_count} chars")

        import math
        units = math.ceil(char_count / 1000)  # כל 1000 תווים = יחידה אחת (עיגול למעלה)
        cost = round(units * price_per_1000, 2)

        with app.app_context():
            from models import OcrResult
            customer = Customer.query.get(customer_id)
            if customer:
                customer.balance -= cost
                txn = Transaction(
                    customer_id=customer_id,
                    amount=-cost,
                    type='debit',
                    description=f'OCR כתב יד - {original_filename} ({char_count} תווים)',
                )
                db.session.add(txn)
                # שמור תוצאת OCR
                ocr_rec = OcrResult(
                    customer_id=customer_id,
                    original_filename=original_filename,
                    original_file_path=filepath,
                    ocr_text=ocr_text,
                    char_count=char_count,
                    cost=cost,
                    engine=ocr_engine,
                    status='completed',
                )
                db.session.add(ocr_rec)
                db.session.commit()
                log.info(f"OCR charged {cost} to customer {customer_id}, balance={customer.balance}")

        # שליחת תוצאה במייל
        _send_ocr_result_email(
            to=customer_email,
            original_filename=original_filename,
            ocr_text=ocr_text,
            char_count=char_count,
            cost=cost,
        )

    except Exception as e:
        log.error(f"OCR worker error: {e}")
    finally:
        # מחיקת הקובץ הזמני
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                log.info(f"OCR temp file deleted: {filepath}")
        except Exception:
            pass


OCR_PROMPT_TEXT = """אתה סורק OCR מכני - אתה מזהה צורות גרפיות של אותיות בלבד.
אין לך שום ידע שפתי. אינך יודע עברית. אינך יודע מה המשמעות של המילים.
אתה רק מעתיק את מה שאתה רואה, כמו מצלמה שמעתיקה פיקסלים.

כללים ברזל:
• העתק כל אות, כל מילה, כל סימן - בדיוק כפי שהם מצוירים בתמונה
• אסור לתקן שגיאות כתיב - אם כתוב "שלבבל" תכתוב "שלבבל"
• אסור להוסיף מילה שאינה בתמונה
• אסור להסיר מילה שיש בתמונה
• אם מילה לא קריאה: כתוב [?]
• שמור על כל סימני פיסוק, מרכאות, סוגריים, קווים, מספרים
• שמור על מבנה שורות ופסקאות
• אל תוסיף כותרות, הסברים, הערות - רק הטקסט עצמו

התחל ישירות:"""


def _claude_ocr(filepath, original_filename):
    """OCR דרך Claude - תמיכה בתמונות ו-PDF עמוד-עמוד."""
    try:
        import anthropic
        import base64
        import io

        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        ext = os.path.splitext(original_filename or filepath)[1].lstrip('.').lower()

        def ocr_image_bytes(img_bytes, mime='image/png'):
            img_b64 = base64.standard_b64encode(img_bytes).decode('utf-8')
            for attempt in range(3):
                try:
                    response = client.messages.create(
                        model='claude-opus-4-5',
                        max_tokens=4096,
                        messages=[{
                            'role': 'user',
                            'content': [
                                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': img_b64}},
                                {'type': 'text', 'text': OCR_PROMPT_TEXT}
                            ]
                        }]
                    )
                    return response.content[0].text.strip()
                except Exception as e:
                    log.warning(f"Claude OCR attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        import time; time.sleep(8)
            return None

        if ext == 'pdf':
            import fitz
            all_pages = []
            doc = fitz.open(filepath)
            num_pages = len(doc)
            log.info(f"Claude OCR: PDF {num_pages} pages")
            for i in range(num_pages):
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(4.0, 4.0))
                img_bytes = pix.tobytes('png')
                text = ocr_image_bytes(img_bytes)
                all_pages.append(f"--- עמוד {i+1} ---\n{text or '[לא קריא]'}")
            doc.close()
            result = '\n\n'.join(all_pages)
        else:
            mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                        'gif': 'image/gif', 'webp': 'image/webp'}
            mime = mime_map.get(ext, 'image/jpeg')
            with open(filepath, 'rb') as f:
                img_bytes = f.read()
            result = ocr_image_bytes(img_bytes, mime)

        log.info(f"Claude OCR completed: {len(result or '')} chars")
        return result

    except Exception as e:
        log.error(f"Claude OCR error: {e}")
        return None


def _gpt4o_ocr(filepath, original_filename):
    """OCR דרך GPT-4o - תמיכה בתמונות ו-PDF עמוד-עמוד."""
    try:
        import base64
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        ext = os.path.splitext(original_filename or filepath)[1].lstrip('.').lower()

        def ocr_image_bytes(img_bytes, mime='image/png'):
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(
                        model='gpt-4o',
                        max_tokens=4096,
                        messages=[{
                            'role': 'user',
                            'content': [
                                {'type': 'text', 'text': OCR_PROMPT_TEXT},
                                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{img_b64}', 'detail': 'high'}}
                            ]
                        }]
                    )
                    return response.choices[0].message.content.strip()
                except Exception as e:
                    log.warning(f"GPT-4o OCR attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        import time; time.sleep(8)
            return None

        if ext == 'pdf':
            import fitz
            all_pages = []
            doc = fitz.open(filepath)
            num_pages = len(doc)
            log.info(f"GPT-4o OCR: PDF {num_pages} pages")
            for i in range(num_pages):
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(4.0, 4.0))
                img_bytes = pix.tobytes('png')
                text = ocr_image_bytes(img_bytes)
                all_pages.append(f"--- עמוד {i+1} ---\n{text or '[לא קריא]'}")
            doc.close()
            result = '\n\n'.join(all_pages)
        else:
            mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                        'gif': 'image/gif', 'webp': 'image/webp'}
            mime = mime_map.get(ext, 'image/jpeg')
            with open(filepath, 'rb') as f:
                img_bytes = f.read()
            result = ocr_image_bytes(img_bytes, mime)

        log.info(f"GPT-4o OCR completed: {len(result or '')} chars")
        return result

    except Exception as e:
        log.error(f"GPT-4o OCR error: {e}")
        return None


def _preprocess_image_for_ocr(img_bytes):
    """
    עיבוד תמונה לשיפור OCR:
    - המרה ל-Grayscale
    - הגברת ניגודיות (contrast boost)
    - Binarization (שחור-לבן טהור) להסרת רעש רקע
    מחזיר bytes של PNG מעובד.
    """
    try:
        import io
        from PIL import Image, ImageEnhance, ImageOps, ImageFilter

        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')

        # Grayscale
        img = img.convert('L')

        # Contrast boost x2
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)

        # Sharpness boost
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.5)

        # Binarization - Otsu-like threshold
        # ניקוי רעש רקע תוך שמירה על אותיות שחורות
        img = img.point(lambda x: 0 if x < 180 else 255, '1')
        img = img.convert('L')

        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()

    except Exception as e:
        log.warning(f"Image preprocessing failed: {e}, using original")
        return img_bytes


def _gemini_ocr_single_pass(client, img_bytes, page_label, gtypes, prompt):
    """מבצע OCR אחד על תמונה ומחזיר טקסט."""
    from google.genai import types as _gtypes
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=[
            prompt,
            _gtypes.Part.from_bytes(data=img_bytes, mime_type='image/png'),
        ],
    )
    return response.text.strip()


def _gemini_ocr(filepath, original_filename):
    """
    OCR לכתב יד עברי — זיהוי שורות + שליחה מקבילה (15 שורות בו זמנית).
    משתמש במפתח API נפרד לOCR.
    """
    try:
        from google import genai
        from google.genai import types as gtypes
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed
        import numpy as np
        from PIL import Image
        import io

        api_key = os.environ.get('GOOGLE_API_KEY_OCR') or os.environ.get('GOOGLE_API_KEY')
        client = genai.Client(api_key=api_key)

        ext = os.path.splitext(original_filename or filepath)[1].lstrip('.').lower()

        OCR_PROMPT = """אתה סורק OCR מכני - אתה מזהה צורות גרפיות של אותיות בלבד.
אין לך שום ידע שפתי. אינך יודע עברית. אינך יודע מה המשמעות של המילים.
אתה רק מעתיק את מה שאתה רואה, כמו מצלמה שמעתיקה פיקסלים - לא כמו קורא שמבין טקסט.

כללים ברזל - חובה לציית להם במדויק:
• העתק כל אות, כל מילה, כל סימן - בדיוק כפי שהם מצוירים בתמונה, אות אחר אות
• אסור בהחלט להחליף מילה במילה אחרת "הגיונית" יותר - גם אם הצורה לא נראית כמו מילה מוכרת, העתק אותה כפי שהיא
• אסור לתקן שגיאות כתיב, ניקוד, או דקדוק
• אסור להוסיף מילה שאינה בתמונה
• אסור להסיר מילה שיש בתמונה
• אסור לחזור על אותה מילה פעמיים אם היא מופיעה רק פעם אחת בתמונה
• אם מילה לא קריאה לחלוטין: כתוב [?] במקומה ועבור הלאה, אל תנחש
• שמור על סימני פיסוק ומספרים בדיוק כפי שהם
• אל תוסיף כותרות, הסברים, הערות, או מילות קישור - רק את הטקסט הגולמי שרשום בתמונה
• זוהי תמונה של עמוד שלם - תן את כל הטקסט הנמצא בעמוד, שורה אחר שורה, מלמעלה למטה

התחל ישירות, ללא הקדמות:"""

        def _zoom_image(img_bytes, scale=4.5):
            """מגדיל את כל התמונה ב-450% לפני שליחה לגמיני."""
            img = Image.open(io.BytesIO(img_bytes)).convert('L')
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return buf.getvalue()

        def process_page_image(page_img_bytes):
            """מגדיל את כל העמוד ב-450% ושולח כקריאה אחת לגמיני, ללא thinking (חוסך עלות מיותרת)."""
            zoomed_bytes = _zoom_image(page_img_bytes, scale=4.5)
            processed = _preprocess_image_for_ocr(zoomed_bytes)

            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model='gemini-3.5-flash',
                        contents=[OCR_PROMPT, gtypes.Part.from_bytes(data=processed, mime_type='image/png')],
                        config=gtypes.GenerateContentConfig(
                            thinking_config=gtypes.ThinkingConfig(thinking_budget=0)
                        ),
                    )
                    try:
                        thoughts = response.usage_metadata.thoughts_token_count or 0
                        log.info(f"OCR call usage: thoughts={thoughts}, total={response.usage_metadata.total_token_count}")
                    except Exception:
                        pass
                    return (response.text or '').strip()
                except Exception as e:
                    log.warning(f"OCR full page attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        import time as _t; _t.sleep(5)
            return None

        # עיבוד לפי סוג קובץ
        if ext == 'pdf':
            try:
                import fitz
                doc = fitz.open(filepath)
                page_texts = []
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    mat = fitz.Matrix(3, 3)
                    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
                    img_bytes = pix.tobytes('png')
                    log.info(f"OCR PDF page {page_num + 1}/{len(doc)}")
                    text = process_page_image(img_bytes)
                    if text:
                        page_texts.append(f"--- עמוד {page_num + 1} ---\n{text}")
                doc.close()
                return '\n\n'.join(page_texts) if page_texts else None
            except Exception as e:
                log.error(f"OCR PDF error: {e}")
                return None
        else:
            with open(filepath, 'rb') as f:
                img_bytes = f.read()
            return process_page_image(img_bytes)

    except Exception as e:
        log.error(f"OCR error: {e}")
        import traceback; log.error(traceback.format_exc())
        return None


def _send_ocr_result_email(to, original_filename, ocr_text, char_count, cost):
    """שולח את תוצאת ה-OCR במייל עם מסמך Word מצורף."""
    try:
        import sendgrid
        import base64
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
        from services.transcribe import _build_word_doc

        title = f'זיהוי כתב יד - {original_filename}'

        if not ocr_text:
            html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#dc2626">שגיאה בזיהוי כתב יד</h2>
<p>לא הצלחנו לזהות את הטקסט מהקובץ <b>{original_filename}</b>.<br>
אנא ודאו שהתמונה ברורה ונסו שנית.</p>
</div>'''
            message = Mail(
                from_email=os.environ.get('SENDGRID_FROM_EMAIL', ''),
                to_emails=to,
                subject=f'שגיאה בזיהוי כתב יד - {original_filename}',
                html_content=html
            )
            sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
            sg.send(message)
            return

        # בניית מסמך Word
        word_bytes = _build_word_doc(
            name='',
            duration_str=f'{char_count} תווים',
            transcript_fixed=ocr_text,
            title=title,
        )
        word_b64 = base64.b64encode(word_bytes).decode('utf-8')

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">זיהוי כתב יד - {original_filename}</h2>
<p style="color:#6b7280">תווים שזוהו: <b>{char_count}</b> | עלות: <b>₪{cost}</b></p>
<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✍️ טקסט מזוהה</h3>
<div style="line-height:1.8;white-space:pre-wrap;text-align:right;direction:rtl">{ocr_text}</div>
</div>
</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        safe_name = os.path.splitext(original_filename)[0][:40] if original_filename else 'ocr'
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', ''),
            to_emails=to,
            subject=f'זיהוי כתב יד - {original_filename}',
            html_content=html
        )
        message.attachment = Attachment(
            FileContent(word_b64),
            FileName(f'כתב_יד_{safe_name}.docx'),
            FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            Disposition('attachment')
        )
        sg.send(message)
        log.info(f"OCR result email sent to {to}")

    except Exception as e:
        log.error(f"OCR result email error: {e}")


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

        attached_file, file_type = _pick_file()
        gdrive_filepath = None  # אם הורדנו מ-Drive - לניקוי בשגיאה

        if attached_file is None:
            # אין קובץ מצורף - נבדוק אם יש קישור Google Drive בגוף המייל
            body_text = request.form.get('text', '') or _strip_html(request.form.get('html', ''))
            file_id = _extract_gdrive_file_id(body_text)

            if not file_id:
                log.warning(f"email-inbound: לא נמצא קישור Drive בגוף המייל. תחילת גוף: {body_text[:300]}")
                _send_guidance_email(sender_email, 'no_attachment', phone=phone)
                return jsonify({'status': 'rejected', 'reason': 'no_attachment'}), 200

            log.info(f"email-inbound: לא נמצא קובץ מצורף, מנסה Google Drive file_id={file_id}")
            filepath, original_filename, filename = _download_gdrive_file(file_id, RECORDINGS_EMAIL_DIR)

            if not filepath:
                _send_guidance_email(sender_email, 'gdrive_download_failed', phone=phone)
                return jsonify({'status': 'rejected', 'reason': 'gdrive_download_failed'}), 200

            gdrive_filepath = filepath
            # זיהוי סוג מ-Drive לפי סיומת
            drive_ext = os.path.splitext(original_filename or '')[1].lstrip('.').lower()
            file_type = 'image' if drive_ext in IMAGE_EXTENSIONS else 'audio'
            base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
            rec_url = f"{base_url}/api/recordings-email/{filename}"
            duration_seconds = _estimate_duration_seconds(filepath) if file_type == 'audio' else 0

        elif file_type == 'image':
            # --- שמירת קובץ תמונה/PDF ---
            mime = (attached_file.mimetype or '').lower()
            ext = IMAGE_MIME_TYPES.get(mime) or \
                  os.path.splitext(attached_file.filename or '')[1].lstrip('.').lower() or 'jpg'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(RECORDINGS_EMAIL_DIR, filename)
            attached_file.save(filepath)
            original_filename = attached_file.filename or filename
            rec_url = ''
            duration_seconds = 0

        else:
            # --- שמירת קובץ האודיו המצורף ---
            audio_file = attached_file
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

        # --- ניתוב: OCR או תמלול ---
        if file_type == 'image':
            _process_ocr_email(
                filepath=filepath,
                original_filename=original_filename,
                customer=customer,
                sender_email=sender_email,
                phone=phone,
                db=db,
            )
            return jsonify({'status': 'accepted', 'type': 'ocr'}), 200

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
    from routes.admin import get_setting
    price_audio = get_setting('price_per_20min_basic', '0.90')
    price_premium = get_setting('price_per_20min_premium', '1.90')
    price_video = get_setting('price_per_20min_video', '1.50')

    options = [
        ('תמלול רגיל, עברית', ''),
        ('תמלול רגיל, יידיש ← עברית', 'רגיל יידיש עברית'),
        ('תמלול רגיל, יידיש ← יידיש', 'רגיל יידיש יידיש'),
        ('תמלול רגיל, אנגלית ← עברית', 'רגיל אנגלית עברית'),
        ('תמלול מקצועי, פלט בעברית', 'מקצועי עברית'),
        ('תמלול מקצועי, פלט בשפת ההקלטה', 'מקצועי מקור'),
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
ב<b>שורת הנושא</b> של המייל כותבים את מספר הטלפון שלך - <b dir="ltr" style="font-family:monospace">{phone}</b>.
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

<div style="background:#fffbeb;border-right:4px solid #f59e0b;padding:14px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 8px;font-weight:700;color:#92400e">💰 מחירון (לכל 20 דקות, או חלק מהן):</p>
<p style="margin:0;line-height:2;color:#111827">
🎵 <b>תמלול רגיל (אודיו):</b> ₪{price_audio}<br>
⭐ <b>תמלול מקצועי:</b> ₪{price_premium}<br>
🎬 <b>וידאו:</b> ₪{price_video}<br>
</p>
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


@email_bp.route('/send-handwriting-instructions', methods=['POST'])
def send_handwriting_instructions():
    """
    נקרא מה-IVR (שלוחה 6, מקש 1). שולח לכתובת המייל הרשומה של הלקוח
    הוראות לשליחת קבצי כתב יד לזיהוי OCR.
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
            log.info(f"send-handwriting-instructions: אין כתובת מייל רשומה ל-{phone}")
            return jsonify({'status': 'no_email'}), 200

        _send_handwriting_instructions_email(customer.email, customer.phone, customer.name)
        return jsonify({'status': 'sent'}), 200


def _send_handwriting_instructions_email(to_email, phone, name=''):
    subject_display = phone
    link = _mailto_link(phone, 'ocr')

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#111827">
<h2 style="color:#1d4ed8">זיהוי כתב יד במייל <span style="font-size:14px;color:#d97706;font-weight:normal">— גרסה נסיונית</span></h2>
<p>שלום {name or ''},</p>

<div style="background:#fffbeb;border-right:4px solid #f59e0b;padding:14px;margin:14px 0;border-radius:8px">
<p style="margin:0;font-weight:700;color:#92400e">⚠️ שימו לב — שירות זה בגרסה נסיונית בלבד</p>
<p style="margin:8px 0 0;line-height:1.8;color:#111827;font-size:14px">
ייתכנו שגיאות, מילים משובשות, או קטעים שלא יזוהו כראוי, במיוחד בכתב יד צפוף, לא ברור, או בכתב רש"י.<br>
אנו ממליצים לבדוק את התוצאה ולא להסתמך עליה באופן מלא.
</p>
</div>

<p style="line-height:1.8">
ניתן לשלוח תמונות או קבצי PDF של כתב יד לזיהוי, בלי להתקשר למערכת.<br>
פשוט שולחים מייל עם <b>הקובץ מצורף</b> לכתובת:
</p>
<div style="background:#eff6ff;border-right:4px solid #2563eb;padding:14px;margin:14px 0;border-radius:8px;font-size:18px;font-weight:700;text-align:center;direction:ltr">
{TRANSCRIBE_INBOUND_EMAIL}
</div>
<p style="line-height:1.8">
ב<b>שורת הנושא</b> (Subject) של המייל כותבים את מספר הטלפון שלך - <b dir="ltr" style="font-family:monospace">{phone}</b> - ואחריו המילה <b>ocr</b>:<br>
<span dir="ltr" style="font-family:monospace;background:#f3f4f6;padding:4px 10px;border-radius:4px;display:inline-block;margin-top:6px">{phone} ocr</span>
</p>
<p style="line-height:1.8">
התוצאה תישלח בחזרה <b>לאותה כתובת מייל</b> שממנה נשלח הקובץ.
שימוש זה מתאפשר רק מכתובת המייל הרשומה במערכת
(<span dir="ltr" style="font-family:monospace">{to_email}</span>), ובתנאי שיש יתרה בארנק.
</p>

<h3 style="color:#1d4ed8;margin-top:24px">פתח מייל מוכן לשליחה</h3>
<p style="line-height:1.8">
לחיצה על הכפתור תפתח טיוטת מייל עם הכתובת ושורת הנושא ממולאות — צריך רק <b>לצרף את הקובץ</b> ולשלוח:
</p>
<p style="text-align:center;margin:20px 0">
<a href="{link} ocr" style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:16px;display:inline-block">📄 פתח מייל מוכן לזיהוי כתב יד</a>
</p>

<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:14px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 8px;font-weight:700;color:#065f46">סוגי קבצים נתמכים:</p>
<p style="margin:0;line-height:2;color:#111827">
🖼️ <b>תמונות:</b> JPG, PNG, WEBP, HEIC<br>
📄 <b>מסמכים:</b> PDF (כולל רב-עמודי)<br>
</p>
<p style="margin:8px 0 0;font-size:13px;color:#6b7280">ניתן לצרף קובץ אחד בלבד לכל מייל. גודל מקסימלי מומלץ: 25MB.</p>
</div>

<div style="background:#fef2f2;border-right:4px solid #ef4444;padding:14px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 8px;font-weight:700;color:#991b1b">טיפים לתוצאה טובה יותר:</p>
<ul style="margin:0;padding-right:20px;line-height:2;color:#111827;font-size:14px">
<li>צלמו בתאורה טובה, ללא צללים</li>
<li>הדף צריך להיות ישר ולא מקופל</li>
<li>ודאו שהכתב נמצא כולו בתוך התמונה</li>
<li>ככל שהתמונה חדה יותר — התוצאה טובה יותר</li>
</ul>
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
            subject='תמלולפון - הוראות לשליחת כתב יד לזיהוי',
            html_content=html,
        )
        sg.send(message)
        log.info(f"Handwriting instructions email sent to {to_email}")
    except Exception as e:
        log.error(f"Failed to send handwriting instructions email to {to_email}: {e}")
