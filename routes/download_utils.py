"""
עזר משותף להורדת קבצים עם שם עברי, בלי לתקוע קוראים "קפדניים" (כמו נטפרי).

הבעיה: Flask/Werkzeug בונים אוטומטית כותרת Content-Disposition עם שני
פרמטרים כששם הקובץ (download_name) לא ניתן לקידוד ב-ASCII: filename="..."
(פולבק ל-ASCII) ו-filename*=UTF-8''... (השם האמיתי, לקוראים מודרניים). כדי
לבנות את הפולבק, Werkzeug עושה `unicodedata.normalize('NFKD', name).encode(
'ascii', 'ignore')` - שעובד טוב לאותיות לטיניות עם ניקוד (é -> e), אבל
לעברית אין שום פירוק ל-ASCII, אז כל התווים העבריים פשוט **נעלמים**, ונשאר
שם קובץ כמעט ריק (רק רווחים/מקפים/הסיומת). קוראים שסומכים על filename=
הפשוט (ולא על filename*=) עלולים לשמור את הקובץ עם שם שבור, ובהתאם לכך
לא לזהות את סוג הקובץ נכון ("נסה סוג אחר").

הפתרון: לקרוא ל-send_file עם שם ASCII תקין וסביר (כדי ש-Werkzeug לא ייכנס
בכלל לנתיב הבעייתי), ואז לדרוס ידנית את הכותרת כך שהיא כוללת גם את הפולבק
ה-ASCII התקין וגם את השם העברי האמיתי דרך filename*=UTF-8''... - כל קורא
"רגיל" יראה את השם העברי היפה, וכל קורא "קפדני" שרק מבין filename= יקבל
עדיין שם קובץ תקין עם הסיומת הנכונה, במקום שם שבור.
"""
from urllib.parse import quote


def send_file_with_hebrew_name(send_file_func, buffer_or_path, hebrew_filename, mimetype, ascii_fallback='document.docx'):
    """עוטף קריאה ל-send_file: שולח בפועל עם שם ASCII בטוח, ואז דורס את
    כותרת ה-Content-Disposition כך שגם השם העברי המלא זמין (עבור קוראים
    מודרניים) וגם פולבק ASCII תקין (עבור קוראים קפדניים)."""
    response = send_file_func(
        buffer_or_path,
        as_attachment=True,
        download_name=ascii_fallback,
        mimetype=mimetype,
    )
    response.headers['Content-Disposition'] = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(hebrew_filename, safe='')}"
    )
    return response
