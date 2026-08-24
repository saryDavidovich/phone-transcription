"""
תיווך (Proxy) להורדת קובצי הקלטה.

הבעיה: קישור ההורדה של ההקלטה שנשלח ללקוח (בתוך המייל) היה עד כה כתובת
ה-URL "הגולמית" של ימות המשיח עצמה - וזו כוללת בתוכה טוקן/שם-משתמש+סיסמה
שמאפשרים גישה למערכת ימות המשיח שלנו. כל מי שמקבל את המייל (או שהמייל
מודלף/מועבר הלאה) יכול להשתמש באותו טוקן כדי לגשת ישירות ל-API של ימות
המשיח ולבצע שם פעולות שרירותיות (כולל מחיקה) - לא רק להוריד את ההקלטה שלו.

הפתרון: הלקוח מקבל קישור לשרת שלנו בלבד (`/dl/rec/<טוקן חתום>`) - הטוקן
חתום קריפטוגרפית (עם ה-SECRET_KEY של האפליקציה) ולא ניתן לזיוף, אבל אינו
מכיל ואינו חושף שום פרט על ימות המשיח (לא טוקן, לא שם משתמש/סיסמה, לא אפילו
את כתובת ה-URL האמיתית). כשהלקוח לוחץ על הקישור - השרת שלנו הוא זה שפונה
בפועל לימות המשיח (בצד שרת, עם הפרטים האמיתיים ששמורים אצלנו ב-DB/משתני
סביבה) ומזרים את קובץ ההקלטה חזרה ללקוח. כך מערכת ימות המשיח שלנו לעולם
לא נחשפת ללקוח הקצה.

עדכון 1 (אומת בפועל): מגישים עמוד HTML עם הקובץ מוטמע כ-data: URI, ולא תגובת
קובץ ישירה - כדי שלא ייחסם ע"י סינון תוכן שמזהה הורדות בינאריות (כמו נטפרי).

עדכון 2 (אומת בפועל): גם עמוד ה-data: URI עצמו, כשהוא גדול (הקלטה של כמה
שניות -> ~175KB אחרי base64), הגיע קצוץ למשתמש הסופי - למרות שהשרת שלנו
קיבל/שלח את הכל במלואו (אומת בלוגים). ההשערה: מתווך שלא מריץ JavaScript
(למשל בוט/סורק קישורים אוטומטי) קורא רק חלק מהתגובה. הפתרון: דו-שלבי -
עמוד HTML ראשוני קטן וקליל בלי שום תוכן כבד (download_recording), ורק
לאחר טעינתו בדפדפן אמיתי, JavaScript מושך בנפרד את תוכן הקובץ כ-JSON
(download_recording_data) ובונה ממנו קובץ מקומי (Blob) - ראו הסבר מפורט
ב-render_lazy_download_page שב-download_utils.py.
"""
import os
import time
import logging
import requests
from flask import Blueprint, abort, current_app
from itsdangerous import URLSafeSerializer, BadSignature

log = logging.getLogger(__name__)

download_bp = Blueprint('download', __name__)

_SALT = 'recording-download-v1'

# User-Agent "רגיל" של דפדפן - כמה שרתים/הגנות אנטי-בוט (וכנראה גם ימות
# המשיח, בהתבסס על שגיאת 418 שראינו בנתיב אחר שפונה אליהם בלי User-Agent
# בכלל - requests שולח ברירת מחדל כמו "python-requests/2.x") מזהים בקשות
# שרת-לשרת "חשודות" ומגיבים בצורה חריגה - חסימה מוחלטת (418) או, כפי
# שנצפה בפועל כאן, ניתוק החיבור באמצע התגובה בלי שגיאה גלויה (הקובץ
# שמתקבל תקין אבל קצר מהמוצהר בכותרת ה-WAV שלו).
_YEMOT_REQUEST_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
}


def _wav_looks_truncated(content):
    """בודק אם קובץ WAV חסר בייטים ביחס למה שהכותרת שלו (chunk 'data')
    מצהירה - זה בדיוק הבאג שנצפה בפועל: קובץ שהתקבל מימות המשיח תקין
    מבחינת המבנה, אבל קצר משמעותית מהאורך שהכותרת שלו טוענת, כאילו
    החיבור נותק/נחתך באמצע השליחה. מחזיר False אם אי אפשר לפרש (לא
    בהכרח WAV, או קובץ פגום מסיבה אחרת) - לא רוצים לחסום הורדה על בסיס
    בדיקה שלא בטוחים בה."""
    try:
        if len(content) < 44 or content[0:4] != b'RIFF' or content[8:12] != b'WAVE':
            return False
        pos = 12
        while pos + 8 <= len(content):
            chunk_id = content[pos:pos + 4]
            chunk_size = int.from_bytes(content[pos + 4:pos + 8], 'little')
            if chunk_id == b'data':
                declared_end = pos + 8 + chunk_size
                # קצת סלאק (1KB) כדי לא להיתקע על הבדלי padding זניחים
                return declared_end > len(content) + 1024
            pos += 8 + chunk_size + (chunk_size % 2)
    except Exception:
        pass
    return False


def _fetch_wav_plain(url, recording_id, label):
    """בדיוק אותה שיטת הורדה שמשמשת בפועל, ובאופן מוכח עובד, את צינור
    התמלול (services/transcribe.py:_gemini_from_url) - קריאת requests.get
    פשוטה יחידה, בלי User-Agent מיוחד, בלי ניסיון חוזר, timeout ארוך.
    ההיגיון (לפי הצעת המשתמש): אם ההורדה לצורך תמלול - שקורית מיד אחרי סיום
    השיחה, מאותה כתובת rec_url בדיוק - תמיד מביאה קובץ שלם (אחרת התמלולים
    היו יוצאים חתוכים), אז יכול להיות שהתוספות שלנו (User-Agent מזויף,
    timeout קצר יותר, ניסיון חוזר מהיר) הן עצמן הגורם לבעיה, ולא פתרון לה.
    מחזיר (content, content_type) תמיד (גם אם קצוץ - משאירים את ההחלטה
    למי שקורא לפונקציה), או None אם הבקשה נכשלה לגמרי."""
    try:
        upstream = requests.get(url, timeout=300)
        upstream.raise_for_status()
    except Exception as e:
        log.warning(f'Recording download proxy [{label}]: recording {recording_id} '
                    f'(plain fetch, like transcription) failed fetching {url!r}: {e}')
        return None
    content = upstream.content
    content_type = upstream.headers.get('Content-Type') or 'audio/wav'
    declared_len = upstream.headers.get('Content-Length')
    truncated = _wav_looks_truncated(content)
    log.info(f'Recording download proxy [{label}]: recording {recording_id} (plain fetch, like '
             f'transcription) - status={upstream.status_code}, declared Content-Length={declared_len!r}, '
             f'actual received={len(content)} bytes, looks_truncated={truncated}')
    return content, content_type


def _fetch_wav(url, recording_id, label):
    """מנסה להביא קובץ הקלטה מכתובת נתונה, עם ניסיון חוזר אחד אם הקובץ
    שחוזר נראה קצוץ. מחזיר (content, content_type) בהצלחה, או None בכישלון
    (עם לוג מפורט שמסביר בדיוק למה - קוד שגיאה/חריגה - כדי שאפשר יהיה
    לאבחן מה בדיוק השתבש בלי לנחש)."""
    for attempt in (1, 2):
        try:
            upstream = requests.get(url, timeout=120, headers=_YEMOT_REQUEST_HEADERS)
            upstream.raise_for_status()
        except Exception as e:
            log.warning(f'Recording download proxy [{label}]: recording {recording_id} attempt {attempt} '
                        f'failed fetching {url!r}: {e}')
            if attempt == 1:
                time.sleep(1)
                continue
            return None
        content = upstream.content
        content_type = upstream.headers.get('Content-Type') or 'audio/wav'
        declared_len = upstream.headers.get('Content-Length')
        if not _wav_looks_truncated(content):
            log.info(f'Recording download proxy [{label}]: recording {recording_id} attempt {attempt} OK - '
                     f'status={upstream.status_code}, url={url!r}, declared Content-Length={declared_len!r}, '
                     f'actual received={len(content)} bytes')
            return content, content_type
        # אבחון קריטי: האם ימות בעצמם כבר הצהירו על גודל קטן (כלומר זה כל מה
        # שהיה להם לתת לנו באותו רגע - תומך בכיוון של תזמון/קובץ שעדיין לא
        # נגמר להיכתב אצלם), או שהם הצהירו על הגודל המלא אבל בפועל קיבלנו
        # פחות בייטים מזה (תומך בכיוון של חיבור שנחתך באמצע - תקלת רשת/תשתית)?
        mismatch = ''
        try:
            if declared_len is not None and int(declared_len) != len(content):
                mismatch = (f' *** MISMATCH: server declared {declared_len} bytes but we only '
                            f'received {len(content)} - connection cut mid-transfer ***')
            elif declared_len is not None:
                mismatch = f' (server itself only declared {declared_len} bytes - this is genuinely all they sent)'
        except (TypeError, ValueError):
            pass
        log.warning(f'Recording download proxy [{label}]: recording {recording_id} attempt {attempt} '
                    f'looks truncated - status={upstream.status_code}, url={url!r}, '
                    f'declared Content-Length={declared_len!r}, actual received={len(content)} bytes{mismatch}')
        if attempt == 1:
            time.sleep(2)
            continue
        # אחרי 2 ניסיונות עדיין קצוץ - עדיף להחזיר את מה שיש (חלקי) מאשר
        # כלום, במיוחד אם זה מקור הגיבוי האחרון שיש לנו
        return content, content_type
    return None


def _serializer(app=None):
    app = app or current_app
    return URLSafeSerializer(app.config['SECRET_KEY'], salt=_SALT)


def recording_download_url(recording_id):
    """בונה קישור להורדת הקלטה דרך השרת שלנו - לא חושף שום פרט על ימות המשיח.
    יש לקרוא לפונקציה הזו מתוך app context פעיל (כל מקומות הקריאה כבר
    רצים בתוך with app.app_context())."""
    if not recording_id:
        return ''
    base = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    token = _serializer().dumps({'rid': int(recording_id)})
    return f'{base}/dl/rec/{token}'


def _resolve_recording_id(token):
    """מפענח את הטוקן, בודק שההקלטה קיימת, ומחזיר (recording_id, rec) או
    שקורא ל-abort(404) בעצמו אם משהו לא תקין."""
    from models import Recording
    try:
        data = _serializer().loads(token)
        recording_id = int(data.get('rid'))
    except (BadSignature, ValueError, TypeError, AttributeError, KeyError):
        abort(404)
    rec = Recording.query.get(recording_id)
    if not rec:
        log.warning(f'Recording download proxy: recording {recording_id} not found')
        abort(404)
    return recording_id, rec


def _fetch_recording_audio(recording_id, rec):
    """מביאה את בייטי ההקלטה בפועל מימות המשיח, ומחזירה (content, content_type).
    קוראת ל-abort(502) אם אף מקור לא החזיר תוצאה שימושית."""
    # ניסיון ראשון ועיקרי: בדיוק אותה שיטה שמוכחת עובדת בפועל בצינור התמלול
    # (services/transcribe.py:_gemini_from_url) - rec.rec_url, requests.get
    # פשוט בלי כותרות מיוחדות. אם זה מחזיר קובץ שלם - זהו, סיימנו.
    plain_result = None
    if rec.rec_url:
        plain_result = _fetch_wav_plain(rec.rec_url, recording_id, 'recording-url-plain')

    if plain_result and not _wav_looks_truncated(plain_result[0]):
        log.info(f'Recording download proxy: recording {recording_id} served via [recording-url-plain]')
        return plain_result

    # השיטה ה"פשוטה" נכשלה/החזירה קובץ קצוץ - עוברים לרשימת מקורות גיבוי
    # (עם כותרות/ניסיון חוזר), כדי שלקוח לעולם לא יקבל דף שגיאה כל עוד יש
    # לנו איזושהי דרך חלופית להביא את ההקלטה:
    # 1. YEMOT_TOKEN ממשתנה סביבה - זו בדיוק הכתובת/האימות שהיו בשימוש
    #    במקום אחר בקוד (admin.send_recordings, לפני שהוחלף בפרוקסי הזה)
    #    ועבדו בפועל בפרודקשן לאורך זמן.
    # 2. yemot_token מהגדרות ה-DB (בפורמט "מספר_מערכת:סיסמה") - פחות בטוח
    #    שמתאים ל-DownloadFile (הוגדר במקור עבור GetTextFile), אבל שווה
    #    ניסיון אם משתנה הסביבה לא מוגדר.
    # 3. rec.rec_url שוב, אבל עם כותרות + ניסיון חוזר (למקרה שזה כן עוזר).
    env_token = os.environ.get('YEMOT_TOKEN', '')
    from routes.admin import get_setting
    setting_token = get_setting('yemot_token', '')

    candidates = []
    if rec.call_id and env_token:
        candidates.append(('env-token', f'https://www.call2all.co.il/ym/api/DownloadFile'
                                         f'?token={env_token}&path=ivr2:/recordings/{rec.call_id}.wav'))
    if rec.call_id and setting_token and setting_token != env_token:
        candidates.append(('setting-token', f'https://www.call2all.co.il/ym/api/DownloadFile'
                                             f'?token={setting_token}&path=ivr2:/recordings/{rec.call_id}.wav'))
    if rec.rec_url:
        candidates.append(('recording-url-retry', rec.rec_url))

    result = None
    for label, url in candidates:
        result = _fetch_wav(url, recording_id, label)
        if result and not _wav_looks_truncated(result[0]):
            log.info(f'Recording download proxy: recording {recording_id} served via [{label}]')
            break
        result = None

    if not result:
        # אף מקור לא החזיר קובץ שלם - עדיף להחזיר את התוצאה ה"פשוטה" (אם יש,
        # גם אם קצוצה) מאשר שגיאה גמורה ללקוח
        if plain_result:
            log.error(f'Recording download proxy: recording {recording_id} - all sources truncated/failed, '
                      f'falling back to (truncated) plain-fetch result rather than an error page')
            result = plain_result
        else:
            log.error(f'Recording download proxy: recording {recording_id} - ALL sources failed, giving up')
            abort(502)

    return result


@download_bp.route('/dl/rec/<token>')
def download_recording(token):
    """שלב 1: עמוד HTML קטן וקליל בלבד (בלי הקובץ בפנים!) - ראו הסבר מפורט
    ב-render_lazy_download_page שב-download_utils.py. הדפדפן של הלקוח הוא
    זה שיריץ את ה-JavaScript שמושך את תוכן הקובץ בפועל, מה-route השני
    למטה (dl/rec/<token>/data)."""
    recording_id, rec = _resolve_recording_id(token)  # מוודא תקינות מוקדם, גם אם לא בשימוש ישיר כאן
    from routes.download_utils import render_lazy_download_page
    return render_lazy_download_page(
        fetch_url=f'/dl/rec/{token}/data',
        page_title='ההקלטה מוכנה להורדה',
        loading_text='מביאים את ההקלטה מימות המשיח...',
    )


@download_bp.route('/dl/rec/<token>/data')
def download_recording_data(token):
    """שלב 2: נטען רק ע"י JavaScript מתוך הדף שב-download_recording, לא
    ע"י בקשת ניווט ישירה של הדפדפן. מחזיר JSON עם הקובץ מקודד ב-base64 -
    לא HTML גדול, ולא תגובת attachment בינארית (משתי הסיבות שכבר טיפלנו
    בהן: לא רוצים שסורק שלא מריץ JS יראה/יקטע תוכן כבד, ולא רוצים תגובת
    קובץ בינארי גולמית שסינון תוכן עלול לחסום)."""
    import base64
    from flask import jsonify

    recording_id, rec = _resolve_recording_id(token)
    content, content_type = _fetch_recording_audio(recording_id, rec)

    return jsonify({
        'filename': f'הקלטה {recording_id}.wav',
        'mimetype': content_type,
        'b64': base64.b64encode(content).decode('ascii'),
    })
