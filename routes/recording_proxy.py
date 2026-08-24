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

עדכון (אומת בפועל): מגישים עמוד HTML עם הקובץ מוטמע כ-data: URI, ולא תגובת
קובץ ישירה - ראו הסבר מפורט ב-render_data_uri_download_page שב-download_utils.py.
זה מה שגורם לנטפרי (ושירותי סינון תוכן דומים) לא לחסום את ההורדה.
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
        if not _wav_looks_truncated(content):
            return content, content_type
        log.warning(f'Recording download proxy [{label}]: recording {recording_id} attempt {attempt} '
                    f'looks truncated ({len(content)} bytes)')
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


@download_bp.route('/dl/rec/<token>')
def download_recording(token):
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

    # רשימת כתובות מועמדות לניסיון, לפי סדר עדיפות - עוברים לבאה בתור רק אם
    # הקודמת נכשלת/מחזירה שגיאה, כדי שלקוח לעולם לא יקבל דף שגיאה כל עוד יש
    # לנו איזושהי דרך חלופית להביא את ההקלטה:
    # 1. YEMOT_TOKEN ממשתנה סביבה - זו בדיוק הכתובת/האימות שהיו בשימוש
    #    במקום אחר בקוד (admin.send_recordings, לפני שהוחלף בפרוקסי הזה)
    #    ועבדו בפועל בפרודקשן לאורך זמן.
    # 2. yemot_token מהגדרות ה-DB (בפורמט "מספר_מערכת:סיסמה") - פחות בטוח
    #    שמתאים ל-DownloadFile (הוגדר במקור עבור GetTextFile), אבל שווה
    #    ניסיון אם משתנה הסביבה לא מוגדר.
    # 3. rec.rec_url - כתובת ה-RecordingUrl הדינמית שימות שולחים ב-webhook.
    #    נמצא בבדיקה בפועל כלא אמינה (מחזירה קבצים קצוצים) - לכן אחרונה
    #    בעדיפות, אבל עדיין עדיפה על שום תוצאה.
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
        candidates.append(('recording-url', rec.rec_url))

    if not candidates:
        log.warning(f'Recording download proxy: recording {recording_id} - no way to fetch '
                    f'(call_id={rec.call_id!r}, env_token configured={bool(env_token)}, '
                    f'setting_token configured={bool(setting_token)}, rec_url={rec.rec_url!r})')
        abort(404)

    result = None
    for label, url in candidates:
        result = _fetch_wav(url, recording_id, label)
        if result:
            log.info(f'Recording download proxy: recording {recording_id} served via [{label}]')
            break

    if not result:
        log.error(f'Recording download proxy: recording {recording_id} - ALL {len(candidates)} '
                  f'source(s) failed, giving up')
        abort(502)

    content, content_type = result

    from routes.download_utils import render_data_uri_download_page
    return render_data_uri_download_page(
        content,
        hebrew_filename=f'הקלטה {recording_id}.wav',
        mimetype=content_type,
        page_title='ההקלטה מוכנה להורדה',
    )
