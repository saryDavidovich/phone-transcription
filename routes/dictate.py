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
  * gemini - gemini-3.5-flash, אותו מודל ואותו קליינט בדיוק
    כמו _gemini_from_url ב-services/transcribe.py, שבו כבר משתמשים בפועל
    לכל התמלולים במסלול המקצועי.
  * openai (ברירת מחדל) - gpt-transcribe (הדור החדש שהחליף את gpt-4o-transcribe, זול
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
from html import escape as _html_escape
import math
import time
import uuid
import random
import base64
import mimetypes
import logging
import threading
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, send_file, current_app, abort, url_for, flash, redirect
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


def dictate_pending_counts():
    """ספירת העבודות שממתינות לטיפול הצוות, מחולקת לשלושת הסוגים:
    open = כתבי-יד בתור, proof = סבבי הגהה ממתינים, fax = פקסים ממתינים
    לשיוך. נספר רק מה שבאמת ממתין (לא "הושלמו"). מחושב פעם אחת לבקשה
    (flask.g) - גם התפריט הצדדי וגם לשוניות התור משתמשים באותה ספירה."""
    from flask import g
    cached = getattr(g, '_dictate_pending_counts', None)
    if cached is not None:
        return cached
    from models import ManuscriptPage, ProofingRound, IncomingFax
    try:
        counts = {
            'open': ManuscriptPage.query.filter(ManuscriptPage.status.in_(OPEN_STATUSES)).count(),
            'proof': ProofingRound.query.filter_by(status='pending').count(),
            'fax': IncomingFax.query.filter_by(status='pending').count(),
        }
        # דפים שהושהו בגלל יתרה לא מספיקה - מחכים ללקוח, לא לצוות: נספרים
        # בנפרד ולא נכנסים ל-total (שמראה את מה שהצוות צריך לטפל בו).
        counts['unpaid'] = ManuscriptPage.query.filter_by(status='pending_payment').count()
    except Exception:
        db.session.rollback()
        counts = {'open': 0, 'proof': 0, 'fax': 0, 'unpaid': 0}
    counts['total'] = counts['open'] + counts['proof'] + counts['fax']
    g._dictate_pending_counts = counts
    return counts


@dictate_bp.app_context_processor
def _inject_dictate_pending():
    # מוזרק כפונקציה (לא כערך מחושב) בכוונה: כך השאילתות רצות רק בתבנית
    # הצוות שקוראת לה (admin/base.html), ולא בכל תבנית באתר (למשל פורטל
    # המוסדות) - האתר צריך להישאר מהיר.
    return {'dictate_pending': dictate_pending_counts}

ENGINES = ('gemini', 'openai')
DEFAULT_DICTATION_ENGINE = os.environ.get('DEFAULT_DICTATION_ENGINE', 'openai')
if DEFAULT_DICTATION_ENGINE not in ENGINES:
    DEFAULT_DICTATION_ENGINE = 'openai'


# --------------------------------------------------------------------------
# פרומפט תמלול לכל סגמנט - אותה רוח בדיוק כמו הפרומפט הקיים ב-
# services/transcribe.py._gemini_from_url (דיוק מלא, בלי סיכום, מינוח תורני/ארמית)
# --------------------------------------------------------------------------
_SEGMENT_PROMPT = """תמלל את קובץ השמע הקצר הזה בדיוק, מילה במילה, בעברית בלבד.
זהו קטע קצר מתוך הקראה בקול של גיליון כתב-יד תורני - שים לב במיוחד למינוח
תורני נכון, ארמית, ראשי תיבות וציטוטים כפי שנאמרו.
אל תתקן, אל תסכם, אל תוסיף הערות - החזר רק את הטקסט המתומלל עצמו.
אם הקטע שקט או לא מכיל דיבור - החזר מחרוזת ריקה."""


# סגמנטים במצב "איות" / "מספר" (ראה services/hebrew_refs.py): הדובר מאיית
# שמות אותיות או אומר מספר, והקוד הופך את זה לראשי תיבות/גימטריה. המנוע
# נדרש רק לזהות אוצר מילים סגור - אסור לו "לפרש" או להשלים.
_SPELL_PROMPT = """הדובר מאיית אותיות עבריות בשמותיהן (אלף, בית, גימל, דלת, הא, וו, זין, חית, טית, יוד, כף, למד, מם, נון, סמך, עין, פא, צדי, קוף, ריש, שין, תו).
החזר אך ורק את שמות האותיות כפי שנאמרו, מופרדים ברווח, בסדר שנאמרו.
אם נאמרה המילה "רווח", "גרש" או "גרשיים" - החזר אותה כמו שהיא.
אל תצרף אותיות למילים, אל תפרש ואל תוסיף שום טקסט אחר."""
_SPELL_PROMPT_OPENAI = "איות אותיות בשמותיהן: אלף בית גימל דלת הא וו זין חית טית יוד כף למד מם נון סמך עין פא צדי קוף ריש שין תו"
_NUMBER_PROMPT = """הדובר אומר מספר בעברית (למשל "מאתיים חמישים ושלוש" או "שלושים ושבע").
החזר אך ורק את המספר בספרות (למשל 253). אם נאמרו כמה מספרים, הפרד ביניהם במילה "רווח".
אל תוסיף שום טקסט אחר."""
_NUMBER_PROMPT_OPENAI = "מספרים בעברית: מאה חמישים וארבע, מאתיים חמישים ושלוש, שלושים ושבע"
_SEGMENT_MODES = ('spell', 'number')


def _apply_segment_mode(text, mode):
    """איות/מספר => המרה דטרמיניסטית (services/hebrew_refs.py). שגיאה לא צפויה
    בהמרה לא מפילה את כל ההקלטה - נשאר הטקסט הגולמי שהמנוע החזיר."""
    try:
        from services.hebrew_refs import apply_mode
        return apply_mode(text, mode or '')   # דיבור רגיל: רק החזרת גרשיים לראשי תיבות מוכרים (רשי -> רש״י)
    except Exception as e:
        log.warning(f"segment mode {mode!r} post-process failed: {e}")
        return text


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


def _transcribe_segment_gemini(wav_bytes, client, gtypes, prompt=None):
    """מתמלל סגמנט קצר בודד עם Gemini. אותו דפוס ניסיונות-חוזרים כמו
    _gemini_from_url ב-services/transcribe.py, רק עם פחות ניסיונות/המתנה
    כי כאן זה סגמנט קצר בודד ולא קריאה שלמה."""
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=[
                    prompt or _SEGMENT_PROMPT,
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


def _transcribe_segment_openai(wav_bytes, client, prompt=None):
    """מתמלל סגמנט קצר בודד עם gpt-transcribe. אותו דפוס ניסיונות-חוזרים
    כמו _transcribe_segment_gemini, כדי ששני המנועים יתנהגו זהה מבחוץ."""
    for attempt in range(3):
        try:
            result = client.audio.transcriptions.create(
                model='gpt-transcribe',
                file=('segment.wav', io.BytesIO(wav_bytes), 'audio/wav'),
                language='he',
                prompt=prompt or _OPENAI_SEGMENT_PROMPT,
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
            # מצב לכל קובץ אודיו: '' (דיבור רגיל), 'spell' (איות אותיות) או 'number' (מספר)
            file_mode = {}
            for j, i in enumerate(audio_meta_indices):
                m = segment_meta[i].get('mode')
                file_mode[j] = m if m in _SEGMENT_MODES else ''

            if engine == 'openai':
                from openai import OpenAI
                client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

                def _process_one(item):
                    j, path = item
                    mode = file_mode.get(j, '')
                    try:
                        with open(path, 'rb') as f:
                            raw = f.read()
                        wav_bytes = _webm_to_wav_16k_mono(raw)
                        p = {'spell': _SPELL_PROMPT_OPENAI, 'number': _NUMBER_PROMPT_OPENAI}.get(mode)
                        return j, _apply_segment_mode(_transcribe_segment_openai(wav_bytes, client, p), mode)
                    except Exception as e:
                        # סגמנט בודד פגום/קצר מדי (למשל חיתוך בלחיצה כפולה) לא מפיל את כל ההקלטה
                        log.warning(f"dictation segment {j} skipped ({e})")
                        return j, ''
            else:
                from google import genai
                from google.genai import types as gtypes
                client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))

                def _process_one(item):
                    j, path = item
                    mode = file_mode.get(j, '')
                    try:
                        with open(path, 'rb') as f:
                            raw = f.read()
                        wav_bytes = _webm_to_wav_16k_mono(raw)
                        p = {'spell': _SPELL_PROMPT, 'number': _NUMBER_PROMPT}.get(mode)
                        return j, _apply_segment_mode(_transcribe_segment_gemini(wav_bytes, client, gtypes, p), mode)
                    except Exception as e:
                        log.warning(f"dictation segment {j} skipped ({e})")
                        return j, ''

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
PAREN_TEXT_PT = 9     # גודל כתב לטקסט בסוגריים. בוורד גוף הטקסט העברי מוצג בפועל ב-11 (w:szCs של ברירת המחדל במסמך)


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
    try:
        from services.hebrew_refs import paren_mask
    except ImportError:   # קובץ ישן בלי הפונקציה - ממשיכים בלי הקטנת סוגריים במקום להיכשל
        paren_mask = lambda t: [False] * len(t or '')
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

    def set_hebrew_font(run, size=None, bold=False, underline=False, cs_size=None):
        run.font.name = FONT_NAME
        run.bold = bold
        run.underline = underline
        if size:
            run.font.size = size
        rPr = run._r.get_or_add_rPr()
        # בטקסט עברי (ריצה עם w:rtl) וורד קורא את הגודל מ-w:szCs ("complex script"),
        # לא מ-w:sz - בלי זה ה-size לא משפיע בוורד בכלל והטקסט מקבל את ברירת המחדל
        # של המסמך. cs_size מוגדר רק היכן שרוצים גודל שונה בפועל (טקסט בסוגריים).
        if cs_size is not None:
            sz_el = rPr.find(qn('w:sz'))
            szcs = rPr.find(qn('w:szCs'))
            if szcs is None:
                szcs = OxmlElement('w:szCs')
                if sz_el is not None:
                    sz_el.addnext(szcs)
                else:
                    rPr.append(szcs)
            szcs.set(qn('w:val'), str(int(round(cs_size.pt * 2))))
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
            # גודל כתב: טקסט בסוגריים עגולים תואמים מעט קטן יותר (ראה paren_mask;
            # "(" או ")" בלי בן-זוג נשארים בכתב רגיל). המסכה מחושבת על טקסט
            # הפסקה כולו, כי סוגריים יכולים לחצות ריצות עיצוב (מודגש/קו תחתון).
            runs_data = para_data.get('runs', [])
            full_text = ''.join((r.get('text') or '') for r in runs_data)
            mask = paren_mask(full_text) if not is_heading else None
            offset = 0
            for run_data in runs_data:
                text = run_data.get('text') or ''
                pieces = []   # (טקסט, בסוגריים?)
                if mask is None or not text:
                    pieces.append((text, False))
                else:
                    start = 0
                    for k in range(1, len(text) + 1):
                        if k == len(text) or mask[offset + k] != mask[offset + start]:
                            pieces.append((text[start:k], mask[offset + start]))
                            start = k
                offset += len(text)
                for piece_text, small in pieces:
                    run = p.add_run(piece_text)
                    set_hebrew_font(
                        run,
                        size=None if is_heading else Pt(PAREN_TEXT_PT if small else 13),
                        bold=bool(run_data.get('bold')) or is_heading,
                        underline=bool(run_data.get('underline')),
                        cs_size=Pt(PAREN_TEXT_PT) if small else None,
                    )
                    if run_data.get('italic'):
                        run.italic = True

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


def _manuscript_proofing_price():
    """מחיר הגהה לדקת עבודה (לא מחיר קבוע לסבב, ולא לפי תווים כמו תמחור
    ההקראה עצמה) - ראה ההגדרה 'price_manuscript_proofing' בעמוד ההגדרות,
    ו-ProofingRound.timer_accumulated_seconds/_proofing_minutes_billed למטה
    לחישוב בפועל."""
    from routes.admin import get_setting
    try:
        return float(get_setting('price_manuscript_proofing', '5.00') or '5.00')
    except (TypeError, ValueError):
        return 5.00


def _proofing_minutes_billed(total_seconds):
    """מעגל כלפי מעלה לדקה שלמה - כל חלק של דקה (אפילו שנייה אחת) מחויב
    כדקה מלאה, בדיוק כמו חיוב טלפון סטנדרטי. 0 שניות = 0 דקות (לא מחויב)."""
    if not total_seconds or total_seconds <= 0:
        return 0
    return math.ceil(total_seconds / 60.0)


def _proofing_timer_total_seconds(round_):
    """סך זמן העבודה שנצבר על הסבב עד כה, כולל המקטע הרץ הנוכחי אם השעון
    פעיל ברגע זה (לא רק המקטעים שכבר נעצרו ונצברו ל-timer_accumulated_seconds)."""
    total = round_.timer_accumulated_seconds or 0.0
    if round_.timer_started_at is not None:
        total += (datetime.utcnow() - round_.timer_started_at).total_seconds()
    return total


def _proofing_timer_state(round_):
    """מצב השעון + האומדן הכספי הנוכחי, לשימוש גם ב-JSON (routes) וגם
    בטעינה הראשונית של proof_studio.html."""
    total = _proofing_timer_total_seconds(round_)
    minutes = _proofing_minutes_billed(total)
    price_per_minute = _manuscript_proofing_price()
    return {
        'running': round_.timer_started_at is not None,
        'accumulated_seconds': round_.timer_accumulated_seconds or 0.0,
        'total_seconds': total,
        'billed_minutes': minutes,
        'price_per_minute': price_per_minute,
        'estimated_cost': round(minutes * price_per_minute, 2),
    }


def _content_to_plain_preview(content):
    """טקסט פשוט (בלי עיצוב) לתצוגה מקדימה בגוף המייל."""
    lines = []
    for para in (content or []):
        text = ''.join(r.get('text', '') for r in para.get('runs', [])).strip()
        if para.get('heading'):
            text = f'* {text} *'
        lines.append(text)
    return '\n\n'.join(lines)


PROOFING_NUMBER_OFFSET = 1000  # ראה _proofing_mailto_link / email_inbound._parse_proofing_subject
PROOFING_SUBJECT_MARKER = 'הגהה'  # ראה גם routes/email_inbound.py._is_proofing_reply - אותו קידומת בדיוק


def _proofing_mailto_link(phone, page_id):
    """קישור mailto מוכן שפותח טיוטת מייל חדשה עם נושא בפורמט שההוק ב-
    routes/email_inbound.py יודע לזהות ולשייך בדיוק לדף הזה - ראה
    _is_proofing_reply/_parse_proofing_subject שם. mailto לא יכול לצרף קובץ
    אוטומטית - הלקוח מצרף בעצמו את קובץ ה-Word המתוקן."""
    from urllib.parse import quote
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
    # מספר הדף מוצג ללקוח עם היסט של 1000 (תמיד 4 ספרות ומעלה, נראה מקצועי) -
    # routes/email_inbound.py._parse_proofing_subject מחזיר אותו לערך האמיתי.
    subject = f'{PROOFING_SUBJECT_MARKER} {phone} {page_id + PROOFING_NUMBER_OFFSET}'
    body = (
        'שלום, מצורף קובץ PDF או תמונה עם תיקוני ההגהה שביצעתי - נא לעדכן בהתאם. תודה.'
    )
    return f"mailto:{TRANSCRIBE_INBOUND_EMAIL}?subject={quote(subject)}&body={quote(body)}"


def _send_manuscript_email(to_email, customer_name, customer_phone, page_id, original_filename, content):
    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email

    docx_bytes = _build_manuscript_docx(customer_name, original_filename, content)
    docx_b64 = base64.b64encode(docx_bytes).decode('utf-8')
    preview = _content_to_plain_preview(content)
    proofing_link = _proofing_mailto_link(customer_phone, page_id)
    proofing_price = _manuscript_proofing_price()

    # הערה: בכוונה **אין** כאן יותר אזכור של אפשרות פקס/קוד אישי בגוף המייל.
    # מי ששולח/מקבל במייל לא אמור לקבל את הקוד האישי שלו במייל בשום מקרה -
    # הקוד נמסר אך ורק בטלפון (תפריט ראשי → שלוחה 6 → הקש 2, ראה
    # routes/api.py get_customer_fax_code ו-phone-transcription-ivr/ivr.js
    # handleFaxCode). אוכלוסיית משתמשי הפקס היא במפורש מי שאין לו מייל בכלל,
    # ולכן אין טעם/צורך להציע את הערוץ הזה בתוך מייל.

    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">כתב יד - {original_filename}</h2>
<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✍️ טקסט</h3>
<div style="line-height:1.8;white-space:pre-wrap;text-align:right;direction:rtl">{preview}</div>
</div>
<div style="background:#eff6ff;border-right:4px solid #2563eb;padding:16px;margin:16px 0;border-radius:8px">
<p style="margin:0 0 12px;line-height:1.8">ניתן לשלוח קובץ PDF או תמונה להגהה אנושית <a href="{proofing_link}" style="color:#1d4ed8;font-weight:700">בקישור זה</a> במחיר <b>₪{proofing_price:.2f}</b> לכל דקה. את ההגהות יש לכתוב לפי ההוראות המצורפות:</p>
<p style="text-align:center;margin:0 0 14px"><a href="{proofing_link}" style="background:#2563eb;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:700;display:inline-block">✏️ שליחת קובץ להגהה</a></p>
<div style="background:#fff;border:1px solid #bfdbfe;border-radius:8px;padding:14px;font-size:14px;line-height:1.8;text-align:right">
<div style="font-weight:700;font-size:15px;margin-bottom:8px;color:#1e3a8a">כללים לשליחת הגהות</div>
<p style="margin:0 0 8px"><b>1. לסמן בראש שורה קו –</b><br>בכל מקום שישנו תיקון באותה שורה, לציין עם קו בתחילת השורה מימין. ואם ישנם כמה תיקונים לעשות כמה קווים, שניים או שלושה. (במקרים שאין קו, אין הרבה סיכוי שהמגיה ישים לב שהוספתם נקודה בשורה זו...).</p>
<p style="margin:0 0 8px"><b>2. מחיקה באמצעות קו חוצה ולא טשטוש</b><br>הקלדן שמכניס את התיקונים מזין לתוכנת חיפוש את המילה השגויה, ומקליד במקומה את המילה הנכונה. ואם עברתם עם עט על כל המילה השגויה כך שאין סיכוי שהקלדן ינחש מה יש מתחת למעטה השחור אז הוא יצטרך "לחפש" בעצמו למה התכוונתם, וזה מכפיל את זמן התיקונים. לכן פשוט תחצו את <s>המילה</s> בקו שתישאר קריאה ויובן שכאן יש שגיאה.</p>
<p style="margin:0 0 8px"><b>3. הוספת סימונים כגון פסיקים ונקודות</b><br>במקום שהוספתם פסיק או נקודה, חשוב לציין בעזרת עיגול או חץ קטנטן כדי שהקלדן ישים לב למיקום המדוייק. ובמקום שהחלפתם את הנקודה למשל בפסיק, אז לסמן בעיגול את הפסיק ולציין לידו נקודה. כנ"ל לגבי גרשיים / סוגריים / מקף / וכו'.</p>
<p style="margin:0 0 8px"><b>4. סימנים ברורים לגבי פעולת התיקון</b><br>במקרה וצריך להדגיש, להוסיף רווח, או לרדת שורה, לחבר פיסקאות, יש לציין בבירור את המילים בתוספת סימון המקום, כמו קו תחתון ולכתוב "הדגשה", ואם זה רווח, אז לעשות סוג של קו בין המילים ולציין "רווח/להסיר רווח / קטע חדש / וכו'"</p>
<p style="margin:0 0 8px"><b>5. לא להוסיף יותר מידי מילים על גבי הטקסט</b><br>במקרה וצריך להוסיף מילים או אותיות, אז לכתוב בכתב ברור מעל המקום הצריך תיקון ולסמן בחץ קטן היכן להכניס את הטקסט, וזה רק במקרה שישנם כמה מילים, אם זו פיסקא שלימה, אז לעשות סימון כגון כוכבית וכדו' למיקום בצידי הדף (בצורה שלא ייקטע בסריקה) ושם לכתוב בצורה קריאה ככל הניתן.</p>
<p style="margin:0"><b>6. במקרה שהתיקון לא ברור בוודאות</b><br>יש לוודא שהתיקון מובן דיו, שהקלדן ישים לב שהוחלף כאן ה-י' ב-ו' ועדיף גם לכתוב בראש המילה השגויה את המילה המתוקנת, ולא רק לטשטש את האות השגויה ולסמן את האות הנכונה. כגון: "כתובת", אז יש לחצות את המילה בקו, ולכתוב מעליה "כתובות". ותמיד במקרים של שינויים גדולים, כגון החלפת פיסקאות וכדו', יש לכתוב הוראות מפורטות וברורות, ולא להשאיר מקום לניחוש או ראש גדול...</p>
</div>
<p style="margin:12px 0 0;font-size:12px;color:#6b7280;text-align:center">הקישור פותח טיוטת מייל מוכנה - רק צריך לצרף את קובץ ה-PDF או התמונה של הדף עם ההגהות, ולשלוח</p>
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


def _send_manuscript_fax(record, to_number, docx_bytes, filename_hint):
    """שולח כתב-יד/הגהה מוכנים בפקס ללקוח שאין לו כתובת מייל (ראה send()/
    proof_complete() למטה) - אותה אוכלוסיית "אנשי הפקס" בדיוק (ראה
    _send_manuscript_email למעלה: "אין להם מייל בכלל"), שעד עכשיו פשוט לא
    היה אפשר למסור להם את הכתב-יד המוכן כי send() דרש כתובת מייל בכל מקרה.
    ממיר את אותו docx שהיה נשלח במייל ל-PDF (LibreOffice, ראה
    services/transcribe.convert_docx_bytes_to_pdf_bytes) ושולח דרך ה-API
    של ימות המשיח (services/transcribe.send_pdf_fax - אותה תשתית פקס-יוצא
    שכבר עובדת עבור תמלולי שיחה, ראה services/transcribe._send_fax).
    record הוא ManuscriptPage או ProofingRound - לשניהם בדיוק אותם שדות
    מעקב (fax_campaign_id/fax_status/fax_status_note). מעלה חריגה בכישלון
    (כמו _send_manuscript_email - sg.send שמעלה חריגה על כישלון) כדי
    שהקוד הקורא (try/except קיים) יעשה rollback ויחזיר שגיאה, בלי לחייב
    את הלקוח על שליחה שלא הצליחה."""
    from services.transcribe import convert_docx_bytes_to_pdf_bytes, send_pdf_fax
    pdf_bytes = convert_docx_bytes_to_pdf_bytes(docx_bytes)
    if not pdf_bytes:
        raise RuntimeError('המרת הקובץ ל-PDF לשליחת פקס נכשלה (בדיקת LibreOffice בשרת) - ראה לוגים')
    result = send_pdf_fax(to_number, pdf_bytes, filename_hint=filename_hint)
    if not result['ok']:
        raise RuntimeError(f'שליחת הפקס נכשלה: {result["error"] or "שגיאה לא ידועה"}')
    record.fax_campaign_id = result['campaign_id']
    record.fax_status = 'sent'
    record.fax_status_note = None


def _send_proofed_manuscript_email(to_email, customer_name, original_filename, final_docx_bytes):
    """שולח ללקוח בחזרה את קובץ ה-Word הסופי אחרי שהנציג סיים לעבד את ההגהה -
    נקרא (אופציונלית) מתוך proof_complete."""
    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email

    docx_b64 = base64.b64encode(final_docx_bytes).decode('utf-8')
    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">כתב יד מתוקן - {original_filename}</h2>
<p style="line-height:1.8">מצורף קובץ ה-Word המעודכן, לאחר עדכון תיקוני ההגהה שנשלחו. תודה!</p>
</div>'''

    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    safe_name = os.path.splitext(original_filename)[0][:40] if original_filename else 'כתב_יד'
    message = Mail(
        from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
        to_emails=to_email,
        subject=f'כתב יד מתוקן - {original_filename}',
        html_content=html,
    )
    message.attachment = Attachment(
        FileContent(docx_b64),
        FileName(f'כתב_יד_מתוקן_{safe_name}.docx'),
        FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        Disposition('attachment'),
    )
    sg.send(message)


def _docx_paragraphs_html(docx_bytes):
    """ממיר bytes של קובץ Word לרשימת HTML של פסקאות (טקסט + מודגש/קו תחתון
    בסיסי בכל run) לתצוגה מקדימה בלבד במסך ההגהה - באותו סגנון עיצוב בדיוק
    (ds-para/ds-run) כמו תצוגת הסקירה הרגילה ב-dictate_studio.html, כדי
    שהמסכים ייראו עקביים. זו תצוגה בלבד - המקור האמיתי לעריכה תמיד הוא
    קובץ ה-Word עצמו (נפתח/מורד בנפרד), לא ה-HTML הזה."""
    from docx import Document
    try:
        doc = Document(io.BytesIO(docx_bytes))
    except Exception as e:
        log.error(f"docx preview parse error: {e}")
        return '<div class="ds-state ds-error">⚠ לא ניתן להציג תצוגה מקדימה של הקובץ - יש להוריד ולפתוח בוורד</div>'

    parts = []
    for para in doc.paragraphs:
        text = ''.join(run.text for run in para.runs) or para.text
        if not text.strip():
            continue
        style_name = (para.style.name if para.style else '') or ''
        is_heading = 'Heading' in style_name or 'Title' in style_name
        runs_html = []
        for run in para.runs:
            if not run.text:
                continue
            classes = ['ds-run']
            if run.bold:
                classes.append('bold')
            if run.underline:
                classes.append('underline')
            if run.italic:
                classes.append('italic')
            runs_html.append(f'<span class="{" ".join(classes)}">{_html_escape(run.text)}</span>')
        inner = ''.join(runs_html) or _html_escape(text)
        parts.append(f'<div class="ds-para{" heading" if is_heading else ""}">{inner}</div>')

    if not parts:
        return '<div class="ds-state" style="color:var(--text3)">(הקובץ ריק)</div>'
    return ''.join(parts)


# ==========================================================================
# Routes
# ==========================================================================

def _defer_manuscript_page_blobs(query, ManuscriptPage):
    """הרשימות (queue/fax_assign) מציגות רק שם קובץ/סטטוס/תאריך - לעולם לא
    את תוכן הקובץ עצמו - אבל בלי defer() מפורש, SQLAlchemy עושה SELECT * כברירת
    מחדל ומביא גם את עמודות ה-LargeBinary (file_data/proof_file_data/
    proof_final_file_data, שיכולות להיות כמה מגה-בייט כל אחת) בשביל כל שורה
    ברשימה - מיותר לגמרי ברשימה, ובאמת יכול לגרום להאטה קשה/OOM כשיש הרבה
    דפים/הרבה תוכן. שם הקובץ + סטטוס בלבד מספיקים לתצוגת רשימה."""
    from sqlalchemy.orm import defer
    return query.options(
        defer(ManuscriptPage.file_data),
        defer(ManuscriptPage.proof_file_data),
        defer(ManuscriptPage.proof_final_file_data),
    )


@dictate_bp.route('')
@login_required
def queue():
    from models import ManuscriptPage, ProofingRound, IncomingFax
    from sqlalchemy.orm import defer, joinedload
    status_filter = request.args.get('status', 'open')
    q = _defer_manuscript_page_blobs(ManuscriptPage.query, ManuscriptPage).options(
        joinedload(ManuscriptPage.customer),
        joinedload(ManuscriptPage.proofing_rounds),
    )
    pages = []
    proof_rounds = []
    incoming_faxes = []
    if status_filter == 'open':
        q = q.filter(ManuscriptPage.status.in_(OPEN_STATUSES))
        q = q.order_by(ManuscriptPage.created_at.asc())  # תור - הישן קודם
        pages = q.limit(200).all()
    elif status_filter == 'unpaid':
        q = q.filter(ManuscriptPage.status == 'pending_payment').order_by(ManuscriptPage.created_at.asc())
        pages = q.limit(200).all()
    elif status_filter == 'done':
        q = q.filter(ManuscriptPage.status == 'done')
        q = q.order_by(ManuscriptPage.created_at.desc())
        pages = q.limit(200).all()
    elif status_filter == 'proof':
        # סבבי הגהה (יכול להיות כמה על אותו כתב-יד) שהלקוח שלח עבורם תיקונים
        # בחזרה - ממתינים לסקירת נציג. ראה models.ProofingRound.
        proof_rounds = (ProofingRound.query.filter_by(status='pending')
                         .options(
                             defer(ProofingRound.customer_file_data),
                             defer(ProofingRound.final_file_data),
                             joinedload(ProofingRound.manuscript_page).joinedload(ManuscriptPage.customer),
                         )
                         .order_by(ProofingRound.requested_at.asc()).limit(200).all())
    elif status_filter == 'proof_done':
        # סבבי הגהה שכבר הושלמו וחויבו.
        proof_rounds = (ProofingRound.query.filter_by(status='done')
                         .options(
                             defer(ProofingRound.customer_file_data),
                             defer(ProofingRound.final_file_data),
                             joinedload(ProofingRound.manuscript_page).joinedload(ManuscriptPage.customer),
                         )
                         .order_by(ProofingRound.completed_at.desc()).limit(200).all())
    elif status_filter == 'fax':
        # פקסים נכנסים גולמיים שממתינים לשיוך ידני של נציג - ראה models.IncomingFax
        incoming_faxes = (IncomingFax.query.filter_by(status='pending')
                           .options(defer(IncomingFax.file_data))
                           .order_by(IncomingFax.received_at.asc()).limit(200).all())
    else:
        q = q.order_by(ManuscriptPage.created_at.desc())
        pages = q.limit(200).all()

    # מספרי "ממתין" על הלשוניות - רק מה שבאמת מחכה לטיפול. "הושלמו" ו"הגהות
    # שהושלמו" בכוונה בלי מספר (אין מה לטפל בהם).
    pending = dictate_pending_counts()
    # עלות לתשלום לדפים מושהים (להצגה בתור)
    from services.pricing import char_cost
    unit_size, price_per_unit = _manuscript_pricing()
    for _p in pages:
        if _p.status == 'pending_payment':
            _p.due_cost = char_cost(_manuscript_char_count(_p.content), unit_size, price_per_unit)
    return render_template('admin/dictate_queue.html', pages=pages, proof_rounds=proof_rounds, unpaid_count=pending.get('unpaid', 0),
                            incoming_faxes=incoming_faxes,
                            status_filter=status_filter, open_pending_count=pending['open'],
                            proof_pending_count=pending['proof'], fax_pending_count=pending['fax'])


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


class _PreviewTooLargeError(Exception):
    """התמונה חורגת ממגבלת הפיקסלים הבטוחה לתצוגה מקדימה - לא ממירים כלל."""
    pass


# התצוגה המקדימה מוטמעת ב-<iframe src="data:application/pdf;base64,...">.
# כרום חוסם כתובת data: ארוכה מ-2MB (kMaxURLChars = 2*1024*1024 תווים) -
# ה-iframe נשאר ריק/מציג "הטעינה של מסמך ה-PDF נכשלה", בלי שום שגיאה בצד
# השרת. נבדק בפועל: data URI של 1.82MB נטען, של 2.08MB נחסם. תמונות מהטלפון
# (4000x3000 ומעלה, 3-10MB) נתנו PDF כבד בהרבה מזה ולכן "לפעמים עובד
# ולפעמים לא". לכן מייצרים תמיד PDF "קל" לתצוגה: base64 מנפח ב-33%, אז
# 1.2MB של PDF => ~1.6MB של data URI, עם מרווח ביטחון מתחת לתקרה.
# הקובץ המקורי המלא נשאר זמין להורדה כרגיל.
PREVIEW_MAX_PDF_BYTES = 1_200_000

# רמות איכות יורדות (צלע ארוכה בפיקסלים, איכות JPEG) - מנסים מהטובה לגרועה
# עד שה-PDF נכנס בתקציב. 2400px על דף A4 הם כ-200dpi - מספיק לקרוא כתב-יד.
_PREVIEW_LEVELS = [(2400, 80), (2000, 75), (1700, 70), (1500, 65),
                   (1300, 60), (1100, 55), (900, 50)]
MAX_PREVIEW_IMAGE_SOURCE_BYTES = 25 * 1024 * 1024    # תמונה בודדת (טלפון ברזולוציה גבוהה)
MAX_PREVIEW_PDF_SOURCE_BYTES = 100 * 1024 * 1024     # PDF סרוק רב-עמודים
PREVIEW_MAX_PAGES = 20          # מקסימום עמודים בתצוגה מקדימה (תמונה רב-עמודית/PDF כבד)


def _pages_per_part(src_bytes, total_pages):
    """כמה עמודים בכל "חלק" של התצוגה. עד 20, אבל לקובץ "כבד" (סריקות/
    צילומים - הרבה בייטים לעמוד) מקטינים ל-10 או 5: התקציב לחלק קבוע
    (PREVIEW_MAX_PDF_BYTES), ו-20 עמודים סרוקים בתקציב הזה היו יורדים
    לרזולוציה נמוכה מדי לקריאת כתב-יד. נקבע לפי הקובץ כולו, ולכן קבוע בין
    בקשות (אותם גבולות חלקים בכל מעבר בין חלקים)."""
    avg = src_bytes / float(max(1, total_pages))
    if avg <= 60_000:
        return PREVIEW_MAX_PAGES
    if avg <= 300_000:
        return PREVIEW_MAX_PAGES // 2
    return max(1, PREVIEW_MAX_PAGES // 4)


PREVIEW_MAX_JPEG_PIXELS = 120_000_000   # JPEG נפתח בדגימה מופחתת (draft) - זול גם ב-48MP+
PREVIEW_MAX_OTHER_PIXELS = 25_000_000   # PNG/TIFF/וכו' מפוענחים במלואם - תקרה זהירה


def _frames_to_light_pdf(frames):
    """מקבל רשימת תמונות PIL (RGB/L) ומחזיר PDF קל (bytes) שנכנס בתקציב
    PREVIEW_MAX_PDF_BYTES - כל עמוד כ-JPEG דחוס. מוריד איכות/רזולוציה בהדרגה
    עד שנכנס. מחזיר None אם גם ברמה הנמוכה ביותר זה לא נכנס."""
    import fitz
    from PIL import Image
    out = None
    for maxside, quality in _PREVIEW_LEVELS:
        doc = fitz.open()
        try:
            for im in frames:
                w, h = im.size
                scale = min(1.0, maxside / float(max(w, h)))
                if scale < 1.0:
                    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=quality, optimize=True)
                page = doc.new_page(width=im.width, height=im.height)
                page.insert_image(page.rect, stream=buf.getvalue())
            out = doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
        if len(out) <= PREVIEW_MAX_PDF_BYTES:
            return out
    return None


def _image_to_pdf_bytes(raw, part=1):
    """ממיר בייטי תמונה (jpg/png/tiff/heic וכו') ל-PDF "קל" לתצוגה מקדימה
    (ראה PREVIEW_MAX_PDF_BYTES). מיישם סיבוב לפי EXIF (תמונות מהטלפון נשמרות
    "שוכבות" עם דגל סיבוב), ממזג שקיפות על רקע לבן, ותומך בתמונה רב-עמודית
    (TIFF של פקס) - מציג רק את "חלק" מספר part (PREVIEW_MAX_PAGES עמודים
    בכל חלק). מחזיר (pdf_bytes, total_pages, part_used, per_part), או None אם אי אפשר
    להקטין מספיק."""
    from PIL import Image, ImageOps, ImageSequence
    try:  # HEIC/HEIF מאייפון - אופציונלי, רק אם החבילה מותקנת
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    img = Image.open(io.BytesIO(raw))

    # הגנה מפני "פצצת דחיסה": קובץ מקור קטן (למשל TIFF של פקס, דחוס מאוד
    # ב-CCITT) יכול להתפרש לרזולוציה ענקית בזיכרון ולהפיל את התהליך (OOM).
    # img.width/height נקראים מהכותרת בלבד (Pillow "עצלן"), אז הבדיקה זולה.
    # JPEG מקבל תקרה גבוהה בהרבה כי פותחים אותו בדגימה מופחתת (draft) -
    # צילום טלפון של 48-108 מגה-פיקסל לא מפוענח במלואו בזיכרון.
    is_jpeg = (img.format == 'JPEG')
    max_pixels = PREVIEW_MAX_JPEG_PIXELS if is_jpeg else PREVIEW_MAX_OTHER_PIXELS
    if img.width * img.height > max_pixels:
        raise _PreviewTooLargeError(
            f"{img.width}x{img.height} = {img.width * img.height:,} פיקסלים - חורג מהמגבלה"
        )
    if is_jpeg:
        # מבקש מהמפענח לפענח ישר בגודל מוקטן (פי 2/4/8) - מהיר וחסכוני בזיכרון
        img.draft('RGB', (_PREVIEW_LEVELS[0][0], _PREVIEW_LEVELS[0][0]))

    total = max(1, int(getattr(img, 'n_frames', 1) or 1))
    per = _pages_per_part(len(raw), total)
    parts = max(1, -(-total // per))
    part = min(max(int(part or 1), 1), parts)
    start = (part - 1) * per
    end = min(total, start + per)

    frames = []
    for i in range(start, end):
        try:
            img.seek(i)
        except EOFError:
            break
        f = img.copy()
        try:
            f = ImageOps.exif_transpose(f)
        except Exception:
            pass
        if f.mode in ('RGBA', 'LA') or (f.mode == 'P' and 'transparency' in f.info):
            f = f.convert('RGBA')
            bg = Image.new('RGB', f.size, (255, 255, 255))
            bg.paste(f, mask=f.split()[-1])
            f = bg
        elif f.mode in ('L',):
            pass
        elif f.mode == '1':
            f = f.convert('L')
        elif f.mode != 'RGB':
            f = f.convert('RGB')
        # מקטינים כבר כאן לתקרה העליונה כדי לא להחזיק תמונה ענקית בזיכרון
        top = _PREVIEW_LEVELS[0][0]
        if max(f.size) > top:
            f.thumbnail((top, top), Image.LANCZOS)
        frames.append(f)
    if not frames:
        return None
    light = _frames_to_light_pdf(frames)
    if light is None:
        return None
    return light, total, part, per


# תור-חוטים ייעודי להמרות תצוגה מקדימה, עם timeout קשיח בזמן-אמת (wall
# clock) - שכבת הגנה נוספת מעבר לתקרות הגודל/פיקסלים למעלה. תקרות אלו
# מכסות את המקרה הידוע (TIFF ענק שנדחס טוב), אבל timeout הוא רשת ביטחון
# גם למקרים לא-צפויים (קובץ תקול שגורם ל-codec להיתקע בלולאה, קידוד נדיר
# ואיטי וכו') - בלעדיו, gunicorn (worker מסוג sync) עלול לחכות עד timeout
# הפנימי שלו (בסביבות 120 שניות, לפי הלוגים) ואז לשלוח SIGKILL לכל
# ה-worker process - זו קריסה הרבה יותר יקרה מ"אין תצוגה מקדימה".
import concurrent.futures as _futures
_preview_executor = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='preview-convert')
PREVIEW_CONVERT_TIMEOUT_SECONDS = 20


def _image_to_pdf_bytes_with_timeout(raw, part=1):
    future = _preview_executor.submit(_image_to_pdf_bytes, raw, part)
    try:
        return future.result(timeout=PREVIEW_CONVERT_TIMEOUT_SECONDS)
    except _futures.TimeoutError:
        # לא ניתן "להרוג" תרד ב-Python באמצע עבודה, אז התהליך ברקע ימשיך
        # לרוץ ויתפוס CPU/RAM עד שיסתיים בעצמו - אבל הבקשה של הלקוח כבר לא
        # תלויה בו ותחזור מיד עם "אין תצוגה מקדימה" במקום להיתקע עד שה-worker
        # כולו ייהרג.
        raise TimeoutError(
            f"המרת התמונה ל-PDF ארכה יותר מ-{PREVIEW_CONVERT_TIMEOUT_SECONDS} שניות"
        )


def _validate_or_repair_pdf(raw):
    """בודקת שקובץ PDF ניתן לפתיחה/רינדור בפועל, ולא רק ש-5 הבייטים
    הראשונים תואמים לחתימת "%PDF-". קובץ יכול להתחיל בחתימה תקינה ועדיין
    להיות פגום מבחינה מבנית (הועלה חלקי, נוצר ע"י כלי סריקה/פקס לא תקני
    וכו') - במקרה כזה, אם רק בודקים את החתימה, ה-iframe בדפדפן מציג שגיאת
    דפדפן גולמית ומבלבלת ("הטעינה של מסמך ה-PDF נכשלה") במקום ההודעה
    האחידה של המערכת ("הקובץ אינו זמין").
    PyMuPDF (fitz, כבר תלות קיימת בפרויקט) פותח את הקובץ ומנסה "לנקות"/
    לבנות מחדש את המבנה הפנימי שלו (garbage collection + דחיסה) - זה גם
    מתקן הרבה מקרים של PDF פגום-חלקית בדרך, לא רק מזהה אותם. מחזירה bytes
    מתוקנים בהצלחה, או None אם הקובץ פגום לגמרי ולא ניתן לפתיחה/תיקון."""
    import fitz
    doc = None
    try:
        doc = fitz.open(stream=raw, filetype='pdf')
        if doc.page_count < 1:
            return None
        return doc.tobytes(garbage=4, deflate=True, clean=True)
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()


def _validate_or_repair_pdf_with_timeout(raw):
    future = _preview_executor.submit(_validate_or_repair_pdf, raw)
    try:
        return future.result(timeout=PREVIEW_CONVERT_TIMEOUT_SECONDS)
    except _futures.TimeoutError:
        raise TimeoutError(
            f"אימות/תיקון ה-PDF ארך יותר מ-{PREVIEW_CONVERT_TIMEOUT_SECONDS} שניות"
        )


def _pdf_part_for_preview(raw, part=1):
    """PDF אמיתי -> (pdf_bytes, total_pages, part_used, per_part) של "חלק" מספר part
    (PREVIEW_MAX_PAGES עמודים בכל חלק), או None אם הקובץ פגום/לא ניתן לפתיחה.
    בונה PDF חדש מהעמודים של החלק בלבד (זה גם מתקן PDF פגום-חלקית, ומבטיח
    שלא מעבדים מסמך של מאות עמודים בבת אחת). אם התוצאה כבדה מדי להטמעה
    כ-data URI (ראה PREVIEW_MAX_PDF_BYTES) - מרנדר את העמודים לתמונות
    דחוסות; הטקסט הנבחר/חיפוש הולכים לאיבוד בגרסת התצוגה בלבד."""
    import fitz
    from PIL import Image
    src = None
    try:
        src = fitz.open(stream=raw, filetype='pdf')
        total = src.page_count
        if total < 1:
            return None
        per = _pages_per_part(len(raw), total)
        parts = max(1, -(-total // per))
        part = min(max(int(part or 1), 1), parts)
        a = (part - 1) * per
        b = min(total, a + per) - 1
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=a, to_page=b)
            data = out.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            out.close()
        if len(data) <= PREVIEW_MAX_PDF_BYTES:
            return data, total, part, per
        n = b - a + 1
        # הרבה עמודים => רזולוציית רינדור נמוכה יותר, כדי לחסוך בזיכרון
        top = 1800 if n <= 8 else 1400
        frames = []
        for i in range(a, b + 1):
            pg = src[i]
            r = pg.rect
            scale = top / float(max(r.width, r.height, 1))
            pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            frames.append(Image.frombytes('RGB', (pix.width, pix.height), pix.samples))
        light = _frames_to_light_pdf(frames)
        if light is None:
            return None
        return light, total, part, per
    except Exception as e:
        log.warning(f"pdf preview part error: {e}")
        return None
    finally:
        if src is not None:
            src.close()


def _pdf_part_for_preview_with_timeout(raw, part=1):
    future = _preview_executor.submit(_pdf_part_for_preview, raw, part)
    try:
        return future.result(timeout=PREVIEW_CONVERT_TIMEOUT_SECONDS)
    except _futures.TimeoutError:
        raise TimeoutError(
            f"הכנת ה-PDF לתצוגה ארכה יותר מ-{PREVIEW_CONVERT_TIMEOUT_SECONDS} שניות"
        )


def _file_to_pdf_preview(raw, filename, log_context='', part=1):
    """הליבה המשותפת של המרת bytes+filename ל-data URI מוטמע, ממיר תמיד
    ל-PDF (גם אם המקור תמונה) - ראה _manuscript_data_uri למטה להסבר המלא
    (חסימת נטפרי). מנוצל גם בתצוגת כתב-היד המקורי (studio) וגם בתצוגת הקובץ
    שהלקוח שלח בחזרה בהגהה ובפקסים נכנסים.
    קובץ עם יותר מ-PREVIEW_MAX_PAGES עמודים מוצג ב"חלקים" (part=1,2,3...) -
    כל חלק הוא PDF קל נפרד (כרום חוסם data URI מעל ~2MB, אז אי אפשר להטמיע
    מסמך שלם). מחזיר dict: uri, is_pdf, part, parts, page_count, first_page,
    last_page. uri=None אם אין תצוגה."""
    info = {'uri': None, 'is_pdf': False, 'part': 1, 'parts': 1, 'per_part': PREVIEW_MAX_PAGES,
            'page_count': 0, 'first_page': 0, 'last_page': 0}
    if not raw:
        return info

    # מזהים PDF אמיתי לפי חתימת התוכן עצמו (magic bytes של PDF: "%PDF-"),
    # לא רק לפי סיומת שם הקובץ - התגלה בפועל שמודול הפקס של ימות המשיח
    # מצרף את הפקס כקובץ עם סיומת .pdf שבפועל אינו PDF תקין (כנראה תמונת
    # TIFF גולמית, פורמט נפוץ לפקסים) - אם סומכים רק על הסיומת, קובץ כזה
    # "עובר" בלי המרה ונשבר בתצוגה בדפדפן ("טעינה של מסמך ה-PDF נכשלה").
    if raw[:5] == b'%PDF-':
        mime = 'application/pdf'
    else:
        mime, _ = mimetypes.guess_type(filename or '')
        if not mime or mime == 'application/pdf':
            ext = os.path.splitext(filename or '')[1].lower()
            mime = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                '.gif': 'image/gif', '.webp': 'image/webp',
                '.tif': 'image/tiff', '.tiff': 'image/tiff',
            }.get(ext, 'application/octet-stream')

    # מגבלת גודל קובץ מקור - קובץ ענק עלול לגרום לעיבוד לקחת המון זמן/זיכרון
    # ולהפיל את הבקשה (OOM/timeout על Railway - נראה כ"upstream error").
    # PDF רב-עמודים נסרק לפעמים לעשרות מגה-בייט - אבל מעבדים ממנו רק חלק של
    # 20 עמודים בכל פעם, אז התקרה שלו גבוהה יותר מזו של תמונה בודדת (שמפוענחת
    # כולה בזיכרון).
    max_src = MAX_PREVIEW_PDF_SOURCE_BYTES if mime == 'application/pdf' else MAX_PREVIEW_IMAGE_SOURCE_BYTES
    if len(raw) > max_src:
        log.warning(f"_file_to_pdf_preview ({log_context}): קובץ גדול מדי לתצוגה מקדימה ({len(raw)} bytes) - מדלגים על התצוגה")
        return info

    try:
        if mime == 'application/pdf':
            # לא מסתפקים בחתימת "%PDF-" - פותחים בפועל ובונים מחדש (ראה
            # _pdf_part_for_preview); קובץ פגום לגמרי => "הקובץ אינו זמין".
            res = _pdf_part_for_preview_with_timeout(raw, part)
            if res is None:
                log.warning(f"_file_to_pdf_preview ({log_context}): קובץ ה-PDF פגום/לא ניתן להקטנה לתצוגה - מדלגים")
                return info
        else:
            try:
                res = _image_to_pdf_bytes_with_timeout(raw, part)
            except (TimeoutError, _PreviewTooLargeError):
                raise
            except Exception as e:
                log.error(f"image->PDF conversion error ({log_context}): {e}")
                # פורמט שלא ניתן לפענוח (למשל HEIC בלי תמיכה) - אפשר להטמיע
                # את הקובץ המקורי רק אם הוא קטן מספיק; אחרת אין תצוגה.
                if len(raw) > PREVIEW_MAX_PDF_BYTES:
                    return info
                b64 = base64.b64encode(raw).decode('ascii')
                info['uri'] = f'data:{mime};base64,{b64}'
                return info
            if res is None:
                log.warning(f"_file_to_pdf_preview ({log_context}): לא ניתן להקטין את התמונה מספיק לתצוגה - מדלגים")
                return info
    except _PreviewTooLargeError as e:
        log.warning(f"_file_to_pdf_preview ({log_context}): תמונה גדולה מדי לתצוגה מקדימה ({e}) - מדלגים על התצוגה")
        return info
    except TimeoutError as e:
        log.error(f"preview conversion timeout ({log_context}): {e}")
        return info

    pdf_bytes, total, part_used, per = res
    parts = max(1, -(-total // per))
    info.update({
        'uri': 'data:application/pdf;base64,' + base64.b64encode(pdf_bytes).decode('ascii'),
        'is_pdf': True, 'part': part_used, 'parts': parts, 'page_count': total,
        'per_part': per,
        'first_page': (part_used - 1) * per + 1,
        'last_page': min(total, part_used * per),
    })
    return info


def _file_to_pdf_data_uri(raw, filename, log_context=''):
    """תאימות לאחור: מחזיר (data_uri, is_pdf) של החלק הראשון."""
    info = _file_to_pdf_preview(raw, filename, log_context)
    return info['uri'], info['is_pdf']


def _manuscript_data_uri(page):
    """מקודד את קובץ כתב-היד כ-data URI (base64) מוטמע ישירות ב-HTML - לא
    כ-src לכתובת נפרדת - כדי שלא תהיה בקשת רשת נפרדת שמסנן תוכן כמו נטפרי
    יכול לעכב. בפועל זה לא הספיק: מתברר שנטפרי חוסם גם תמונות שמגיעות כ-data
    URI מוטמע (כנראה לפי ניתוח התוכן/mime של האלמנט עצמו, לא רק לפי בקשת
    הרשת) - אז כל תמונה מומרת ל-PDF חד-עמודי (Pillow) לפני ההטמעה, ומוצגת
    ב-iframe בדיוק כמו PDF "אמיתי" שהגיע במייל. PDF לא נחסם אצל נטפרי באותה
    צורה שתמונה נחסמת.
    מחזיר (data_uri, is_pdf) - is_pdf קובע ב-template אם להציג ב-<iframe>
    (PDF) או ב-<img> (fallback, רק אם המרה ל-PDF נכשלה מסיבה כלשהי)."""
    raw = _manuscript_file_bytes(page)
    return _file_to_pdf_data_uri(raw, page.original_filename or page.file_path, log_context=f'page={page.id}')


def _manuscript_preview(page, part=1):
    """כמו _manuscript_data_uri אבל עם תמיכה ב"חלקים" לקובץ רב-עמודים -
    מחזיר את ה-dict המלא של _file_to_pdf_preview."""
    raw = _manuscript_file_bytes(page)
    return _file_to_pdf_preview(raw, page.original_filename or page.file_path,
                                log_context=f'page={page.id}', part=part)


def _preview_json(info):
    """תשובת JSON לטעינת חלק אחר של התצוגה המקדימה בלי לרענן את הדף (חשוב
    בסטודיו - רענון היה מוחק עבודה שלא נשמרה: הקלטה/עריכה)."""
    from flask import jsonify
    return jsonify({k: info[k] for k in ('uri', 'is_pdf', 'part', 'parts',
                                         'page_count', 'first_page', 'last_page', 'per_part')})


def _proof_returned_kind(filename):
    """קובע איך להציג את הקובץ שהלקוח שלח בחזרה בסבב הגהה: 'docx' אם יש לו
    מחשב ותיקן ישירות בקובץ ה-Word (הסיומת docx) - אז מציגים תצוגת טקסט
    מפורקת (_docx_paragraphs_html). אחרת 'image' - המקרה הנפוץ בפועל של
    לקוח בלי גישה נוחה למחשב, שהדפיס את הדף, תיקן בעט בכתב יד, וצילם/סרק
    ושלח בחזרה תמונה/PDF - אז מציגים אותו כמו כתב-היד המקורי (iframe PDF,
    ראה _file_to_pdf_data_uri)."""
    ext = os.path.splitext(filename or '')[1].lstrip('.').lower()
    return 'docx' if ext == 'docx' else 'image'


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
    pv = _manuscript_preview(page, request.args.get('part', 1, type=int))
    return render_template(
        'admin/dictate_studio.html',
        page=page,
        default_engine=DEFAULT_DICTATION_ENGINE,
        manuscript_data_uri=pv['uri'],
        manuscript_is_pdf=pv['is_pdf'],
        pv=pv,
        pv_url=url_for('dictate.studio_preview_part', page_id=page.id),
    )


@dictate_bp.route('/<int:page_id>/preview-part')
@login_required
def studio_preview_part(page_id):
    """חלק אחר (20 עמודים) של כתב-היד בסטודיו - JSON, נטען ב-JS בלי רענון."""
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    return _preview_json(_manuscript_preview(page, request.args.get('part', 1, type=int)))


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


def _manuscript_billing_account(customer):
    """החשבון שמחויב על הדף: "מסמך כללי" של מוסד (Customer.is_institution_self,
    ראה routes/institution.py.ensure_institution_self_customer) לא צובר יתרה
    משלו - מחייבים ישירות את יתרת המוסד; בכל מקרה אחר - הלקוח עצמו."""
    if customer and customer.is_institution_self and customer.institution:
        return customer.institution
    return customer


def _manuscript_held_recipients(customer, billing_account):
    """למי שולחים הודעת "הדף הושהה - צריך להטעין יתרה". מחזיר רשימת
    (כתובת מייל, סוג, טלפון_לקישור_טעינה) כש"סוג" הוא:
      'customer' - לקוח רגיל: הסבר + קישור טעינה
      'institution' - מסמך כללי של מוסד: אל המוסד עצמו, עם קישור טעינה
      'student' - תלמיד של מוסד: אומרים לו לפנות למוסד לבקש תוספת יתרה
      'manager' - מנהל המוסד: מודיעים שלתלמיד פלוני ממתין מסמך וצריך להוסיף לו יתרה."""
    inst = customer.institution if customer else None
    out = []
    if customer and customer.is_institution_self and inst:
        out.append(((inst.notify_email or inst.email or '').strip(), 'institution', (inst.phone or '').strip()))
    elif customer and customer.institution_id and inst:
        student_mail = (customer.email or '').strip()
        manager_mail = (inst.notify_email or inst.email or '').strip()
        if manager_mail:
            out.append((manager_mail, 'manager', ''))
        if student_mail and student_mail.lower() != manager_mail.lower():
            out.append((student_mail, 'student', ''))
    else:
        out.append(((customer.email or '').strip() if customer else '', 'customer', ((customer.phone or '').strip() if customer else '')))
    return [r for r in out if r[0]]


def _send_manuscript_held_email(page, customer, billing_account, cost):
    """מיילים על דף שהושהה בגלל יתרה לא מספקת. אחרי הטעינה המסמך יישלח
    אוטומטית. תלמיד של מוסד: גם התלמיד (לפנות למוסד לבקש תוספת יתרה) וגם
    מנהל המוסד (לתלמיד פלוני ממתין מסמך וצריך להוסיף לו יתרה) מקבלים מייל.
    מחזיר את הכתובות שנשלח אליהן (מחרוזת, מופרדות בפסיק), או None."""
    recipients = _manuscript_held_recipients(customer, billing_account)
    if not recipients:
        return None
    from html import escape
    from routes.email_inbound import _is_system_inbound_address
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email
    balance = float(getattr(billing_account, 'balance', 0) or 0)
    base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
    fname = escape(page.original_filename or 'כתב יד')
    student_name = escape(customer.display_name if customer else '')
    inst_name = escape(customer.institution.name) if (customer and customer.institution) else ''
    amounts = (f'<div style="background:#fef3c7;border-right:4px solid #f59e0b;padding:14px;margin:14px 0;border-radius:8px">'
               f'<p style="margin:0">עלות: <b>₪{cost:.2f}</b><br>יתרה נוכחית: <b>₪{balance:.2f}</b></p></div>')
    footer = '<p style="color:#6b7280;font-size:13px">מערכת תמלול פון 03-3131795</p>'
    phone_howto = ('<p style="text-align:center;font-weight:700;color:#1d4ed8">'
                   'אפשר גם בטלפון: התקשרו ל-03-3131795 ובתפריט הראשי בחרו בטעינת ארנק</p>')

    def link_html(topup_phone):
        if topup_phone and base_url and os.environ.get('NEDARIM_MOSAD'):
            from urllib.parse import quote
            link = f"{base_url}/payment/nedarim/topup-link/{quote(topup_phone)}"
            return (f'<p style="text-align:center;margin:18px 0"><a href="{link}" '
                    f'style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 28px;'
                    f'border-radius:8px;font-weight:700;display:inline-block">💳 לטעינת יתרה בכרטיס אשראי</a></p>')
        return ''

    def build(kind, topup_phone):
        if kind == 'student':
            subject = 'תמלול פון - הכתב יד שלך מוכן, יש לפנות למוסד להוספת יתרה'
            body = (f'<p>שלום,</p><p>ההקראה של <b>{fname}</b> הושלמה ומוכנה למשלוח, אך <b>אין לך מספיק יתרה</b> '
                    f'ולכן היא הושהתה.</p>{amounts}'
                    f'<p><b>עליך לפנות להנהלת המוסד{(" (" + inst_name + ")") if inst_name else ""} ולבקש שיוסיפו לך יתרה.</b> '
                    f'הודענו להנהלת המוסד על כך. מיד כשהיתרה תתווסף, המסמך יישלח אליך אוטומטית - אין צורך לפנות אלינו.</p>')
        elif kind == 'manager':
            subject = f'תמלול פון - לתלמיד {student_name} ממתין מסמך, נדרשת הוספת יתרה'
            body = (f'<p>שלום,</p><p>התלמיד/ה <b>{student_name}</b> שלח/ה את הקובץ <b>{fname}</b>. ההקראה הושלמה אך '
                    f'<b>ליתרה של התלמיד אין מספיק</b> ולכן המסמך הושהה.</p>{amounts}'
                    f'<p><b>כדי שהמסמך יישלח, יש להוסיף ליתרת התלמיד</b> דרך אזור המוסד באתר (לשונית התלמידים, שדה הסכום ליד התלמיד). '
                    f'מיד לאחר הוספת היתרה המסמך יישלח לתלמיד אוטומטית.</p>')
        else:
            subject = 'תמלול פון - הכתב יד מוכן, נדרשת טעינת יתרה'
            body = (f'<p>שלום,</p><p>ההקראה של <b>{fname}</b> הושלמה ומוכנה למשלוח, אך <b>היתרה בארנק אינה מספיקה</b> '
                    f'ולכן היא הושהתה.</p>{amounts}'
                    f'<p><b>מיד אחרי הטעינה המסמך יישלח אליכם אוטומטית</b> - אין צורך לפנות אלינו שוב.</p>'
                    f'{link_html(topup_phone)}{phone_howto}')
        html = f'<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#111827"><h2 style="color:#1d4ed8">הקראת כתב יד מוכנה</h2>{body}{footer}</div>'
        return subject, html

    sent_to = []
    for addr, kind, topup_phone in recipients:
        try:
            if _is_system_inbound_address(addr):
                log.error(f"חסימת שליחת מייל השהיה לכתובת המערכת עצמה ({addr}) - מניעת לולאה")
                continue
            subject, html = build(kind, topup_phone)
            sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
            sg.send(Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
                         to_emails=addr, subject=subject, html_content=html))
            sent_to.append(addr)
            log.info(f"manuscript held email ({kind}) sent to {addr} (page={page.id})")
        except Exception as e:
            log.error(f"manuscript held email error to {addr} (page={page.id}): {e}")
    return ', '.join(sent_to) or None


def _manuscript_send_core(page, form_email='', free=False, notify_if_held=True):
    """הלב של "שלח": מוסר את הדף ללקוח ומחייב. מחזיר (payload, http_status).
    - יתרה מספיקה -> נשלח + חיוב (status='done').
    - יתרה לא מספיקה -> לא שגיאה: הדף מושהה (status='pending_payment'),
      נשלח מייל ללקוח עם הסבר/קישור טעינה, ובעת הטעינה הוא יישלח אוטומטית
      (process_pending_manuscripts). payload['held'] = True.
    - free=True (נציג בחר "שלח ללא חיוב") -> נשלח בלי שום חיוב."""
    from models import Transaction
    if not page.content:
        return {'error': 'אין תוכן לשליחה'}, 400

    customer = page.customer
    to_email = (form_email or '').strip() or ((customer.email or '').strip() if customer else '')
    is_institution_student = bool(customer and customer.institution_id)
    to_fax_number = ''
    if not to_email and not is_institution_student and customer:
        to_fax_number = (customer.fax or customer.phone or '').strip()

    if not to_email and not is_institution_student and not to_fax_number:
        return {'error': 'אין כתובת מייל, מספר פקס או שיוך למוסד ליעד - אין דרך למסור ללקוח הזה'}, 400

    # התשלום יורד מהלקוח רק פעם אחת - בפעם הראשונה שהדף באמת מסתיים ונשלח
    # בהצלחה (status עובר ל-'done'). שליחה חוזרת של דף שכבר נשלח היא לא חיוב
    # נוסף. "הקלט מחדש" (redo) מאפס ל-'pending' - ושליחה הבאה שוב מחויבת.
    already_sent = (page.status == 'done')

    billing_account = _manuscript_billing_account(customer)

    unit_size, price_per_unit = _manuscript_pricing()
    char_count = _manuscript_char_count(page.content)
    # חיוב יחסי לכמות התווים, מעוגל כלפי מעלה ל-10 אגורות
    from services.pricing import char_cost
    full_cost = char_cost(char_count, unit_size, price_per_unit)
    cost = 0.0 if (already_sent or free) else full_cost

    if cost > 0:
        if not billing_account:
            return {'error': 'לא נמצא לקוח/מוסד משויך לדף זה - לא ניתן לחייב'}, 400
        if (billing_account.balance or 0) < cost:
            log.info(
                f"manuscript send: יתרה לא מספיקה עבור {'institution' if billing_account is not customer else 'customer'} "
                f"{billing_account.id} (צריך {cost}, יש {billing_account.balance}), page={page.id} - מושהה"
            )
            was_held = (page.status == 'pending_payment')
            page.status = 'pending_payment'
            # כתובת היעד שנבחרה נשמרת (sent_to) כדי שהמשלוח האוטומטי אחרי
            # הטעינה ילך לאותה כתובת בדיוק.
            page.sent_to = to_email or None
            page.sent_via = None
            db.session.commit()
            notified = None
            if notify_if_held and not was_held:
                notified = _send_manuscript_held_email(page, customer, billing_account, cost)
            return {
                'status': 'held', 'held': True, 'cost': cost,
                'balance': float(billing_account.balance or 0),
                'notified_to': notified, 'already_held': was_held,
            }, 200

    try:
        if to_email:
            _send_manuscript_email(to_email, customer.name if customer else '', customer.phone if customer else '', page.id, page.original_filename, page.content)
            page.sent_via = 'email'
            page.sent_to = to_email
        elif to_fax_number:
            docx_bytes = _build_manuscript_docx(customer.name if customer else '', page.original_filename, page.content)
            _send_manuscript_fax(page, to_fax_number, docx_bytes, filename_hint=f'manuscript_{page.id}.pdf')
            page.sent_via = 'fax'
            page.sent_to = to_fax_number
        else:
            # לקוח מוסד ללא מייל - שום מסירה אקטיבית, רק סימון "מוכן"
            # (המוסד רואה/מוריד בעצמו בתיק התלמיד באתר).
            page.sent_via = None
            page.sent_to = None

        if cost > 0 and billing_account:
            billing_account.balance -= cost
            db.session.add(Transaction(
                customer_id=customer.id,
                amount=-cost,
                type='manuscript_dictation',
                description=f'הקראת כתב יד - {page.original_filename}' + (' (חויב מיתרת המוסד)' if billing_account is not customer else ''),
            ))
        elif free and not already_sent and customer:
            db.session.add(Transaction(
                customer_id=customer.id,
                amount=0.0,
                type='manuscript_free',
                description=f'הקראת כתב יד - {page.original_filename} (נשלח ללא חיוב ע"י נציג, עלות רגילה ₪{full_cost:.2f})',
            ))
        if not already_sent:
            page.char_count = char_count
            page.cost = cost
        page.sent_at = datetime.utcnow()
        page.status = 'done'
        db.session.commit()
        return {
            'status': 'sent', 'to': page.sent_to, 'via': page.sent_via or 'portal',
            'cost': cost, 'char_count': char_count, 'resend': already_sent,
            'free': bool(free and not already_sent),
        }, 200
    except Exception as e:
        db.session.rollback()
        log.error(f"manuscript send error (page={page.id}): {e}", exc_info=True)
        return {'error': f'שליחה נכשלה: {e}'}, 500


@dictate_bp.route('/<int:page_id>/send', methods=['POST'])
@login_required
def send(page_id):
    """מסמן דף כתב-יד כמוכן/משויך, וממסור אותו ללקוח - לפי מי הלקוח, בדרך
    שונה (ראה גם proof_complete למטה):
      1. יש כתובת מייל (הוזנה ידנית בטופס, או קיימת על הלקוח) -> מייל.
      2. אין מייל אבל הלקוח שייך למוסד -> סימון "מוכן" (המוסד רואה/מוריד
         בעצמו בתיק התלמיד באתר).
      3. אין מייל ואין מוסד, אבל יש טלפון/פקס -> פקס.
      4. אף אחד מהנ"ל - שגיאה אמיתית.
    יתרה לא מספיקה: הדף מושהה (לא שגיאה) והלקוח מקבל מייל - ראה
    _manuscript_send_core. הפרמטר free=1 = שליחה ללא חיוב (כפתור נציג)."""
    from models import ManuscriptPage
    page = ManuscriptPage.query.get_or_404(page_id)
    free = (request.form.get('free') or '') in ('1', 'true', 'on')
    payload, code = _manuscript_send_core(page, request.form.get('to_email') or '', free=free)
    return jsonify(payload), code


def process_pending_manuscripts(customer_id=None, institution_id=None):
    """אחרי טעינת יתרה: שולח אוטומטית כל דף שהושהה (pending_payment) ושעכשיו
    היתרה מספיקה לו - בלי שום פעולת נציג. customer_id = לקוח/תלמיד שהיתרה
    שלו התעדכנה; institution_id = מוסד שהיתרה שלו התעדכנה (חל על "מסמכים
    כלליים" של המוסד, שמחויבים מיתרת המוסד)."""
    from flask import has_app_context, current_app
    if has_app_context():
        app_obj = current_app._get_current_object()
    else:
        from app import app as app_obj
    with app_obj.app_context():
        from models import ManuscriptPage, Customer
        q = ManuscriptPage.query.filter_by(status='pending_payment')
        if customer_id:
            q = q.filter(ManuscriptPage.customer_id == customer_id)
        elif institution_id:
            q = q.join(Customer, ManuscriptPage.customer_id == Customer.id).filter(
                Customer.institution_id == institution_id, Customer.is_institution_self.is_(True))
        else:
            return 0
        ids = [pid for (pid,) in q.with_entities(ManuscriptPage.id).order_by(ManuscriptPage.created_at.asc()).all()]
        sent = 0
        for pid in ids:
            try:
                # נעילת השורה - שתי טעינות/webhooks במקביל לא ישלחו פעמיים
                page = ManuscriptPage.query.filter_by(id=pid).with_for_update().first()
                if not page or page.status != 'pending_payment' or not page.content:
                    db.session.rollback()
                    continue
                target = page.sent_to if '@' in (page.sent_to or '') else ''
                payload, code = _manuscript_send_core(page, target, free=False, notify_if_held=False)
                if payload.get('status') == 'sent':
                    sent += 1
                    log.info(f"process_pending_manuscripts: נשלח דף {pid} אחרי טעינה (עלות {payload.get('cost')})")
                else:
                    db.session.rollback()
                    log.info(f"process_pending_manuscripts: דף {pid} עדיין לא נשלח: {payload}")
            except Exception as e:
                db.session.rollback()
                log.error(f"process_pending_manuscripts error (page={pid}): {e}", exc_info=True)
        return sent


def trigger_pending_manuscripts(customer_id=None, institution_id=None, delay=2):
    """מפעיל את process_pending_manuscripts ברקע (לא חוסם את בקשת הטעינה)."""
    import threading, time

    def _run():
        time.sleep(delay)
        try:
            process_pending_manuscripts(customer_id=customer_id, institution_id=institution_id)
        except Exception as e:
            log.error(f"trigger_pending_manuscripts error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


# ==========================================================================
# הגהה חוזרת - הלקוח שלח בחזרה קובץ Word עם תיקונים משלו (ראה
# routes/email_inbound.py._is_proofing_reply), והנציג סוקר/מעדכן ומסיים כאן.
# ==========================================================================

@dictate_bp.route('/proof/<int:round_id>/preview-part')
@login_required
def proof_preview_part(round_id):
    """חלק אחר (20 עמודים) מהקובץ שהלקוח שלח בהגהה - JSON, בלי רענון דף."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    return _preview_json(_file_to_pdf_preview(
        round_.customer_file_data, round_.customer_file_filename,
        log_context=f'proof round={round_.id}', part=request.args.get('part', 1, type=int)))


@dictate_bp.route('/proof/<int:round_id>')
@login_required
def studio_proof(round_id):
    from flask import flash, redirect
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    page = round_.manuscript_page
    if not round_.customer_file_data:
        flash('אין קובץ מצורף לסבב ההגהה הזה')
        return redirect(url_for('dictate.queue', status='proof'))

    returned_kind = _proof_returned_kind(round_.customer_file_filename)
    if returned_kind == 'docx':
        returned_preview_html = _docx_paragraphs_html(round_.customer_file_data)
        returned_data_uri, returned_is_pdf = None, False
        pv = None
    else:
        returned_preview_html = None
        pv = _file_to_pdf_preview(
            round_.customer_file_data, round_.customer_file_filename,
            log_context=f'proof round={round_.id}', part=request.args.get('part', 1, type=int)
        )
        returned_data_uri, returned_is_pdf = pv['uri'], pv['is_pdf']

    # תוכן ההתחלה לעורך המובנה בדפדפן: אם הנציג כבר התחיל לערוך קודם (יש
    # edited_content שמור על הסבב) ממשיכים משם - אחרת מתחילים מהתוכן המקורי
    # הנקי שנשלח ללקוח (page.content), בדיוק כמו שהוא נראה בקובץ המקורי.
    editor_initial_content = round_.edited_content if round_.edited_content else (page.content or [])

    base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')
    original_docx_url = f"{base_url}{url_for('dictate.proof_original_docx', round_id=round_.id)}"
    ms_word_link = f"ms-word:ofe|u|{original_docx_url}"

    # סבבי הגהה אחרים על אותו כתב-יד (אם יש) - לניווט/הקשר, ראה
    # models.ProofingRound - עכשיו אפשר כמה סבבים נפרדים על אותו דף.
    sibling_rounds = sorted(page.proofing_rounds, key=lambda r: r.created_at)
    round_index = next((i for i, r in enumerate(sibling_rounds) if r.id == round_.id), 0) + 1

    return render_template(
        'admin/proof_studio.html',
        page=page,
        round=round_,
        readonly=(round_.status != 'pending'),
        returned_kind=returned_kind,
        returned_preview_html=returned_preview_html,
        returned_data_uri=returned_data_uri,
        returned_is_pdf=returned_is_pdf,
        pv=pv,
        pv_url=url_for('dictate.proof_preview_part', round_id=round_.id),
        editor_initial_content=editor_initial_content,
        proofing_price_per_minute=_manuscript_proofing_price(),
        timer_state=_proofing_timer_state(round_),
        sibling_rounds=sibling_rounds,
        round_index=round_index,
        ms_word_link=ms_word_link,
    )


@dictate_bp.route('/proof/<int:round_id>/original.docx')
@login_required
def proof_original_docx(round_id):
    """הקובץ שנשלח במקור ללקוח - נבנה תמיד מחדש מ-page.content (מקור האמת),
    לא נשמר בנפרד - כך גם אם מבקשים אותו שוב ושוב זה תמיד עקבי לתוכן הנוכחי."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    page = round_.manuscript_page
    docx_bytes = _build_manuscript_docx(page.customer.name, page.original_filename, page.content)
    safe_name = os.path.splitext(page.original_filename or 'כתב_יד')[0][:40]
    return send_file(
        io.BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=f'מקור_{safe_name}.docx',
    )


@dictate_bp.route('/proof/<int:round_id>/returned.docx')
@login_required
def proof_returned_docx(round_id):
    """הקובץ שהלקוח שלח בחזרה עם התיקונים שלו בסבב הזה - יכול להיות קובץ
    Word (אם תיקן במחשב) או תמונה/PDF (אם תיקן בכתב יד על דף מודפס
    וצילם/סרק - ראה _proof_returned_kind). שם ה-route עצמו נשאר עם סיומת
    .docx מטעמי תאימות לאחור בלבד - שם ההורדה בפועל תמיד תואם לקובץ האמיתי."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if not round_.customer_file_data:
        abort(404)
    kind = _proof_returned_kind(round_.customer_file_filename)
    if kind == 'docx':
        mimetype = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    else:
        mimetype = mimetypes.guess_type(round_.customer_file_filename or '')[0] or 'application/octet-stream'
    return send_file(
        io.BytesIO(round_.customer_file_data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=round_.customer_file_filename or f'הגהה_{round_id}',
    )


@dictate_bp.route('/proof/<int:round_id>/upload-final', methods=['POST'])
@login_required
def proof_upload_final(round_id):
    """הנציג פתח את הקובץ המקורי בוורד האמיתי, החיל את התיקונים שראה בקובץ
    שהלקוח שלח, ושומר/מעלה כאן את הקובץ הסופי - זה מה שיישלח בסוף ללקוח
    (אם התבקש) ויישמר כעותק הרשמי המתוקן."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status != 'pending':
        return jsonify({'error': 'סבב הגהה זה כבר הושלם'}), 400
    f = request.files.get('final_file')
    if not f or not f.filename:
        return jsonify({'error': 'לא נבחר קובץ'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext != '.docx':
        return jsonify({'error': 'יש להעלות קובץ Word (.docx) בלבד'}), 400
    round_.final_file_data = f.read()
    round_.final_filename = f.filename
    db.session.commit()
    return jsonify({'status': 'ok', 'filename': f.filename})


@dictate_bp.route('/proof/<int:round_id>/save-edited', methods=['POST'])
@login_required
def proof_save_edited(round_id):
    """שומר את התוכן שנערך בעורך המובנה בדפדפן במסך ההגהה (עיצוב מלא -
    מודגש/נטוי/קו תחתון/כותרת, חיפוש-והחלפה, בדיקת איות - ראה
    templates/admin/proof_studio.html). בכל שמירה: (1) שומר את מבנה ה-JSON
    עצמו ב-edited_content, כדי שאם הנציג יחזור למסך מאוחר יותר הוא ימשיך
    מאיפה שהפסיק ולא יתחיל מאפס, וגם (2) בונה קובץ Word אמיתי (docx) מחדש
    מאותו תוכן דרך _build_manuscript_docx - אותו בילדר OOXML/RTL בדיוק
    שמשמש בכל שאר המערכת - ושומר אותו כ-final_file_data. זה מה שיישלח
    בפועל אם/כש-proof_complete יופעל, וגם מה שאפשר להוריד לבדיקה.
    לא נוגע ב-page.content המקורי - זה תמיד נשאר נגיש/משוחזר בנפרד."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status != 'pending':
        return jsonify({'error': 'סבב הגהה זה כבר הושלם - לא ניתן לערוך אותו יותר'}), 400
    page = round_.manuscript_page
    data = request.get_json(silent=True) or {}
    content = data.get('content')
    if not isinstance(content, list):
        return jsonify({'error': 'תוכן לא תקין'}), 400

    # נירמול/הגנה - לא סומכים על מבנה חופשי שמגיע מה-JS בדפדפן
    clean_content = []
    for para in content:
        if not isinstance(para, dict):
            continue
        runs = []
        for run in (para.get('runs') or []):
            if not isinstance(run, dict):
                continue
            text = run.get('text')
            if not isinstance(text, str):
                continue
            runs.append({
                'text': text,
                'bold': bool(run.get('bold')),
                'italic': bool(run.get('italic')),
                'underline': bool(run.get('underline')),
            })
        clean_content.append({'heading': bool(para.get('heading')), 'runs': runs})

    try:
        docx_bytes = _build_manuscript_docx(
            page.customer.name if page.customer else '', page.original_filename, clean_content
        )
    except Exception as e:
        log.error(f"proof save-edited docx build error (round={round_id}): {e}", exc_info=True)
        return jsonify({'error': f'שגיאה ביצירת קובץ ה-Word: {e}'}), 500

    safe_name = os.path.splitext(page.original_filename or 'כתב_יד')[0][:40]
    filename = f'מתוקן_{safe_name}.docx'

    round_.edited_content = clean_content
    round_.final_file_data = docx_bytes
    round_.final_filename = filename
    db.session.commit()
    return jsonify({'status': 'ok', 'filename': filename})


@dictate_bp.route('/proof/<int:round_id>/timer/start', methods=['POST'])
@login_required
def proof_timer_start(round_id):
    """מפעיל את שעון ההגהה (חיוב לפי דקות עבודה בפועל) - ראה
    models.ProofingRound.timer_started_at/timer_accumulated_seconds."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status != 'pending':
        return jsonify({'error': 'סבב הגהה זה כבר הושלם'}), 400
    if round_.timer_started_at is None:
        round_.timer_started_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'status': 'ok', **_proofing_timer_state(round_)})


@dictate_bp.route('/proof/<int:round_id>/timer/stop', methods=['POST'])
@login_required
def proof_timer_stop(round_id):
    """עוצר את שעון ההגהה ומוסיף את הזמן שנצבר במקטע הזה ל-timer_accumulated_seconds.
    אפשר להפעיל ולעצור כמה פעמים (הפסקות) - הזמן מצטבר על פני כל המקטעים."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.timer_started_at is not None:
        elapsed = (datetime.utcnow() - round_.timer_started_at).total_seconds()
        round_.timer_accumulated_seconds = (round_.timer_accumulated_seconds or 0.0) + elapsed
        round_.timer_started_at = None
        db.session.commit()
    return jsonify({'status': 'ok', **_proofing_timer_state(round_)})


@dictate_bp.route('/proof/<int:round_id>/timer/reset', methods=['POST'])
@login_required
def proof_timer_reset(round_id):
    """מאפס את שעון ההגהה לגמרי (לתיקון טעות - למשל שכחו לעצור אתמול) -
    לא זמין אחרי שהסבב כבר הושלם וחויב."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status != 'pending':
        return jsonify({'error': 'סבב הגהה זה כבר הושלם'}), 400
    round_.timer_started_at = None
    round_.timer_accumulated_seconds = 0.0
    db.session.commit()
    return jsonify({'status': 'ok', **_proofing_timer_state(round_)})


@dictate_bp.route('/proof/<int:round_id>/timer/status')
@login_required
def proof_timer_status(round_id):
    """מצב השעון הנוכחי - נקרא בטעינת הדף מחדש (למשל אחרי רענון דפדפן)
    כדי לחשב מחדש את הזמן שחלף לפי מה שבאמת שמור בשרת, לא לפי מה שהיה
    בזיכרון הדפדפן שאבד ברענון."""
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    return jsonify(_proofing_timer_state(round_))


def _hebrew_spellcheck_paragraphs(paragraphs):
    """שולח את הפסקאות ל-Claude לבדיקת איות/דקדוק בעברית ברמה גבוהה, ומחזיר
    רשימת בעיות עם מיקום (אינדקס פסקה) והצעות תיקון. במכוון לא מבקשים
    מהמודל להחזיר offsets מספריים של תווים - מודלי שפה לא אמינים בספירת
    תווים על טקסט ארוך, וטעות קטנה שם תגרום לסימון במקום שגוי לגמרי.
    במקום זה מבקשים רק את המילה/הביטוי השגוי עצמו, וה-JS בצד הדפדפן מאתר
    בעצמו את המיקום המדויק ב-DOM לפי חיפוש טקסט רגיל בתוך אותה פסקה (ראה
    wrapFirstOccurrence ב-proof_studio.html) - הרבה יותר אמין."""
    import anthropic
    import re

    numbered = '\n'.join(f'[[{i}]] {p}' for i, p in enumerate(paragraphs) if p and p.strip())
    if not numbered.strip():
        return []

    prompt = f"""אתה מגיה מקצועי לעברית ברמה גבוהה מאוד. להלן טקסט מחולק לפסקאות ממוספרות (כל פסקה מתחילה בתגית [[מספר]]).

מצא אך ורק שגיאות איות ודקדוק ברורות ומובהקות בעברית. אל תעיר על סגנון, אל תשנה ניסוח, ואל תיגע בשמות פרטיים/מקומות/מונחים זרים אלא אם יש בהם שגיאת כתיב ודאית.

עבור כל שגיאה שמצאת החזר אובייקט עם השדות הבאים:
- paragraph: מספר הפסקה כפי שמופיע ב-[[מספר]] (מספר שלם)
- wrong: המילה או הביטוי השגוי בדיוק כפי שהוא מופיע בטקסט המקורי (אותיות, רווחים וניקוד זהים)
- suggestions: רשימה של 1 עד 3 הצעות תיקון, מהסבירה ביותר לפחות סבירה
- reason: הסבר קצר מאוד (עד 6 מילים) בעברית לסוג השגיאה

החזר אך ורק מערך JSON תקני - בלי שום טקסט נוסף לפני או אחרי, ובלי ```. אם אין שום שגיאה החזר מערך ריק [].

הטקסט לבדיקה:
{numbered}"""

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    response = client.messages.create(
        model='claude-opus-4-5',
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        log.warning(f"spellcheck: לא נמצא JSON בתגובת המודל: {raw[:300]!r}")
        return []
    try:
        issues = json.loads(match.group(0))
    except Exception as e:
        log.warning(f"spellcheck: JSON parse נכשל: {e} raw={raw[:300]!r}")
        return []
    if not isinstance(issues, list):
        return []

    clean_issues = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        try:
            para_idx = int(it.get('paragraph'))
        except (TypeError, ValueError):
            continue
        wrong = it.get('wrong')
        if not isinstance(wrong, str) or not wrong.strip():
            continue
        suggestions = [s for s in (it.get('suggestions') or []) if isinstance(s, str) and s.strip()][:3]
        if not suggestions:
            continue
        reason = it.get('reason')
        clean_issues.append({
            'paragraph': para_idx,
            'wrong': wrong,
            'suggestions': suggestions,
            'reason': reason if isinstance(reason, str) else '',
        })
    return clean_issues


@dictate_bp.route('/proof/<int:round_id>/spellcheck', methods=['POST'])
@login_required
def proof_spellcheck(round_id):
    """בדיקת איות/דקדוק בעברית ברמה גבוהה על התוכן הנוכחי בעורך (AI, לא
    Hunspell/מילון סטטי - הרבה יותר טוב על עברית) - ראה _hebrew_spellcheck_paragraphs."""
    from models import ProofingRound
    ProofingRound.query.get_or_404(round_id)  # מוודא round_id תקין, אותה הרשאה כמו שאר המסך
    data = request.get_json(silent=True) or {}
    paragraphs = data.get('paragraphs')
    if not isinstance(paragraphs, list):
        return jsonify({'error': 'קלט לא תקין'}), 400
    paragraphs = [p if isinstance(p, str) else '' for p in paragraphs][:400]  # הגנת קצה
    try:
        issues = _hebrew_spellcheck_paragraphs(paragraphs)
    except Exception as e:
        log.error(f"spellcheck error (round={round_id}): {e}", exc_info=True)
        return jsonify({'error': f'שגיאה בבדיקת האיות: {e}'}), 500
    return jsonify({'issues': issues})


@dictate_bp.route('/proof/<int:round_id>/final.docx')
@login_required
def proof_final_docx(round_id):
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if not round_.final_file_data:
        abort(404)
    return send_file(
        io.BytesIO(round_.final_file_data),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=round_.final_filename or f'מתוקן_{round_id}.docx',
    )


@dictate_bp.route('/proof/<int:round_id>/complete', methods=['POST'])
@login_required
def proof_complete(round_id):
    """מסיים את סבב ההגהה: עוצר את השעון אוטומטית אם עדיין רץ, מחשב את
    המחיר לפי דקות העבודה שנצברו (מעוגל כלפי מעלה לדקה שלמה) כפול המחיר-
    לדקה בהגדרות, מחייב את הלקוח, ואופציונלית שולח את הקובץ הסופי בחזרה
    ללקוח במייל. התשלום יורד רק כאן - לא בעת קבלת ההגהה מהלקוח - אותה
    פילוסופיה בדיוק כמו send() הרגיל."""
    from models import ProofingRound, Transaction
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status != 'pending':
        return jsonify({'error': 'אין הגהה ממתינה לסבב הזה'}), 400
    if not round_.final_file_data:
        return jsonify({'error': 'יש להעלות/לשמור קודם את קובץ ה-Word הסופי המתוקן'}), 400

    # אם השעון עדיין רץ בעת הסיום - עוצרים אותו אוטומטית לפני החישוב, כדי
    # שלא "יאבד" את הזמן האחרון שעדיין לא נצבר.
    if round_.timer_started_at is not None:
        elapsed = (datetime.utcnow() - round_.timer_started_at).total_seconds()
        round_.timer_accumulated_seconds = (round_.timer_accumulated_seconds or 0.0) + elapsed
        round_.timer_started_at = None

    minutes = _proofing_minutes_billed(round_.timer_accumulated_seconds or 0)
    price_per_minute = _manuscript_proofing_price()
    price = round(minutes * price_per_minute, 2)

    page = round_.manuscript_page
    customer = page.customer if page else None
    # ראה send() למעלה - "מסמכים כלליים" של מוסד מחויבים ישירות מיתרת
    # המוסד, לא מיתרת לקוח-הדמה (שתמיד 0).
    billing_account = customer
    if customer and customer.is_institution_self and customer.institution:
        billing_account = customer.institution
    if price > 0:
        if not billing_account:
            return jsonify({'error': 'לא נמצא לקוח/מוסד משויך - לא ניתן לחייב'}), 400
        if billing_account.balance < price:
            return jsonify({
                'error': f'אין מספיק יתרה לחיוב ההגהה (עלות: ₪{price:.2f} עבור {minutes} דק׳, יתרה נוכחית: ₪{billing_account.balance:.2f}) - '
                         f'יש לטעון יתרה ולנסות שוב',
                'insufficient_balance': True,
                'cost': price,
                'minutes': minutes,
                'balance': billing_account.balance,
            }), 402

    # שליחה בחזרה היא תמיד אופציונלית (send_back) - ללקוח מוסד ממילא לא
    # צריך: סימון "done" מספיק, הוא רואה/מוריד את ההגהה הסופית בעצמו בתיק
    # התלמיד באתר (ראה docstring של send() למעלה, אותה פילוסופיה בדיוק).
    # כשכן מבקשים לשלוח בחזרה (send_back=1) ואין ללקוח מייל - שולחים בפקס
    # (אותם "אנשי הפקס" מ-send() למעלה) אם יש טלפון, במקום לחסום.
    send_back = request.form.get('send_back') == '1'
    to_email = (request.form.get('to_email') or '').strip() or ((customer.email or '').strip() if customer else '')
    is_institution_student = bool(customer and customer.institution_id)
    to_fax_number = ''
    if send_back and not to_email and not is_institution_student and customer:
        to_fax_number = (customer.fax or customer.phone or '').strip()

    if send_back and not to_email and not to_fax_number and not is_institution_student:
        return jsonify({'error': 'אין כתובת מייל או מספר פקס ליעד לשליחה חזרה - הזינו כתובת'}), 400

    sent_via = None
    try:
        if send_back:
            if to_email:
                _send_proofed_manuscript_email(to_email, customer.name if customer else '', page.original_filename, round_.final_file_data)
                sent_via = 'email'
                round_.sent_to = to_email
            elif to_fax_number:
                _send_manuscript_fax(round_, to_fax_number, round_.final_file_data, filename_hint=f'proof_{round_.id}.pdf')
                sent_via = 'fax'
                round_.sent_to = to_fax_number
            # אם send_back=1 אבל הלקוח שייך למוסד וגם לא הוזנה כתובת מייל -
            # לא עושים כלום כאן (הכתובת הייתה ריקה לגיטימית) - עדיין מסמנים
            # done, המוסד יראה/יוריד בעצמו.
            round_.sent_via = sent_via

        if price > 0 and billing_account:
            billing_account.balance -= price
            db.session.add(Transaction(
                customer_id=customer.id,
                amount=-price,
                type='manuscript_proofing',
                description=f'הגהת כתב יד - {page.original_filename} ({minutes} דק׳)' + (' (חויב מיתרת המוסד)' if billing_account is not customer else ''),
            ))
        round_.cost = price
        round_.status = 'done'
        round_.completed_at = datetime.utcnow()
        db.session.commit()
        return jsonify({
            'status': 'done', 'cost': price, 'minutes': minutes,
            'sent': bool(sent_via), 'via': sent_via,
            'to': round_.sent_to if sent_via else None,
        })
    except Exception as e:
        db.session.rollback()
        log.error(f"proofing round complete error (round={round_id}): {e}", exc_info=True)
        return jsonify({'error': f'שגיאה בסיום ההגהה: {e}'}), 500


@dictate_bp.route('/proof/<int:round_id>/delete', methods=['POST'])
@login_required
def delete_proof_request(round_id):
    """מבטל סבב הגהה ממתין (למשל אם הגיע בטעות / קובץ פגום) בלי לחייב את
    הלקוח - מוחק את הסבב לגמרי (לא היה מעולם חלק מהיסטוריית חיוב, אז אין
    צורך לשמר אותו). סבב שכבר הושלם וחויב לא ניתן למחיקה מכאן."""
    from flask import flash, redirect
    from models import ProofingRound
    round_ = ProofingRound.query.get_or_404(round_id)
    if round_.status == 'pending':
        db.session.delete(round_)
        db.session.commit()
        flash('סבב ההגהה בוטל')
    else:
        flash('לא ניתן לבטל סבב הגהה שכבר הושלם')
    return redirect(url_for('dictate.queue', status='proof'))


# ==========================================================================
# פקס נכנס - הלקוח שולח פקס (במקום/בנוסף למייל) למספר המערכת, עם 153
# כקידומת (ראה מודול "קבלת פקסים" של ימות המשיח). ימות מעבירה כל פקס נכנס
# במייל לכתובת ייעודית (routes/email_inbound.py.FAX_INBOUND_EMAIL), ומשם
# הוא נשמר כ-IncomingFax "לא משויך" - כאן הנציג משייך אותו ידנית (בלי OCR
# אוטומטי, לפי החלטה מפורשת) לפי מה שכתוב בעמוד הראשון של הפקס עצמו (טלפון
# + קוד אישי - ראה _ensure_customer_fax_code למטה), או ככתב יד חדש להקראה,
# או כסבב הגהה חדש על כתב יד קיים של אותו לקוח.
# ==========================================================================

def _generate_fax_code():
    """קוד אישי בן 5 ספרות (מספרי בלבד - קל לכתוב ולקרוא על דף), ייחודי בין
    הלקוחות. נוצר עצלנית - לא לכל הלקוחות יש כזה, רק מי שבאמת קיבל הנחיה
    לשלוח פקס (ראה _ensure_customer_fax_code)."""
    from models import Customer
    for _ in range(30):
        candidate = ''.join(random.choices('0123456789', k=5))
        if not Customer.query.filter_by(fax_code=candidate).first():
            return candidate
    raise RuntimeError('לא ניתן היה ליצור קוד פקס פנוי')


def _ensure_customer_fax_code(customer):
    """מחזיר את קוד הפקס האישי של הלקוח - יוצר ושומר אחד חדש אם עדיין אין
    לו (למשל לקוח ותיק, או שליחת מייל ראשונה שלו)."""
    if not customer.fax_code:
        customer.fax_code = _generate_fax_code()
        db.session.commit()
    return customer.fax_code


@dictate_bp.route('/fax/<int:fax_id>/preview-part')
@login_required
def fax_preview_part(fax_id):
    """חלק אחר (20 עמודים) של הפקס - JSON, בלי רענון דף."""
    from models import IncomingFax
    fax = IncomingFax.query.get_or_404(fax_id)
    return _preview_json(_file_to_pdf_preview(
        fax.file_data, fax.filename or 'fax.pdf',
        log_context=f'incoming fax={fax.id}', part=request.args.get('part', 1, type=int)))


@dictate_bp.route('/fax/<int:fax_id>')
@login_required
def fax_assign(fax_id):
    """מסך שיוך פקס נכנס - הנציג רואה את הפקס עצמו (מוצג כ-PDF, בדיוק כמו
    כתב-היד/הקובץ שהתקבל בהגהה - ראה _file_to_pdf_data_uri), מחפש את הלקוח
    לפי הטלפון/קוד שכתובים על הדף, ובוחר האם זה כתב יד חדש להקראה או סבב
    הגהה על כתב יד קיים שלו."""
    from models import IncomingFax
    fax = IncomingFax.query.get_or_404(fax_id)
    pv = _file_to_pdf_preview(fax.file_data, fax.filename or 'fax.pdf',
                              log_context=f'incoming fax={fax.id}', part=request.args.get('part', 1, type=int))
    data_uri, is_pdf = pv['uri'], pv['is_pdf']

    selected_customer = None
    customer_pages = []
    customer_id = request.args.get('customer_id', type=int)
    if customer_id:
        from models import Customer, ManuscriptPage
        selected_customer = Customer.query.get(customer_id)
        if selected_customer:
            # ⚠️ זה ככל הנראה הגורם המרכזי לקריסה/לאיטיות אחרי "נמצא לקוח":
            # התבנית (fax_assign.html) מציגה ברשימת "בחר כתב יד" רק
            # original_filename/created_at/status - אבל בלי defer() מפורש
            # השאילתה מביאה בשביל כל דף גם את שלוש עמודות ה-LargeBinary
            # (file_data/proof_file_data/proof_final_file_data, כל אחת יכולה
            # להיות כמה מגה-בייט) - ללקוח עם היסטוריה של הרבה דפים זה יכול
            # להיות עשרות/מאות מגה-בייט בזיכרון בכל טעינה של המסך הזה, בדיוק
            # ה-timeout/SIGKILL (OOM) שראינו בלוגים.
            customer_pages = (_defer_manuscript_page_blobs(
                ManuscriptPage.query.filter_by(customer_id=selected_customer.id), ManuscriptPage)
                               .order_by(ManuscriptPage.created_at.desc()).all())

    return render_template(
        'admin/fax_assign.html',
        fax=fax,
        data_uri=data_uri,
        is_pdf=is_pdf,
        pv=pv,
        pv_url=url_for('dictate.fax_preview_part', fax_id=fax.id),
        selected_customer=selected_customer,
        customer_pages=customer_pages,
    )


@dictate_bp.route('/fax/customer-search')
@login_required
def fax_customer_search():
    """חיפוש לקוח לפי טלפון או קוד פקס אישי - לשימוש במסך שיוך הפקס
    (fax_assign.html, JS). התאמה חלקית על טלפון, מדויקת על קוד (5 ספרות)."""
    from models import Customer
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'results': []})
    matches = (Customer.query.filter(
        db.or_(Customer.phone.ilike(f'%{q}%'), Customer.fax_code == q)
    ).order_by(Customer.created_at.desc()).limit(15).all())
    return jsonify({'results': [
        {'id': c.id, 'phone': c.phone, 'name': c.name or '', 'fax_code': c.fax_code or ''}
        for c in matches
    ]})


@dictate_bp.route('/fax/customer-verify')
@login_required
def fax_customer_verify():
    """אימות דו-גורמי (טלפון + קוד אישי) לשיוך פקס - **שני** השדות חייבים
    להתאים בדיוק לאותו לקוח, בשונה מ-fax_customer_search למעלה (שמספיק בו
    אחד מהשניים, ומיועד לחיפוש חופשי במקומות אחרים). זו הגנה מכוונת נגד
    מישהו שכותב על הפקס מספר טלפון שאינו שלו (של אדם אחר) כדי לחייב את
    האדם ההוא - קוד הפקס האישי נמסר אך ורק בטלפון (שלוחה 6, הקש 2 - ראה
    routes/api.py get_customer_fax_code), אף פעם לא במייל, כך שרק הלקוח
    האמיתי (שהתקשר בעצמו וקיבל את הקוד בקולו) יכול לספק את שני הפרטים יחד.
    מסך שיוך הפקס (fax_assign.html) משתמש אך ורק בנתיב הזה, לא בחיפוש
    החופשי - שני השדות שם חובה."""
    from models import Customer
    phone = (request.args.get('phone') or '').strip()
    code = (request.args.get('code') or '').strip()
    if not phone or not code:
        return jsonify({'customer': None, 'reason': 'missing_fields'})
    customer = Customer.query.filter_by(phone=phone, fax_code=code).first()
    if not customer:
        return jsonify({'customer': None, 'reason': 'no_match'})
    return jsonify({'customer': {
        'id': customer.id, 'phone': customer.phone, 'name': customer.name or '',
        'fax_code': customer.fax_code or '',
    }})


@dictate_bp.route('/fax/<int:fax_id>/assign-new', methods=['POST'])
@login_required
def fax_assign_new(fax_id):
    """משייך את הפקס ככתב יד חדש להקראה עבור הלקוח שנבחר - נכנס לתור הרגיל
    ('בתור') בדיוק כמו כתב יד שמתקבל במייל (ראה email_inbound._capture_manuscript_page)."""
    from models import IncomingFax, Customer, ManuscriptPage
    fax = IncomingFax.query.get_or_404(fax_id)
    if fax.status != 'pending':
        return jsonify({'error': 'הפקס הזה כבר שויך/טופל'}), 400
    customer_id = request.form.get('customer_id', type=int)
    customer = Customer.query.get(customer_id) if customer_id else None
    if not customer:
        return jsonify({'error': 'יש לבחור לקוח'}), 400

    page = ManuscriptPage(
        customer_id=customer.id,
        original_filename=f"פקס נכנס - {fax.received_at.strftime('%d-%m-%Y') if fax.received_at else ''}".strip(' -'),
        file_data=fax.file_data,
        status='pending',
    )
    db.session.add(page)
    db.session.flush()

    fax.status = 'assigned'
    fax.assigned_customer_id = customer.id
    fax.assigned_manuscript_page_id = page.id
    fax.assigned_at = datetime.utcnow()
    fax.assigned_by = getattr(current_user, 'username', None)
    db.session.commit()
    log.info(f"fax_assign_new: פקס {fax.id} שויך ככתב יד חדש (page={page.id}) ללקוח {customer.id}")
    flash('הפקס שויך ככתב יד חדש - נכנס לתור ההקראה')
    return redirect(url_for('dictate.queue', status='fax'))


@dictate_bp.route('/fax/<int:fax_id>/assign-proof', methods=['POST'])
@login_required
def fax_assign_proof(fax_id):
    """משייך את הפקס כסבב הגהה חדש על כתב יד קיים שנבחר - בדיוק כמו תגובת
    הגהה שמגיעה במייל (ראה email_inbound._handle_proofing_reply /
    ProofingRound)."""
    from models import IncomingFax, Customer, ManuscriptPage, ProofingRound
    fax = IncomingFax.query.get_or_404(fax_id)
    if fax.status != 'pending':
        return jsonify({'error': 'הפקס הזה כבר שויך/טופל'}), 400
    customer_id = request.form.get('customer_id', type=int)
    page_id = request.form.get('page_id', type=int)
    customer = Customer.query.get(customer_id) if customer_id else None
    if not customer:
        return jsonify({'error': 'יש לבחור לקוח'}), 400
    page = ManuscriptPage.query.filter_by(id=page_id, customer_id=customer.id).first() if page_id else None
    if not page:
        return jsonify({'error': 'יש לבחור כתב יד קיים של הלקוח הזה'}), 400

    round_ = ProofingRound(
        manuscript_page_id=page.id,
        status='pending',
        customer_file_data=fax.file_data,
        customer_file_filename=fax.filename or f'פקס_{fax.id}.pdf',
        requested_at=datetime.utcnow(),
    )
    db.session.add(round_)
    db.session.flush()

    fax.status = 'assigned'
    fax.assigned_customer_id = customer.id
    fax.assigned_proofing_round_id = round_.id
    fax.assigned_at = datetime.utcnow()
    fax.assigned_by = getattr(current_user, 'username', None)
    db.session.commit()
    log.info(f"fax_assign_proof: פקס {fax.id} שויך כסבב הגהה חדש (round={round_.id}) על כתב יד {page.id}")
    flash('הפקס שויך כסבב הגהה חדש')
    return redirect(url_for('dictate.queue', status='fax'))


@dictate_bp.route('/fax/<int:fax_id>/ignore', methods=['POST'])
@login_required
def fax_ignore(fax_id):
    """מסמן פקס כלא-רלוונטי (למשל עמוד ריק/לא קריא/פקס שגוי) בלי לשייך אותו
    לאף לקוח - נשאר בתיעוד ההיסטורי (IncomingFax) עם הערה, לא נמחק."""
    from models import IncomingFax
    fax = IncomingFax.query.get_or_404(fax_id)
    if fax.status == 'pending':
        fax.status = 'ignored'
        fax.note = (request.form.get('note') or '').strip()[:500] or None
        fax.assigned_at = datetime.utcnow()
        fax.assigned_by = getattr(current_user, 'username', None)
        db.session.commit()
        flash('הפקס סומן כלא-רלוונטי')
    return redirect(url_for('dictate.queue', status='fax'))


@dictate_bp.route('/fax/<int:fax_id>/download')
@login_required
def fax_download(fax_id):
    """הורדת קובץ הפקס הגולמי כפי שהתקבל מימות - לפתיחה חיצונית אם התצוגה
    המוטמעת לא ברורה מספיק."""
    from models import IncomingFax
    fax = IncomingFax.query.get_or_404(fax_id)
    if not fax.file_data:
        abort(404)
    mimetype = mimetypes.guess_type(fax.filename or '')[0] or 'application/pdf'
    return send_file(
        io.BytesIO(fax.file_data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=fax.filename or f'פקס_{fax_id}.pdf',
    )


