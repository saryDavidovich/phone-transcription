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
import logging
import requests
from flask import Blueprint, abort, current_app
from itsdangerous import URLSafeSerializer, BadSignature

log = logging.getLogger(__name__)

download_bp = Blueprint('download', __name__)

_SALT = 'recording-download-v1'


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
    if not rec or not rec.rec_url:
        log.warning(f'Recording download proxy: recording {recording_id} not found or has no rec_url')
        abort(404)

    try:
        # תמיד מביאים את הקובץ המלא לזיכרון בצד השרת - צריך את כל הבייטים
        # כדי לקודד ל-base64 ולהטמיע בעמוד ה-HTML (ראו למטה).
        upstream = requests.get(rec.rec_url, timeout=120)
        upstream.raise_for_status()
    except Exception:
        log.exception(f'Recording download proxy: failed to fetch upstream for recording {recording_id}')
        abort(502)

    content_type = upstream.headers.get('Content-Type') or 'audio/wav'

    from routes.download_utils import render_data_uri_download_page
    return render_data_uri_download_page(
        upstream.content,
        hebrew_filename=f'הקלטה {recording_id}.wav',
        mimetype=content_type,
        page_title='ההקלטה מוכנה להורדה',
    )
