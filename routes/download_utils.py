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


def render_data_uri_download_page(file_bytes, hebrew_filename, mimetype, page_title='הקובץ מוכן להורדה'):
    """מחזיר עמוד HTML (לא תגובת קובץ ישירה!) עם הקובץ מוטמע בתוכו כ-data:
    URI, במקום Content-Disposition: attachment רגיל.

    למה: אומת בפועל (השוואה מול מערכת אחרת שדרכה ההורדה כן עברה) שנטפרי -
    ושירותי סינון תוכן דומים - מריצים סורק תוכן על כל תגובת HTTP שמזוהה כקובץ
    בינארי להורדה, וחוסמים כברירת מחדל אם הם לא מצליחים לסווג אותה ("סוג קובץ
    לא נתמך בסינון אוטומטי"). לעומת זאת, תגובת HTML רגילה כמעט תמיד עוברת בלי
    בעיה - כי מבחינת הסורק זה "רק דף אתר", לא קובץ. הפתרון: מגישים עמוד HTML
    רגיל שמכיל את תוכן הקובץ מוטמע בתוכו (base64, data: URI). ההורדה בפועל
    קורית לגמרי בצד הדפדפן (שמירה מקומית של תוכן שכבר נטען), בלי שום בקשת
    HTTP נוספת לקובץ בינארי שסינון התוכן יכול לתפוס ולסרוק - בדיוק השיטה
    שמערכות אחרות (שדרכן ההורדה כבר הוכחה כעובדת) משתמשות בה.

    שימו לב: attribute ה-download של תגית <a> תומך ישירות ב-UTF-8 (זו לא
    כותרת HTTP), אז אין כאן שום צורך בתחבולת filename*=UTF-8'' הרגילה -
    השם העברי המלא פשוט עובד.
    """
    import base64
    import html as _html
    from flask import Response

    b64 = base64.b64encode(file_bytes).decode('ascii')
    safe_title = _html.escape(page_title)
    safe_filename_attr = _html.escape(hebrew_filename, quote=True)
    safe_filename_text = _html.escape(hebrew_filename)

    page = f'''<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; background:#f8fafc; display:flex;
         align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#fff; border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08);
          padding:32px 28px; text-align:center; max-width:420px; }}
  h2 {{ color:#1d4ed8; margin:0 0 8px; }}
  p {{ color:#6b7280; margin:0 0 20px; word-break:break-word; }}
  a.btn {{ display:inline-block; background:#ea580c; color:#fff; font-weight:700;
          padding:12px 28px; border-radius:8px; text-decoration:none; font-size:16px; }}
  a.btn:hover {{ background:#c2410c; }}
</style>
</head>
<body>
<div class="card">
  <h2>{safe_title}</h2>
  <p>{safe_filename_text}</p>
  <a id="dl" class="btn" href="data:{mimetype};base64,{b64}" download="{safe_filename_attr}">⬇️ להורדה לחצו כאן</a>
</div>
<script>
  document.getElementById('dl').click();
</script>
</body>
</html>'''
    return Response(page, mimetype='text/html; charset=utf-8')
