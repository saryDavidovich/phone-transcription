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
import math
import time
import uuid
import base64
import mimetypes
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
#
# no_space_before/no_space_after: סגמנטים של סימני פיסוק שהוכנסו בלחיצת כפתור
# (סוגריים/מקף/נקודותיים - ראו PUNCTUATION_BUTTONS בסטודיו) מסמנים את זה כדי
# שלא ייכנס רווח מיותר צמוד לסימן (למשל "(שלום)" ולא "( שלום )"). סגמנט אודיו
# רגיל תמיד מקבל רווח מפריד לפניו כברירת מחדל, בדיוק כמו קודם.
# --------------------------------------------------------------------------
def _build_content_from_segments(segments):
    """segments: רשימת dict-ים לפי סדר ההקלטה, כל אחד
       {'text', 'bold', 'underline', 'heading', 'new_paragraph',
        'no_space_before'?, 'no_space_after'?}."""
    paragraphs = []
    pending_new_paragraph = True  # הסגמנט הראשון תמיד פותח פסקה
    prev_no_space_after = True    # אין רווח לפני התו הראשון בפסקה

    for seg in segments:
        if seg.get('new_paragraph'):
            pending_new_paragraph = True
        text = (seg.get('text') or '').strip()
        if not text:
            continue  # סגמנט שקט (למשל לחיצה כפולה בטעות) - לא זורק את pending_new_paragraph
        if pending_new_paragraph or not paragraphs:
            paragraphs.append({'heading': bool(seg.get('heading')), 'runs': []})
            pending_new_paragraph = False
            prev_no_space_after = True  # תו ראשון בפסקה חדשה - בלי רווח מוביל

        para = paragraphs[-1]
        runs = para['runs']
        suppress_space = prev_no_space_after or bool(seg.get('no_space_before'))
        prefixed = text if (not runs or suppress_space) else (' ' + text)
        bold, underline = bool(seg.get('bold')), bool(seg.get('underline'))
        if runs and runs[-1]['bold'] == bold and runs[-1]['underline'] == underline:
            runs[-1]['text'] += prefixed
        else:
            runs.append({'text': prefixed, 'bold': bold, 'underline': underline})

        prev_no_space_after = bool(seg.get('no_space_after'))

    return paragraphs


# --------------------------------------------------------------------------
# worker ברקע: ממיר+מתמלל כל סגמנט (במקביל, עד 6 בו-זמנית), מרכיב לפסקאות, שומר
# --------------------------------------------------------------------------
def _dictation_worker(app, page_id, segment_files, segment_meta, engine=None):
    """segment_files: נתיבים לקבצי אודיו זמניים, לפי סדר ההקלטה - אך ורק
       עבור פריטי meta מסוג 'audio' (ראו process() למטה); פריטי 'literal'
       (סימני פיסוק שהוכנסו בלחיצת כפתור - ראו PUNCTUATION_BUTTONS בסטודיו)
       לא מקבלים קובץ בכלל, כי הטקסט שלהם כבר ידוע ואין מה לתמלל.
       segment_meta: רשימה לפי סדר ההקלטה המלא (אודיו + literal ביחד) של
       {'type': 'audio'|'literal', 'text'? (ל-literal בלבד),
        'bold','underline','heading','new_paragraph','no_space_before'?,'no_space_after'?}.
       engine: 'gemini' או 'openai' - איזה מנוע תמלול להשתמש בו לסגמנטי האודיו."""
    engine = engine if engine in ENGINES else DEFAULT_DICTATION_ENGINE
    from services.job_tracker import tracked
    with app.app_context(), tracked('dictation', label=f'דף {page_id}'):
        from models import ManuscriptPage
        from concurrent.futures import ThreadPoolExecutor

        page = ManuscriptPage.query.get(page_id)
        if not page:
            return
        try:
            # מיפוי: הקובץ ה-j-י שהועלה שייך לפריט ה-meta ה-i-י שהוא מסוג 'audio',
            # לפי אותו סדר יחסי (ראו process()).
            audio_meta_indices = [i for i, m in enumerate(segment_meta) if m.get('type', 'audio') == 'audio']
            if len(audio_meta_indices) != len(segment_files):
                log.warning(
                    f"dictation worker: audio meta count ({len(audio_meta_indices)}) "
                    f"!= uploaded files ({len(segment_files)}) for page={page_id}"
                )
            meta_idx_to_file_j = {i: j for j, i in enumerate(audio_meta_indices)}

            if engine == 'openai':
                from openai import OpenAI
                client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

                def _process_one(item):
                    j, path = item
                    with open(path, 'rb') as f:
                        raw = f.read()
                    wav_bytes = _webm_to_wav_16k_mono(raw)
                    return j, _transcribe_segment_openai(wav_bytes, client)
            else:
                from google import genai
                from google.genai import types as gtypes
                client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))

                def _process_one(item):
                    j, path = item
                    with open(path, 'rb') as f:
                        raw = f.read()
                    wav_bytes = _webm_to_wav_16k_mono(raw)
                    return j, _transcribe_segment_gemini(wav_bytes, client, gtypes)

            texts_by_file_j = {}
            with ThreadPoolExecutor(max_workers=6) as ex:
                for j, text in ex.map(_process_one, list(enumerate(segment_files))):
                    texts_by_file_j[j] = text

            segments = []
            for i, meta in enumerate(segment_meta):
                if meta.get('type') == 'literal':
                    text = meta.get('text', '')
                else:
                    file_j = meta_idx_to_file_j.get(i)
                    text = texts_by_file_j.get(file_j, '') if file_j is not None else ''
                segments.append(dict(meta, text=text))

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
            log.info(
                f"dictation processed: page={page_id}, engine={engine}, paragraphs={len(content)}, "
                f"segments={len(segment_meta)} (audio={len(segment_files)}, literal={len(segment_meta) - len(segment_files)})"
            )
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
    """בונה את קובץ ה-Word הסופי - אותו דפוס RTL/גופן מוטמע ומוכח בפועל
    כמו services/transcribe.py._build_word_doc (המשמש למסלול התמלול המקצועי),
    כולל התיקון הקריטי ל"סימן הפסקה" עצמו (w:rtl ברמת ה-rPr של pPr, לא רק
    w:bidi) שבלעדיו וורד לפעמים מותיר סימני פיסוק בסוף שורה במקום הלא נכון,
    ועיצוב נוסף (כותרות/פרטי לקוח/פוטר עם מספור עמודים/יישור לשני הצדדים
    בגוף הטקסט) שעושה את המסמך ל"נעים" יותר לקריאה.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
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
        # מסמן גם את "סימן הפסקה" עצמו כ-RTL - קריטי לוורד (בניגוד ל-LibreOffice)
        # כדי שסימני פיסוק בסוף השורה (נקודה, סימן שאלה) יתמקמו נכון ולא "יברחו" לתחילת השורה
        mark_rPr = pPr.find(qn('w:rPr'))
        if mark_rPr is None:
            mark_rPr = OxmlElement('w:rPr')
            pPr.insert_element_before(mark_rPr, 'w:sectPr', 'w:pPrChange')
        mark_rtl = mark_rPr.find(qn('w:rtl'))
        if mark_rtl is None:
            mark_rtl = OxmlElement('w:rtl')
            mark_rPr.insert_element_before(mark_rtl, *_RPR_RTL_SUCCESSORS)
        mark_lang = mark_rPr.find(qn('w:lang'))
        if mark_lang is None:
            mark_lang = OxmlElement('w:lang')
            mark_rPr.append(mark_lang)
        mark_lang.set(qn('w:val'), 'he-IL')
        mark_lang.set(qn('w:bidi'), 'he-IL')

    def set_rtl(paragraph, justify=False):
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if justify else WD_ALIGN_PARAGRAPH.RIGHT
        if not justify:
            # זה התיקון האמיתי לכותרות (ולכל פסקה RIGHT לא-מיושרת) שנראו זזות
            # שמאלה בוורד: בתוך פסקת bidi, וורד קורא jc="right" כ"ימין הלוגי"
            # (לפי כיוון קריאה), שבפועל נופל על שמאל הפיזי של העמוד - זה קורה
            # רק ל-right/left הפיזיים, ולכן פסקאות מיושרות לשני הצדדים
            # (jc="both", justify=True) לא נפגעות מזה בכלל. "start" הוא הערך
            # שוורד בפועל ממפה נכון לימין הפיזי בפסקת RTL.
            pPr = paragraph._p.get_or_add_pPr()
            jc = pPr.find(qn('w:jc'))
            if jc is not None:
                jc.set(qn('w:val'), 'start')
        add_bidi(paragraph)

    def clear_indent(paragraph):
        """מאפס הזחה (w:ind) שמגיעה בירושה מסגנונות ה-Title/Heading המובנים
        של וורד. בלעדי זה, למרות ש-jc מוגדר right, תיבת הטקסט של הכותרת
        עצמה יכולה להיות מוזחת מהשוליים האמיתיים של העמוד - מה שגורם לכותרת
        להיראות "זזה" שמאלה בפועל כשפותחים את הקובץ בוורד."""
        pf = paragraph.paragraph_format
        pf.left_indent = 0
        pf.right_indent = 0
        pf.first_line_indent = 0

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

    def add_bottom_border(paragraph):
        pPr = paragraph._p.get_or_add_pPr()
        pBdr = OxmlElement('w:pBdr')
        bottom = OxmlElement('w:bottom')
        bottom.set(qn('w:val'), 'single')
        bottom.set(qn('w:sz'), '6')
        bottom.set(qn('w:space'), '4')
        bottom.set(qn('w:color'), '999999')
        pBdr.append(bottom)
        pPr.insert_element_before(
            pBdr, 'w:shd', 'w:tabs', 'w:suppressAutoHyphens', 'w:kinsoku', 'w:wordWrap',
            'w:overflowPunct', 'w:topLinePunct', 'w:autoSpaceDE', 'w:autoSpaceDN', 'w:bidi', *_BIDI_SUCCESSORS
        )

    def add_page_number_field(paragraph):
        run = paragraph.add_run()
        fld_begin = OxmlElement('w:fldChar')
        fld_begin.set(qn('w:fldCharType'), 'begin')
        instr = OxmlElement('w:instrText')
        instr.set(qn('xml:space'), 'preserve')
        instr.text = 'PAGE'
        fld_end = OxmlElement('w:fldChar')
        fld_end.set(qn('w:fldCharType'), 'end')
        run._r.append(fld_begin)
        run._r.append(instr)
        run._r.append(fld_end)
        set_hebrew_font(run, size=Pt(10))
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    def add_footer(doc):
        section = doc.sections[0]
        footer = section.footer
        footer_para = footer.paragraphs[0]
        footer_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_bidi(footer_para)
        run = footer_para.add_run('הופק באמצעות מערכת תמלול פון 03-3131795   |   עמוד ')
        set_hebrew_font(run, size=Pt(11))
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        add_page_number_field(footer_para)

    doc = Document()

    # --- הגדרת עברית כברירת מחדל של כל המסמך (סגנון Normal + הכותרות) ---
    doc.core_properties.language = 'he-IL'

    normal_style = doc.styles['Normal']
    normal_style.font.name = FONT_NAME
    normal_pPr = normal_style.element.get_or_add_pPr()
    normal_pPr.insert_element_before(OxmlElement('w:bidi'), *_BIDI_SUCCESSORS)
    normal_rPr = normal_style.element.get_or_add_rPr()
    n_rFonts = normal_rPr.find(qn('w:rFonts'))
    if n_rFonts is None:
        n_rFonts = OxmlElement('w:rFonts')
        normal_rPr.append(n_rFonts)
    n_rFonts.set(qn('w:cs'), FONT_NAME)
    n_rFonts.set(qn('w:ascii'), FONT_NAME)
    n_rFonts.set(qn('w:hAnsi'), FONT_NAME)
    n_rtl = OxmlElement('w:rtl')
    normal_rPr.insert_element_before(n_rtl, *_RPR_RTL_SUCCESSORS)
    n_lang = OxmlElement('w:lang')
    n_lang.set(qn('w:val'), 'he-IL')
    n_lang.set(qn('w:eastAsia'), 'he-IL')
    n_lang.set(qn('w:bidi'), 'he-IL')
    normal_rPr.append(n_lang)

    # אותו דבר גם ברמת הסגנונות "Title" ו-"Heading 1" עצמם - כדי שכותרות
    # יהיו RTL כברירת מחדל של הסגנון ולא יסתמכו רק על עקיפה ידנית לכל פסקה.
    # מאפסים גם הזחה בסגנון עצמו (w:ind) - התבנית המובנית של וורד לפעמים
    # מגדירה שם הזחה שגורמת לכותרת להיראות זזה שמאלה, גם כשה-jc בפועל right.
    for style_name in ('Title', 'Heading 1'):
        try:
            style = doc.styles[style_name]
            style_el = style.element
        except KeyError:
            continue
        style_pPr = style_el.get_or_add_pPr()
        style_bidi = OxmlElement('w:bidi')
        style_pPr.insert_element_before(style_bidi, *_BIDI_SUCCESSORS)
        style.paragraph_format.left_indent = 0
        style.paragraph_format.right_indent = 0
        style.paragraph_format.first_line_indent = 0

    # settings.xml - שפת ברירת מחדל לאיות/תיקון אוטומטי של טקסט חדש שיוקלד
    settings_el = doc.settings.element
    theme_font_lang = settings_el.find(qn('w:themeFontLang'))
    if theme_font_lang is None:
        theme_font_lang = OxmlElement('w:themeFontLang')
        settings_el.append(theme_font_lang)
    theme_font_lang.set(qn('w:bidi'), 'he-IL')

    embed_ttf = OxmlElement('w:embedTrueTypeFonts')
    settings_el.insert_element_before(
        embed_ttf, 'w:embedSystemFonts', 'w:saveSubsetFonts', 'w:saveFormsData', 'w:mirrorMargins',
        'w:alignBordersAndEdges', 'w:bordersDoNotSurroundHeader', 'w:bordersDoNotSurroundFooter',
        'w:gutterAtTop', 'w:hideSpellingErrors', 'w:hideGrammaticalErrors', 'w:activeWritingStyle',
        'w:proofState', 'w:formsDesign', 'w:attachedTemplate', 'w:linkStyles', 'w:themeFontLang'
    )

    section = doc.sections[0]
    sectPr = section._sectPr
    sectPr.append(OxmlElement('w:bidi'))
    pgNumType = OxmlElement('w:pgNumType')
    pgNumType.set(qn('w:fmt'), 'decimal')
    sectPr.append(pgNumType)

    add_footer(doc)

    title = doc.add_heading(f'כתב יד - {original_filename}', 0)
    set_rtl(title)
    clear_indent(title)
    for run in title.runs:
        set_hebrew_font(run)

    if customer_name:
        h_details = doc.add_heading('פרטי לקוח', level=1)
        set_rtl(h_details)
        clear_indent(h_details)
        for run in h_details.runs:
            set_hebrew_font(run)

        try:
            from zoneinfo import ZoneInfo
            now_il = datetime.now(ZoneInfo('Asia/Jerusalem'))
        except Exception:
            now_il = datetime.utcnow()
        info = doc.add_paragraph(f'לקוח: {customer_name}   |   תאריך: {now_il.strftime("%d/%m/%Y %H:%M")}')
        set_rtl(info)
        add_bottom_border(info)
        for run in info.runs:
            set_hebrew_font(run, size=Pt(11))

    h_content = doc.add_heading('תוכן', level=1)
    set_rtl(h_content)
    clear_indent(h_content)
    for run in h_content.runs:
        set_hebrew_font(run)

    if not content:
        empty = doc.add_paragraph('(לא הוקלט תוכן)')
        set_rtl(empty)
        for run in empty.runs:
            set_hebrew_font(run)
    else:
        for para_data in content:
            is_heading = bool(para_data.get('heading'))
            if is_heading:
                p = doc.add_heading('', level=1)
                set_rtl(p)
                clear_indent(p)
            else:
                p = doc.add_paragraph()
                set_rtl(p, justify=True)
            for run_data in para_data.get('runs', []):
                run = p.add_run(run_data.get('text', ''))
                set_hebrew_font(
                    run,
                    size=None if is_heading else Pt(13),
                    bold=bool(run_data.get('bold')) or is_heading,
                    underline=bool(run_data.get('underline')),
                )

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    docx_bytes = buf.read()
    docx_bytes = _embed_font_in_docx(docx_bytes, FONT_NAME, FONT_REGULAR_PATH, FONT_BOLD_PATH)
    return docx_bytes


def _manuscript_char_count(content):
    """סופר תווים (כולל רווחים) מתוך כל ריצות הטקסט בתוכן - אותו חישוב
    בדיוק שמוצג לנציג בסטודיו (updateCharCount ב-dictate_studio.html, שסופר
    textContent מה-DOM), כדי שהמחיר שיחושב בפועל בעת שליחה יתאים למה שהנציג
    ראה על המסך."""
    total = 0
    for para in (content or []):
        for run in para.get('runs', []):
            total += len(run.get('text', '') or '')
    return total


def _manuscript_pricing():
    """קורא את הגדרות התמחור (routes/admin.py get_setting) - הן גודל יחידת
    התווים והן המחיר ליחידה ניתנים לקביעה בהגדרות (בניגוד ל-OCR, ששם רק
    המחיר מוגדר וגודל היחידה קבוע ל-1000)."""
    from routes.admin import get_setting
    try:
        unit_size = int(float(get_setting('manuscript_char_unit_size', '1000') or '1000'))
    except (TypeError, ValueError):
        unit_size = 1000
    if unit_size <= 0:
        unit_size = 1000
    try:
        price_per_unit = float(get_setting('price_per_manuscript_char_unit', '0.10') or '0.10')
    except (TypeError, ValueError):
        price_per_unit = 0.10
    return unit_size, price_per_unit


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


def _manuscript_file_bytes(page):
    """מחזיר את בייטי הקובץ - קודם כל מ-file_data ב-DB (מקור האמת, שורד
    דיפלויים/הפעלה מחדש של הקונטיינר), ורק אם זה ריק (דפים ישנים שנוצרו
    לפני ההוספה של file_data) מנסה ליפול חזרה לדיסק המקומי - שם, ב-Railway,
    יכול כבר לא להיות קיים בגלל דיפלוי שקרה בינתיים."""
    if page.file_data:
        return page.file_data
    if page.file_path and os.path.exists(page.file_path):
        try:
            with open(page.file_path, 'rb') as f:
                return f.read()
        except Exception as e:
            log.error(f"manuscript file read error from disk (page={page.id}): {e}")
    return None


def _image_to_pdf_bytes(raw):
    """ממיר בייטי תמונה (jpg/png/וכו') לקובץ PDF חד-עמודי (Pillow). PDF פשוט
    לא נשמר עם ערוץ שקיפות (alpha) - אם קיים, ממזגים על רקע לבן קודם."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    out = io.BytesIO()
    img.save(out, format='PDF')
    return out.getvalue()


def _manuscript_data_uri(page):
    """מקודד את קובץ כתב-היד כ-data URI (base64) מוטמע ישירות ב-HTML - לא
    כ-src לכתובת נפרדת - כדי שלא תהיה בקשת רשת נפרדת שמסנן תוכן כמו נטפרי
    יכול לעכב. בפועל זה לא הספיק: מתברר שנטפרי חוסם גם תמונות שמגיעות כ-data
    URI מוטמע (כנראה לפי ניתוח התוכן/mime של האלמנט עצמו, לא רק לפי בקשת
    הרשת) - אז כל תמונה מומרת כאן ל-PDF חד-עמודי (Pillow) לפני ההטמעה,
    ומוצגת ב-iframe בדיוק כמו PDF "אמיתי" שהגיע במייל. PDF לא נחסם אצל נטפרי
    באותה צורה שתמונה נחסמת.
    מחזיר (data_uri, is_pdf) - is_pdf קובע ב-template אם להציג ב-<iframe>
    (PDF) או ב-<img> (fallback, רק אם המרה ל-PDF נכשלה מסיבה כלשהי)."""
    raw = _manuscript_file_bytes(page)
    if not raw:
        return None, False
    mime, _ = mimetypes.guess_type(page.original_filename or page.file_path or '')
    if not mime:
        ext = os.path.splitext(page.original_filename or page.file_path or '')[1].lower()
        mime = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.gif': 'image/gif', '.webp': 'image/webp', '.pdf': 'application/pdf',
        }.get(ext, 'application/octet-stream')

    if mime != 'application/pdf':
        try:
            raw = _image_to_pdf_bytes(raw)
            mime = 'application/pdf'
        except Exception as e:
            log.error(f"manuscript image->PDF conversion error (page={page.id}): {e}")
            # ממשיכים עם התמונה המקורית - עדיף תצוגה שעלולה להיחסם מאשר כלום

    b64 = base64.b64encode(raw).decode('ascii')
    return f'data:{mime};base64,{b64}', (mime == 'application/pdf')


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
    manuscript_data_uri, manuscript_is_pdf = _manuscript_data_uri(page)
    return render_template(
        'admin/dictate_studio.html',
        page=page,
        default_engine=DEFAULT_DICTATION_ENGINE,
        manuscript_data_uri=manuscript_data_uri,
        manuscript_is_pdf=manuscript_is_pdf,
    )


@dictate_bp.route('/<int:page_id>/file')
@login_required
def page_file(page_id):
    """נשאר לתאימות לאחור (לא בשימוש יותר בסטודיו עצמו - ראה
    manuscript_data_uri/studio() למעלה) - קורא עכשיו גם מ-file_data ב-DB,
    לא רק מהדיסק המקומי (שיכול להיות ריק אחרי דיפלוי ב-Railway)."""
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    raw = _manuscript_file_bytes(page)
    if not raw:
        return "הקובץ אינו זמין", 404
    mime, _ = mimetypes.guess_type(page.original_filename or '')
    return send_file(
        io.BytesIO(raw), as_attachment=False, download_name=page.original_filename,
        mimetype=mime or 'application/octet-stream',
    )


@dictate_bp.route('/<int:page_id>/process', methods=['POST'])
@login_required
def process(page_id):
    # בניגוד לשיחות טלפון ותמונות OCR (שמגיעות אוטומטית ואפשר לדחות אותן
    # לתור), כאן זו פעולה שהמנהל יוזם ממש עכשיו בסטודיו - הוא מקליט את
    # עצמו ולוחץ "עבד". אין טעם "לתייק לתור" הקלטה שעוד לא קיימת; במקום זה
    # פשוט חוסמים את ההתחלה כדי לא להתחיל עבודה שעלולה להיקטע בדפלוי תוך
    # כדי מצב תחזוקה, ומבקשים מהמנהל להמתין רגע ולנסות שוב.
    from routes.admin import get_setting
    if get_setting('maintenance_mode', '0') == '1':
        return jsonify({'error': 'מצב תחזוקה פעיל כרגע - המתן מספר דקות ונסה שוב'}), 503

    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)

    audio_files = request.files.getlist('segments')
    meta_raw = request.form.get('meta', '[]')

    try:
        segment_meta = json.loads(meta_raw)
    except Exception:
        segment_meta = []

    if not segment_meta:
        return jsonify({'error': 'לא התקבלה הקלטה'}), 400

    engine = request.form.get('engine') or DEFAULT_DICTATION_ENGINE
    if engine not in ENGINES:
        engine = DEFAULT_DICTATION_ENGINE

    # כל פריט meta מסוג 'audio' (או בלי type בכלל - תאימות לאחור) אמור להגיע
    # עם קובץ תואם ב-audio_files, לפי אותו סדר יחסי. פריטי 'literal' (סימני
    # פיסוק שהוכנסו בלחיצת כפתור) לא מגיעים עם קובץ בכלל - זה תקין ומכוון,
    # לא "חוסר". _dictation_worker מתמודד בעצמו עם אי-התאמה (טקסט ריק לאותו
    # סגמנט) בלי לקרוס, אז כאן רק רושמים אזהרה ל-log.
    expected_audio_count = sum(1 for m in segment_meta if m.get('type', 'audio') == 'audio')
    if expected_audio_count != len(audio_files):
        log.warning(
            f"process: expected {expected_audio_count} audio files per meta "
            f"but got {len(audio_files)} (page={page_id})"
        )

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


@dictate_bp.route('/<int:page_id>/delete', methods=['POST'])
@login_required
def delete(page_id):
    """מחיקה לצמיתות של דף כתב-יד מהתור/מהרשימה - כולל ניקוי הקובץ מהדיסק
    אם קיים שם (file_data ב-DB נמחק אוטומטית עם השורה עצמה)."""
    from flask import flash, redirect, url_for
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    filename = page.original_filename
    try:
        if page.file_path and os.path.exists(page.file_path):
            os.remove(page.file_path)
    except Exception as e:
        log.warning(f"manuscript delete: file cleanup failed (page={page_id}): {e}")
    db.session.delete(page)
    db.session.commit()
    flash(f'הדף "{filename}" נמחק בהצלחה')
    return redirect(url_for('dictate.queue'))


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
    from models import ManuscriptPage, Transaction
    page = ManuscriptPage.query.get_or_404(page_id)
    if not page.content:
        return jsonify({'error': 'אין תוכן לשליחה'}), 400

    to_email = (request.form.get('to_email') or '').strip() or (page.customer.email or '').strip()
    if not to_email:
        return jsonify({'error': 'אין כתובת מייל ליעד - הזינו כתובת'}), 400

    # התשלום יורד מהלקוח רק כאן - באישור הסופי ובשליחה בפועל, ולא בשום שלב
    # מוקדם יותר (הקלטה/תמלול/עריכה). מחושב מחדש בכל שליחה, כדי לשקף גם
    # תיקונים ידניים שנעשו בתצוגה המקדימה (saveEdits נקרא תמיד לפני שליחה).
    customer = page.customer
    unit_size, price_per_unit = _manuscript_pricing()
    char_count = _manuscript_char_count(page.content)
    units = math.ceil(char_count / unit_size) if char_count > 0 else 0
    cost = round(units * price_per_unit, 2)

    if cost > 0:
        if not customer:
            return jsonify({'error': 'לא נמצא לקוח משויך לדף זה - לא ניתן לחייב'}), 400
        if customer.balance < cost:
            log.info(
                f"manuscript send: יתרה לא מספיקה עבור customer {customer.id} "
                f"(צריך {cost}, יש {customer.balance}), page={page_id}"
            )
            return jsonify({
                'error': f'אין מספיק יתרה ללקוח לשליחה (עלות: ₪{cost:.2f}, יתרה נוכחית: ₪{customer.balance:.2f}) - '
                         f'יש לטעון יתרה ללקוח ולנסות לשלוח שוב',
                'insufficient_balance': True,
                'cost': cost,
                'balance': customer.balance,
            }), 402

    try:
        _send_manuscript_email(to_email, page.customer.name, page.original_filename, page.content)
        if cost > 0 and customer:
            customer.balance -= cost
            db.session.add(Transaction(
                customer_id=customer.id,
                amount=-cost,
                type='manuscript_dictation',
                description=f'הקראת כתב יד - {page.original_filename}',
            ))
        page.char_count = char_count
        page.cost = cost
        page.sent_at = datetime.utcnow()
        page.sent_to = to_email
        page.status = 'done'
        db.session.commit()
        return jsonify({'status': 'sent', 'to': to_email, 'cost': cost, 'char_count': char_count})
    except Exception as e:
        db.session.rollback()
        log.error(f"manuscript send error (page={page_id}): {e}", exc_info=True)
        return jsonify({'error': f'שליחה נכשלה: {e}'}), 500
