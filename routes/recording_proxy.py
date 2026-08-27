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
קיבל/שלח את הכל במלואו (אומת בלוגים). ניסינו דף HTML קטן + JSON אחד גדול
שה-JavaScript מביא בנפרד (עדיין נחתך - אומת עם כלי הפיתוח של הדפדפן:
Size בפועל היה חלק קטן ממה שהיה צריך, אף שהשרת שלח הכל).

עדכון 3 (אז "ננטש", עכשיו מוחזר כרשת ביטחון - ראו עדכון 5): פיצול להורדה
בהרבה חתיכות JSON קטנות (~9KB כל אחת) עם הרכבה מחדש ב-JavaScript. בזמנו
עבר לצד כי המשתמש ביקש לחזור לגישה הפשוטה והסטנדרטית (עדכון 4) - אבל זו
התבררה כחסומה לגמרי (ראו שם), אז חתיכות ה-JSON חוזרות עכשיו כרשת ביטחון.

עדכון 4 (נבדק ואומת בפרודקשן - זה גרם לחסימה!): ניסינו הורדת קובץ **רגילה
וסטנדרטית** - send_file עם Content-Disposition: attachment ושם עברי תקין,
מוגש ישירות מהזיכרון (BytesIO), בדיוק כמו כל הורדת קובץ רגילה משרת.
**המשתמש אישר בפועל שזה נחסם ע"י נטפרי** ("צדקת יש חסימה של נטפרי"), בדיוק
כמו הבעיה המקורית שהתחלנו איתה לפני שעברנו ל-data: URI. זה מאשר במאה אחוז:
נטפרי חוסמת תגובת attachment בינארית, נקודה - וזו לא הייתה הסיבה לקיצוץ
שראינו קודם עם data: URI/JSON (שם לא הייתה חסימה גלויה בכלל, רק תוכן קצוץ
בשקט - תופעה שונה, גורם שונה). מסקנה: attachment פסול לגמרי כדרך הגשה,
לא משנה מי בונה אותו.

עדכון 5 (הפתרון הנוכחי) - proxy שקוף מול שירות Node.js נפרד: הפרויקט הזה
מורכב בפועל משני שירותים נפרדים בפריסה - זה (Flask/Python, מטפל בכל מה
שאינו שיחות ימות עצמן) ו-`phone-transcription-ivr` (Node.js/Express, שירות
נפרד שכן מדבר ישירות עם ימות המשיח לאורך כל השיחה). ההשערה המובילה לקיצוץ
בעדכון 2: כל הניסיונות שם חלקו תכונה משותפת - הפייתון עצמו בנה מחרוזת
תגובה שלמה בזיכרון (HTML/JSON עם base64) ואז שלח אותה כתגובה אחת גדולה עם
Content-Length ידוע מראש; משהו ב-gunicorn (worker סינכרוני, יחד עם
threading.Thread של התמלול באותו תהליך) קוטע כתיבות חוסמות גדולות כאלה.
נוד ג'יי אס לא סבל מאותה תופעה בפרויקט נפרד (הפורומים) עם תבנית תגובה
זהה. הפתרון: הפייתון (שהדומיין שלו כבר סומך עליו נטפרי, ולכן נשאר הכתובת
היחידה שהלקוח בדפדפן פונה אליה) מעביר את בניית עמוד ההורדה בפועל לנוד
(נקודת קצה פנימית `/internal/download-page`, שרת-לשרת בלבד, מוגנת בסוד
משותף INTERNAL_PROXY_SECRET - הלקוח בדפדפן לעולם לא נוגע בכתובת הזו או
בדומיין של הנוד), ורק **מזרים** (streaming, עם requests(stream=True) +
iter_content, בלי buffering מלא ובלי Content-Length ידוע מראש - כתובת
chunked-transfer אמיתית) את מה שהנוד מחזיר ישירות ללקוח. אם ה-relay לנוד
נכשל מכל סיבה (לא מוגדר / timeout / שגיאת רשת / סטטוס לא תקין) - נופלים
בחזרה לרשת הביטחון: חתיכות JSON קטנות (עדכון 3), שלא נבדקו סופית מול הבאג
המקורי אבל בטוח יותר לא-חסום מ-attachment, ולא נבנות כמחרוזת ענקית אחת.

עדכון 6: לאחר פריסת עדכון 5 נצפה מקרה חדש - הקלטה ירדה בשלמות (לא נחסמה,
לא נחתכה ברשת) אבל הייתה קצרה בפועל (3 שניות, רק תחילת ההודעה) ביחס לאורך
השיחה האמיתי. השערת המשתמש: ימות המשיח לא תמיד "מספיקים לסיים לכתוב" את
קובץ ההקלטה בדיוק ברגע שאנחנו מבקשים אותו, כך שהתשובה שחוזרת תקינה
ועקבית מבחינת הכותרת שלה (data chunk לא גדול ממה שבאמת קיבלנו - אין כאן
"נחתך באמצע השליחה" כמו בעדכונים 1-4) אבל פשוט קצרה מכיוון שזה כל מה
שהיה מוכן באותו רגע. הפתרון: מעבירים לנוד את משך השיחה האמיתי (מ-
Recording.duration_seconds ב-DB, ב-payload['expected_duration_seconds']),
והנוד (fetchOnceWithRetries ב-ivr.js) משווה את אורך ה-WAV שהתקבל בפועל
(לפי fmt.byteRate ו-data chunk, לא לפי מה שהכותרת רק "מצהירה") מול האורך
הצפוי - ואם קצר משמעותית (יותר משתי שניות), ממתין (0/2/4 שניות בין
ניסיונות) ומנסה שוב, עד 3 ניסיונות לכל כתובת, ותמיד מחזיר את הניסיון
הכי-טוב שהתקבל (גם אם אף אחד לא היה מושלם) במקום שגיאה. נבדק בפועל מול
שרת מדומה: גם תרחיש שבו הקובץ "נהיה מוכן" רק בניסיון השלישי (מחזיר את
הגרסה המלאה בהצלחה), וגם תרחיש שבו הוא אף פעם לא נהיה מוכן (מחזיר את
הגרסה הכי טובה שהייתה בלי לקרוס). אם duration_seconds לא ידוע/0 - הבדיקה
הזו פשוט מדלגת, וממשיכים כמו בעדכון 5 (רק בדיקת "נחתך באמצע").

עדכון 7 (נבדק בפועל מול לוגים אמיתיים - הפריך את השערת עדכון 5!): לאחר
פריסת עדכון 6, בדיקה עם הקלטה של 15 שניות עדיין יצאה פחות משנייה אצל
הלקוח. הלוגים האמיתיים (Railway) הראו משהו מכריע: `fetchOnceWithRetries
[primary-url] attempt 1: declared Content-Length=254604, actual
received=254604 bytes, wav duration=15.91s, expected duration=15s,
looks_truncated=false` - כלומר **השליפה מימות הצליחה במלואה כבר בניסיון
הראשון**, ומיד אחר כך `routes.recording_proxy: ... streaming response via
Node relay` - כלומר גם הפייתון קיבל מהנוד תגובה תקינה (200) והתחיל להזרים
אותה. ובכל זאת הלקוח קיבל פחות משנייה. זה מוכיח בוודאות: הבעיה **איננה**
בשליפה מימות (תמיד הייתה שלמה), **ואיננה** קשורה ל"האם הפייתון בונה
מחרוזת שלמה בזיכרון לפני השליחה מול מזרים אותה" - כי גם הזרמה (streaming
אמיתי, בלי buffering, בדיוק כמו שעדכון 5 הציע) עדיין הגיעה קצוצה. הפתרון
שנוסה: מפרידים בין "מי שולף מימות" (הנוד, `/internal/fetch-recording-bytes`
- הוכח אמין) לבין "איך מגישים ללקוח" (הרבה חתיכות JSON קטנות ונפרדות,
render_chunked_download_page מעדכון 3).

עדכון 8 (הפתרון הנוכחי - נבדק ואומת בנפרד לפני שהוכנס לכאן): גם הרבה
חתיכות JSON קטנות (עדכון 7) נבדקו בפרודקשן על מערכת אחרת לגמרי (פרויקט
הפורומים, Next.js, שרת/דומיין נפרדים - כדי לבודד את הבעיה) - ועדיין
הגיעו קצוצות אצל הלקוח, על אף שהשרת שם חישב ושלח את הגודל הנכון במלואו
(אומת ישירות על המסך: "גודל בפועל: 119,884 בייטים" - ועדיין ירדה הקלטה
של פחות משנייה). זה הוכיח שהבעיה **איננה** תשתית Python/gunicorn/Railway
הספציפית שלנו, וגם **איננה** קשורה ל"ניווט ישיר מול fetch מדף שכבר טעון" -
היא חוזרת בדיוק אותו דבר בכל וריאציה שבה הקובץ **מוטמע כ-data: URI בתוך
HTML/JSON** (עדכונים 1-3, 6, 7 - כל התגובות ה"חסומות בשקט" האלה חלקו
בדיוק את התכונה הזו). מה שלא נבדק אף פעם עד עכשיו: תגובת HTTP בינארית
אמיתית ורגילה של קובץ אודיו - Content-Type: audio/wav, Content-Disposition:
inline (לא attachment - attachment כבר אומת כחסום ע"י נטפרי, ראו עדכון 4;
inline הוא צורה שונה לגמרי מבחינת סינון תוכן, בדיוק כמו קישור רגיל לקובץ
אודיו/מדיה שכבר עובד בכל אתר אחר). נבדק בנפרד לפני ההטמעה כאן (פרויקט
מבודד, שרת Next.js אמיתי, קובץ WAV אמיתי של 15 שניות בדיוק, גם curl וגם
דפדפן Chromium אמיתי) - הגיע שלם, בייט-בבייט זהה למקור, עם הכותרות
הנכונות. הפתרון: `download_recording` למטה כבר לא מגישה שום עמוד HTML/
JSON - היא מביאה את בייטי ההקלטה (דרך `_get_or_fetch_cached_audio`, שהיא
עצמה נשארת ללא שינוי - הנוד `_fetch_via_node` והבדיקת האורך מעדכון 6-7
עדיין תקפות ומועילות, הן לא היו הבעיה) ומחזירה אותם ישירות ללקוח דרך
`plain_audio_response` (ב-download_utils.py) - קישור אחד, לחיצה אחת, בלי
שום שלב ביניים שהמשתמש צריך להבין או לבנות בעצמו.
"""
import os
import time
import logging
import tempfile
import requests
from flask import Blueprint, abort, current_app
from itsdangerous import URLSafeSerializer, BadSignature

log = logging.getLogger(__name__)

download_bp = Blueprint('download', __name__)

_SALT = 'recording-download-v1'

# תיקיית מטמון זמנית לבייטי הקלטה שכבר הובאו מימות, לצורך רשת הביטחון
# (הורדה מחולקת לחתיכות קטנות - ראו _get_or_fetch_cached_audio ו-
# download_recording_chunk למטה). דיסק מקומי (לא משתנה זיכרון) כי gunicorn
# רץ עם כמה worker processes נפרדים (--workers 3) שלא חולקים זיכרון Python
# ביניהם - אבל כן חולקים את אותה מערכת קבצים מקומית (כולם על אותו קונטיינר).
_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'rec_download_cache')
_CHUNK_CHARS = 12000  # תווי base64 לכל חתיכה (~9KB בפועל אחרי פענוח)

# הגדרות ה-proxy השקוף מול שירות ה-Node.js (phone-transcription-ivr) - ראו
# "עדכון 5" בראש הקובץ. שני משתני הסביבה האלה חייבים להיות מוגדרים (ולהיות
# זהים בין שני השירותים עבור הסוד) כדי שנתיב הנוד יופעל בכלל - אם אחד מהם
# חסר, פשוט נופלים בשקט לרשת הביטחון (חתיכות JSON).
_NODE_IVR_URL = os.environ.get('NODE_IVR_URL', '').rstrip('/')
_INTERNAL_PROXY_SECRET = os.environ.get('INTERNAL_PROXY_SECRET', '')

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
    קוראת ל-abort(502) אם אף מקור לא החזיר תוצאה שימושית. משמשת רק את רשת
    הביטחון (חתיכות JSON) - כשה-relay לנוד עובד, הנוד הוא זה ששולף בעצמו."""
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


def _cache_paths(recording_id):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    bin_path = os.path.join(_CACHE_DIR, f'{recording_id}.bin')
    type_path = os.path.join(_CACHE_DIR, f'{recording_id}.type')
    return bin_path, type_path


def _get_or_fetch_cached_audio(recording_id, rec):
    """מביאה את בייטי ההקלטה, עם מטמון על דיסק מקומי (משותף בין כל
    worker processes של gunicorn על אותו קונטיינר) - כדי שכל חתיכה שנבקש
    בהמשך לא תצטרך לפנות שוב לימות המשיח מההתחלה. מנסה קודם את הנוד
    (_fetch_via_node - שולף אמין עם בדיקת אורך + ניסיון חוזר, ראו "עדכון 7"
    בראש הקובץ), ורק אם זה לא זמין/נכשל נופלת לשליפה הישירה של הפייתון
    (_fetch_recording_audio)."""
    bin_path, type_path = _cache_paths(recording_id)
    if os.path.exists(bin_path) and os.path.exists(type_path):
        try:
            with open(bin_path, 'rb') as f:
                content = f.read()
            with open(type_path, 'r', encoding='utf-8') as f:
                content_type = f.read().strip() or 'audio/wav'
            if content:
                return content, content_type
        except Exception as e:
            log.warning(f'Recording download proxy: recording {recording_id} - cache read failed: {e}')

    node_result = _fetch_via_node(recording_id, rec)
    if node_result:
        content, content_type = node_result
    else:
        content, content_type = _fetch_recording_audio(recording_id, rec)
    try:
        with open(bin_path, 'wb') as f:
            f.write(content)
        with open(type_path, 'w', encoding='utf-8') as f:
            f.write(content_type or 'audio/wav')
    except Exception as e:
        log.warning(f'Recording download proxy: recording {recording_id} - failed to write cache: {e}')
    return content, content_type


def _clear_cache(recording_id):
    bin_path, type_path = _cache_paths(recording_id)
    for p in (bin_path, type_path):
        try:
            os.remove(p)
        except OSError:
            pass


def _fetch_via_node(recording_id, rec):
    """מבקשת מהנוד (phone-transcription-ivr) לשלוף את ההקלטה מימות בעצמו
    (עם בדיקת אורך מול duration_seconds + ניסיון חוזר, ראו
    fetchOnceWithRetries ב-ivr.js - זה כן הוכח עובד היטב בלוגים אמיתיים)
    ומחזירה (content, content_type), או None אם צריך ליפול לשליפה הישירה
    של הפייתון (_fetch_recording_audio). ראו "עדכון 7" בראש הקובץ."""
    if not (_NODE_IVR_URL and _INTERNAL_PROXY_SECRET):
        return None

    payload = {}
    if rec.rec_url:
        payload['url'] = rec.rec_url
    if rec.call_id:
        payload['call_id'] = rec.call_id
    if not payload:
        return None
    # אורך השיחה האמיתי (אם ידוע) - מאפשר לנוד לזהות שהקובץ שקיבל מימות
    # קצר מדי (למשל אם ימות עדיין לא סיימו "לכתוב" את הקובץ באותו רגע)
    # ולנסות שוב עם המתנה, במקום להסתפק בקובץ תקין אבל חלקי.
    if rec.duration_seconds:
        payload['expected_duration_seconds'] = int(rec.duration_seconds)

    try:
        upstream = requests.post(
            f'{_NODE_IVR_URL}/internal/fetch-recording-bytes',
            json=payload,
            headers={'X-Internal-Secret': _INTERNAL_PROXY_SECRET},
            timeout=240,
        )
        upstream.raise_for_status()
        data = upstream.json()
        import base64
        content = base64.b64decode(data['b64'])
        content_type = data.get('mimetype') or 'audio/wav'
    except Exception as e:
        log.warning(f'Recording download proxy: recording {recording_id} - Node fetch failed: {e}')
        return None

    log.info(f'Recording download proxy: recording {recording_id} - fetched via Node '
             f'({len(content)} bytes, looks_truncated={_wav_looks_truncated(content)})')
    return content, content_type


@download_bp.route('/dl/rec/<token>')
def download_recording(token):
    """נקודת ההורדה שהלקוח בדפדפן פונה אליה - מגישה את ההקלטה ישירות
    כתגובת HTTP בינארית רגילה (audio/*, inline) - בדיוק כמו קישור רגיל
    לקובץ אודיו באתר כלשהו. בלי HTML, בלי JSON, בלי חתיכות, בלי שום שלב
    ביניים - לחיצה אחת על הקישור, וזהו. ראו "עדכון 8" בראש הקובץ להסבר
    המלא למה זו כעת דרך ההגשה, ומה נבדק לפני שהוחלט עליה."""
    recording_id, rec = _resolve_recording_id(token)
    content, content_type = _get_or_fetch_cached_audio(recording_id, rec)
    _clear_cache(recording_id)  # תגובה חד-פעמית - אין יותר בקשות המשך (חתיכות) שדורשות מטמון
    from routes.download_utils import plain_audio_response
    return plain_audio_response(
        content=content,
        content_type=content_type,
        hebrew_filename=f'הקלטה {recording_id}.wav',
    )


@download_bp.route('/dl/rec/<token>/chunk/<int:index>')
def download_recording_chunk(token, index):
    """שמור כרשת ביטחון/תאימות לאחור בלבד - נקודת הקצה הזו כבר לא בשימוש
    ע"י download_recording למעלה (ראו "עדכון 8" בראש הקובץ: ההגשה בחתיכות
    JSON קטנות התבררה כלא-הפתרון - היא נבדקה ונכשלה באותו אופן בדיוק כמו
    כל וריאציה אחרת שמטמיעה את הקובץ בתוך HTML/JSON). נשארת כאן רק כדי
    שקישורי חתיכה ישנים שכבר נשלחו (אם יש) לא יקרסו עם 404 מיידי."""
    import base64
    from flask import jsonify

    recording_id, rec = _resolve_recording_id(token)
    content, content_type = _get_or_fetch_cached_audio(recording_id, rec)
    b64_full = base64.b64encode(content).decode('ascii')

    total_chunks = max(1, (len(b64_full) + _CHUNK_CHARS - 1) // _CHUNK_CHARS)
    if index < 0 or index >= total_chunks:
        abort(404)

    start = index * _CHUNK_CHARS
    piece = b64_full[start:start + _CHUNK_CHARS]
    is_last = index == total_chunks - 1

    log.info(f'Recording download proxy: recording {recording_id} chunk {index + 1}/{total_chunks} '
             f'({len(piece)} b64 chars)')

    if is_last:
        _clear_cache(recording_id)

    return jsonify({
        'filename': f'הקלטה {recording_id}.wav',
        'mimetype': content_type,
        'index': index,
        'total_chunks': total_chunks,
        'is_last': is_last,
        'b64_chunk': piece,
    })
