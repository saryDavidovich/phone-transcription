"""
routes/dictate.py

תור "הקראת כתבי-יד" - תכונה עצמאית ומקבילה לגמרי לצנרת ה-OCR הקיימת
(routes/email_inbound.py -> models.OcrResult, routes/admin.py ocr_*).

הרעיון: כל גיליון כתב-יד שמתקבל במייל נשמר גם כ-ManuscriptPage (ראו models.py,
וההוק ב-_capture_manuscript_page שב-email_inbound.py). נציג צוות פותח כאן דף
בודד, רואה רק את התמונה (בלי טקסט ה-OCR - זה בכוונה, כדי לא "להטות" את מה
שהוא שומע/קורא), לוחץ הקלטה ומקריא את התוכן בקול רם וברור.

איך העיצוב (B/U/כותרת/Enter) בפועל "נתפס": בניגוד לניסיון ראשון שניסה למזג
תמלול רציף אחד מול חותמות-זמן של לחיצות (טריק שדורש timestamps ברמת מילה -
פיצ'ר שקיים ב-Whisper אבל לא ב-Gemini), הגישה כאן פשוטה ואמינה יותר, ועובדת
עם כל מנוע: **כל לחיצת כפתור בדפדפן סוגרת את סגמנט ההקלטה הנוכחי ופותחת
סגמנט חדש** עם מצב העיצוב המעודכן (ראו templates/admin/dictate_studio.html).
כך כל סגמנט אודיו מגיע לשרת כבר "מתויג" בדיוק עם העיצוב שהיה פעיל בזמן
שהוקלט - השרת רק צריך לתמלל כל סגמנט קצר בנפרד (במקביל) ולהרכיב אותם לפי
הסדר. ראו _build_content_from_segments למטה.

מנוע התמלול - ניתן לבחירה לכל הקלטה בנפרד (ראו הדרופדאון בסטודיו), כדי
שאפשר יהיה להריץ את שני המנועים זה מול זה על אותם דפים ולהחליט לפי תוצאות
אמיתיות איזה מהם עדיף - ולא רק לפי תיאוריה:
  * gemini (ברירת מחדל) - gemini-3.5-flash, אותו מודל ואותו קליינט בדיוק
    כמו _gemini_from_url ב-services/transcribe.py, שבו כבר משתמשים בפועל
    לכל התמלולים במסלול המקצועי.
  * openai - gpt-transcribe (הדור החדש שהחליף את gpt-4o-transcribe, זול
    יותר ותומך ב-language hint), אותו קליינט openai שכבר בשימוש במקומות
    אחרים במערכת (routes/email_inbound.py, services/transcribe_service.py).
ניתן לשנות את ברירת המחדל דרך משתנה הסביבה DEFAULT_DICTATION_ENGINE.

חשוב לגבי עלות: פיצול ההקלטה לסגמנטים קצרים (לפי כל לחיצת כפתור) לא מעלה
את העלות בצורה משמעותית - שני המנועים מחייבים לפי משך האודיו בפועל (או
טוקנים לפי משך, אצל Gemini), וזה לא משתנה כתלות בכמות הסגמנטים. התוספת
היחידה היא חזרה על טקסט ההנחיה הקצר (_SEGMENT_PROMPT) בכל סגמנט - עלות
של שברירי אגורות גם בעשרות לחיצות כפתור בדף אחד.

איך העיצוב (B/U/כותרת/Enter) בפועל "נתפס": בניגוד לניסיון ראשון שניסה למזג
תמלול רציף אחד מול חותמות-זמן של לחיצות (טריק שדורש timestamps ברמת מילה -
פיצ'ר שקיים ב-Whisper הישן אבל לא ב-Gemini ולא באופן אמין ב-gpt-transcribe),
הגישה כאן פשוטה ואמינה יותר, ועובדת עם כל מנוע: **כל לחיצת כפתור בדפדפן
סוגרת את סגמנט ההקלטה הנוכחי ופותחת סגמנט חדש** עם מצב העיצוב המעודכן
(ראו templates/admin/dictate_studio.html). כך כל סגמנט אודיו מגיע לשרת כבר
"מתויג" בדיוק עם העיצוב שהיה פעיל בזמן שהוקלט - השרת רק צריך לתמלל כל
סגמנט קצר בנפרד (במקביל) ולהרכיב אותם לפי הסדר. ראו _build_content_from_segments למטה.

שלב א' (נוכחי, לפי סיכום עם המשתמש): רץ בשקט מוחלט - הלקוח לא נחשף לתכונה
הזו בשום צורה. בהמשך, אם התוצאה תוכיח את עצמה, זה עשוי להחליף את ה-OCR.
העיצוב (מודגש/קו תחתון/כותרת) מגיע בפועל ללקוח הסופי כ-Word מעוצב אמיתי.
"""
import os
import io
import json
import time
import uuid
import base64
import logging
import threading
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, send_file, current_app
from flask_login import login_required, current_user

from app import db

log = logging.getLogger(__name__)

dictate_bp = Blueprint('dictate', __name__)


@dictate_bp.context_processor
def _inject_shared_admin_context():
    # templates/admin/base.html מציג new_messages_count בתפריט הצד - זה מוזרק
    # כרגיל ע"י admin_bp.context_processor (routes/admin.py), אבל context
    # processor שרשום על בלופרינט אחד לא חל על בקשות שמטופלות ע"י בלופרינט
    # אחר (dictate_bp) - גם אם שניהם מרנדרים את אותו template. בלי זה כל דף
    # כאן שקורא ל-base.html קורס ב-500 (UndefinedError).
    from routes.admin import inject_new_messages_count
    return inject_new_messages_count()

# אותה תיקייה בדיוק שבה email_inbound.py שומר את עותקי הכתב-יד
MANUSCRIPT_DIR = os.environ.get('MANUSCRIPT_DIR', 'manuscripts')
os.makedirs(MANUSCRIPT_DIR, exist_ok=True)

# תיקיית קבצי אודיו זמניים של ההקלטות עצמן (נמחקות מיד אחרי התמלול)
DICTATION_AUDIO_DIR = os.environ.get('DICTATION_AUDIO_DIR', 'dictation_audio')
os.makedirs(DICTATION_AUDIO_DIR, exist_ok=True)

OPEN_STATUSES = ('pending', 'recording', 'processing', 'review', 'error')

ENGINES = ('gemini', 'openai')
DEFAULT_DICTATION_ENGINE = os.environ.get('DEFAULT_DICTATION_ENGINE', 'gemini')
if DEFAULT_DICTATION_ENGINE not in ENGINES:
    DEFAULT_DICTATION_ENGINE = 'gemini'


# --------------------------------------------------------------------------
# פרומפט תמלול לכל סגמנט - אותה רוח בדיוק כמו הפרומפט הקיים ב-
# services/transcribe.py._gemini_from_url (דיוק מלא, בלי סיכום, מינוח תורני/ארמית)
# --------------------------------------------------------------------------
_SEGMENT_PROMPT = """תמלל את קובץ השמע הקצר הזה בדיוק, מילה במילה, בעברית בלבד.
זהו קטע קצר מתוך הקראה בקול של גיליון כתב-יד תורני - שים לב במיוחד למינוח
תורני נכון, ארמית, ראשי תיבות וציטוטים כפי שנאמרו.
אל תתקן, אל תסכם, אל תוסיף הערות - החזר רק את הטקסט המתומלל עצמו.
אם הקטע שקט או לא מכיל דיבור - החזר מחרוזת ריקה."""


def _webm_to_wav_16k_mono(raw_bytes):
    """ממיר בייטים גולמיים מ-MediaRecorder בדפדפן (webm/opus בד"כ) ל-WAV מונו
    16kHz - בדיוק הפורמט ש-Gemini מקבל בפועל בשאר המערכת (services/transcribe.py)."""
    import tempfile
    from pydub import AudioSegment

    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp_in:
        tmp_in.write(raw_bytes)
        tmp_in_path = tmp_in.name
    try:
        seg = AudioSegment.from_file(tmp_in_path)
        seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        buf = io.BytesIO()
        seg.export(buf, format='wav')
        return buf.getvalue()
    finally:
        os.unlink(tmp_in_path)


def _transcribe_segment_gemini(wav_bytes, client, gtypes):
    """מתמלל סגמנט קצר בודד עם Gemini. אותו דפוס ניסיונות-חוזרים כמו
    _gemini_from_url ב-services/transcribe.py, רק עם פחות ניסיונות/המתנה
    כי כאן זה סגמנט קצר בודד ולא קריאה שלמה."""
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=[
                    _SEGMENT_PROMPT,
                    gtypes.Part.from_bytes(data=wav_bytes, mime_type='audio/wav'),
                ],
            )
            return (response.text or '').strip()
        except Exception as e:
            log.warning(f"gemini segment transcribe attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(5)
    return ''


# gpt-transcribe (הדור החדש, במקום gpt-4o-transcribe) - לא מקבל פרומפט חופשי
# ארוך כמו מודל שיחה, אלא שדה prompt קצר שמשמש כ"רמז הקשר" בלבד, פלוס language
_OPENAI_SEGMENT_PROMPT = ("קטע קצר מהקראה בקול של גיליון כתב-יד תורני בעברית וארמית - "
                          "מינוח תורני, ראשי תיבות וציטוטים.")


def _transcribe_segment_openai(wav_bytes, client):
    """מתמלל סגמנט קצר בודד עם gpt-transcribe. אותו דפוס ניסיונות-חוזרים
    כמו _transcribe_segment_gemini, כדי ששני המנועים יתנהגו זהה מבחוץ."""
    for attempt in range(3):
        try:
            result = client.audio.transcriptions.create(
                model='gpt-transcribe',
                file=('segment.wav', io.BytesIO(wav_bytes), 'audio/wav'),
                language='he',
                prompt=_OPENAI_SEGMENT_PROMPT,
                response_format='text',
            )
            text = result if isinstance(result, str) else getattr(result, 'text', '')
            return (text or '').strip()
        except Exception as e:
            log.warning(f"openai segment transcribe attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(5)
    return ''


# --------------------------------------------------------------------------
# בניית פסקאות/ריצות עיצוב ישירות מהסגמנטים, לפי הסדר. אין כאן שום ניחוש
# לפי חותמות זמן - כל סגמנט כבר "מגיע" עם העיצוב הנכון שלו (ראו הסבר בראש
# הקובץ), אז זו רק הרכבה: heading פותח/סוגר פסקה, new_paragraph (מ-Enter)
# פותח פסקה חדשה, bold/underline קובעים אם להצמיד לריצה האחרונה או לפתוח חדשה.
# --------------------------------------------------------------------------
def _build_content_from_segments(segments):
    """segments: רשימת dict-ים לפי סדר ההקלטה, כל אחד
       {'text', 'bold', 'underline', 'heading', 'new_paragraph'}."""
    paragraphs = []
    pending_new_paragraph = True  # הסגמנט הראשון תמיד פותח פסקה

    for seg in segments:
        if seg.get('new_paragraph'):
            pending_new_paragraph = True
        text = (seg.get('text') or '').strip()
        if not text:
            continue  # סגמנט שקט (למשל לחיצה כפולה בטעות) - לא זורק את pending_new_paragraph
        if pending_new_paragraph or not paragraphs:
            paragraphs.append({'heading': bool(seg.get('heading')), 'runs': []})
            pending_new_paragraph = False

        para = paragraphs[-1]
        runs = para['runs']
        prefixed = (' ' + text) if runs else text
        bold, underline = bool(seg.get('bold')), bool(seg.get('underline'))
        if runs and runs[-1]['bold'] == bold and runs[-1]['underline'] == underline:
            runs[-1]['text'] += prefixed
        else:
            runs.append({'text': prefixed, 'bold': bold, 'underline': underline})

    return paragraphs


# --------------------------------------------------------------------------
# worker ברקע: ממיר+מתמלל כל סגמנט (במקביל, עד 6 בו-זמנית), מרכיב לפסקאות, שומר
# --------------------------------------------------------------------------
def _dictation_worker(app, page_id, segment_files, segment_meta, engine=None):
    """segment_files: נתיבים לקבצי אודיו זמניים, לפי סדר ההקלטה.
       segment_meta: רשימה מקבילה של {'bold','underline','heading','new_paragraph'}.
       engine: 'gemini' או 'openai' - איזה מנוע תמלול להשתמש בו לסגמנטים האלה."""
    engine = engine if engine in ENGINES else DEFAULT_DICTATION_ENGINE
    with app.app_context():
        from models import ManuscriptPage
        from concurrent.futures import ThreadPoolExecutor

        page = ManuscriptPage.query.get(page_id)
        if not page:
            return
        try:
            if engine == 'openai':
                from openai import OpenAI
                client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

                def _process_one(item):
                    idx, path = item
                    with open(path, 'rb') as f:
                        raw = f.read()
                    wav_bytes = _webm_to_wav_16k_mono(raw)
                    return idx, _transcribe_segment_openai(wav_bytes, client)
            else:
                from google import genai
                from google.genai import types as gtypes
                client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))

                def _process_one(item):
                    idx, path = item
                    with open(path, 'rb') as f:
                        raw = f.read()
                    wav_bytes = _webm_to_wav_16k_mono(raw)
                    return idx, _transcribe_segment_gemini(wav_bytes, client, gtypes)

            texts_by_idx = {}
            with ThreadPoolExecutor(max_workers=6) as ex:
                for idx, text in ex.map(_process_one, list(enumerate(segment_files))):
                    texts_by_idx[idx] = text

            segments = [dict(meta, text=texts_by_idx.get(i, '')) for i, meta in enumerate(segment_meta)]
            content = _build_content_from_segments(segments)

            if not content:
                page.status = 'error'
                page.error_message = 'לא זוהה דיבור בהקלטה - נסו להקליט שוב'
                db.session.commit()
                return

            page.content = content
            page.status = 'review'
            page.error_message = None
            db.session.commit()
            log.info(f"dictation processed: page={page_id}, engine={engine}, paragraphs={len(content)}, segments={len(segment_files)}")
        except Exception as e:
            log.error(f"dictation worker error (page={page_id}): {e}", exc_info=True)
            page.status = 'error'
            page.error_message = str(e)
            db.session.commit()
        finally:
            for path in segment_files:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass


# --------------------------------------------------------------------------
# בניית ה-Word המעוצב הסופי - אותו סגנון RTL/גופן מוטמע כמו services/transcribe.py
# --------------------------------------------------------------------------
def _build_manuscript_docx(customer_name, original_filename, content):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    from services.transcribe import (
        FONT_DISPLAY_NAME, FONT_REGULAR_PATH, FONT_BOLD_PATH,
        _embed_font_in_docx, _BIDI_SUCCESSORS,
    )

    FONT_NAME = FONT_DISPLAY_NAME
    _RPR_RTL_SUCCESSORS = ('w:cs', 'w:em', 'w:lang', 'w:eastAsianLayout', 'w:specVanish', 'w:oMath')

    def add_bidi(paragraph):
        pPr = paragraph._p.get_or_add_pPr()
        bidi = OxmlElement('w:bidi')
        pPr.insert_element_before(bidi, *_BIDI_SUCCESSORS)

    def set_rtl(paragraph):
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        add_bidi(paragraph)

    def set_hebrew_font(run, size=None, bold=False, underline=False):
        run.font.name = FONT_NAME
        run.bold = bold
        run.underline = underline
        if size:
            run.font.size = size
        rPr = run._r.get_or_add_rPr()
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = OxmlElement('w:rFonts')
            rPr.append(rFonts)
        rFonts.set(qn('w:cs'), FONT_NAME)
        rFonts.set(qn('w:ascii'), FONT_NAME)
        rFonts.set(qn('w:hAnsi'), FONT_NAME)
        rtl = rPr.find(qn('w:rtl'))
        if rtl is None:
            rtl = OxmlElement('w:rtl')
            rPr.insert_element_before(rtl, *_RPR_RTL_SUCCESSORS)
        lang = rPr.find(qn('w:lang'))
        if lang is None:
            lang = OxmlElement('w:lang')
            rPr.append(lang)
        lang.set(qn('w:val'), 'he-IL')
        lang.set(qn('w:bidi'), 'he-IL')

    doc = Document()
    doc.core_properties.language = 'he-IL'

    normal_style = doc.styles['Normal']
    normal_style.font.name = FONT_NAME
    normal_pPr = normal_style.element.get_or_add_pPr()
    normal_pPr.insert_element_before(OxmlElement('w:bidi'), *_BIDI_SUCCESSORS)

    section = doc.sections[0]
    bidi_doc = OxmlElement('w:bidi')
    section._sectPr.append(bidi_doc)

    title = doc.add_heading(f'כתב יד - {original_filename}', 0)
    set_rtl(title)
    for run in title.runs:
        set_hebrew_font(run)

    if customer_name:
        info = doc.add_paragraph(f'לקוח: {customer_name}')
        set_rtl(info)
        for run in info.runs:
            set_hebrew_font(run, size=Pt(11))

    if not content:
        empty = doc.add_paragraph('(לא הוקלט תוכן)')
        set_rtl(empty)
        for run in empty.runs:
            set_hebrew_font(run)
    else:
        for para_data in content:
            if para_data.get('heading'):
                p = doc.add_heading('', level=1)
            else:
                p = doc.add_paragraph()
            set_rtl(p)
            for run_data in para_data.get('runs', []):
                run = p.add_run(run_data.get('text', ''))
                set_hebrew_font(
                    run,
                    size=None if para_data.get('heading') else Pt(13),
                    bold=bool(run_data.get('bold')) or para_data.get('heading', False),
                    underline=bool(run_data.get('underline')),
                )

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    docx_bytes = buf.read()
    docx_bytes = _embed_font_in_docx(docx_bytes, FONT_NAME, FONT_REGULAR_PATH, FONT_BOLD_PATH)
    return docx_bytes


def _content_to_plain_preview(content):
    """טקסט פשוט (בלי עיצוב) לתצוגה מקדימה בגוף המייל."""
    lines = []
    for para in (content or []):
        text = ''.join(r.get('text', '') for r in para.get('runs', [])).strip()
        if para.get('heading'):
            text = f'* {text} *'
        lines.append(text)
    return '\n\n'.join(lines)


def _send_manuscript_email(to_email, customer_name, original_filename, content):
    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email

    docx_bytes = _build_manuscript_docx(customer_name, original_filename, content)
    docx_b64 = base64.b64encode(docx_bytes).decode('utf-8')
    preview = _content_to_plain_preview(content)

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">כתב יד - {original_filename}</h2>
<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✍️ טקסט</h3>
<div style="line-height:1.8;white-space:pre-wrap;text-align:right;direction:rtl">{preview}</div>
</div>
</div>'''

    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    safe_name = os.path.splitext(original_filename)[0][:40] if original_filename else 'כתב_יד'
    message = Mail(
        from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
        to_emails=to_email,
        subject=f'כתב יד - {original_filename}',
        html_content=html,
    )
    message.attachment = Attachment(
        FileContent(docx_b64),
        FileName(f'כתב_יד_{safe_name}.docx'),
        FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        Disposition('attachment'),
    )
    sg.send(message)


# ==========================================================================
# Routes
# ==========================================================================

@dictate_bp.route('')
@login_required
def queue():
    from models import ManuscriptPage
    status_filter = request.args.get('status', 'open')
    q = ManuscriptPage.query
    if status_filter == 'open':
        q = q.filter(ManuscriptPage.status.in_(OPEN_STATUSES))
        q = q.order_by(ManuscriptPage.created_at.asc())  # תור - הישן קודם
    elif status_filter == 'done':
        q = q.filter(ManuscriptPage.status == 'done')
        q = q.order_by(ManuscriptPage.created_at.desc())
    else:
        q = q.order_by(ManuscriptPage.created_at.desc())

    pages = q.limit(200).all()
    return render_template('admin/dictate_queue.html', pages=pages, status_filter=status_filter)


@dictate_bp.route('/<int:page_id>')
@login_required
def studio(page_id):
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    if page.status == 'pending':
        page.status = 'recording'
        page.claimed_by = getattr(current_user, 'username', 'admin')
        page.claimed_at = datetime.utcnow()
        db.session.commit()
    return render_template(
        'admin/dictate_studio.html',
        page=page,
        default_engine=DEFAULT_DICTATION_ENGINE,
    )


@dictate_bp.route('/<int:page_id>/file')
@login_required
def page_file(page_id):
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    if not page.file_path or not os.path.exists(page.file_path):
        return "הקובץ אינו זמין", 404
    return send_file(page.file_path, as_attachment=False, download_name=page.original_filename)


@dictate_bp.route('/<int:page_id>/process', methods=['POST'])
@login_required
def process(page_id):
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)

    audio_files = request.files.getlist('segments')
    meta_raw = request.form.get('meta', '[]')
    if not audio_files:
        return jsonify({'error': 'לא התקבל אודיו'}), 400

    engine = request.form.get('engine') or DEFAULT_DICTATION_ENGINE
    if engine not in ENGINES:
        engine = DEFAULT_DICTATION_ENGINE

    try:
        segment_meta = json.loads(meta_raw)
    except Exception:
        segment_meta = []
    # הגנה - אם מספר הפריטים לא תואם מכל סיבה, לא קורסים, פשוט משלימים ברירת מחדל
    if len(segment_meta) != len(audio_files):
        log.warning(f"process: meta length ({len(segment_meta)}) != files ({len(audio_files)}), padding")
        segment_meta = (segment_meta + [{}] * len(audio_files))[:len(audio_files)]

    segment_paths = []
    for f in audio_files:
        ext = os.path.splitext(f.filename or '')[1] or '.webm'
        path = os.path.join(DICTATION_AUDIO_DIR, f'{uuid.uuid4().hex}{ext}')
        f.save(path)
        segment_paths.append(path)

    page.status = 'processing'
    page.engine = engine
    page.claimed_by = getattr(current_user, 'username', 'admin')
    db.session.commit()

    app_obj = current_app._get_current_object()
    t = threading.Thread(target=_dictation_worker, args=(app_obj, page_id, segment_paths, segment_meta, engine), daemon=True)
    t.start()

    return jsonify({'status': 'processing', 'engine': engine})


@dictate_bp.route('/<int:page_id>/status')
@login_required
def status(page_id):
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    return jsonify({
        'status': page.status,
        'content': page.content,
        'error_message': page.error_message,
        'engine': page.engine,
    })


@dictate_bp.route('/<int:page_id>/content', methods=['POST'])
@login_required
def save_content(page_id):
    """שמירת תיקונים ידניים קטנים שהנציג עשה בתצוגה המקדימה לפני השליחה."""
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    data = request.get_json(silent=True) or {}
    content = data.get('content')
    if content is None:
        return jsonify({'error': 'חסר content'}), 400
    page.content = content
    db.session.commit()
    return jsonify({'status': 'saved'})


@dictate_bp.route('/<int:page_id>/redo', methods=['POST'])
@login_required
def redo(page_id):
    """מוותרים על התוצאה ומתחילים הקלטה מחדש לאותו דף."""
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    page.status = 'pending'
    page.content = None
    page.error_message = None
    page.claimed_by = None
    page.claimed_at = None
    db.session.commit()
    return jsonify({'status': 'pending'})


@dictate_bp.route('/<int:page_id>/send', methods=['POST'])
@login_required
def send(page_id):
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    if not page.content:
        return jsonify({'error': 'אין תוכן לשליחה'}), 400

    to_email = (request.form.get('to_email') or '').strip() or (page.customer.email or '').strip()
    if not to_email:
        return jsonify({'error': 'אין כתובת מייל ליעד - הזינו כתובת'}), 400

    try:
        _send_manuscript_email(to_email, page.customer.name, page.original_filename, page.content)
        page.sent_at = datetime.utcnow()
        page.sent_to = to_email
        page.status = 'done'
        db.session.commit()
        return jsonify({'status': 'sent', 'to': to_email})
    except Exception as e:
        log.error(f"manuscript send error (page={page_id}): {e}", exc_info=True)
        return jsonify({'error': f'שליחה נכשלה: {e}'}), 500
