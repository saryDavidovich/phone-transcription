"""
הסרה מרשימת התפוצה (הודעה תפוצתית) - דרך SendGrid בלבד, בלי שהקישור יוביל לאתר שלנו.

- בסנדגריד יש "קבוצת הסרה" (Unsubscribe Group). כל מייל תפוצה נשלח משויך אליה, ובתחתית שלו קישור
  הסרה שסנדגריד ממלא בעצמו (<%asm_group_unsubscribe_raw_url%>) - הקישור מוביל לדף של סנדגריד.
- מי שלחץ "הסרה" - סנדגריד לא ישלח אליו יותר מיילי תפוצה (מיילים רגילים של המערכת - תמלולים וכו' - ממשיכים).
- את מספר הקבוצה לוקחים ממשתנה הסביבה SENDGRID_UNSUBSCRIBE_GROUP_ID, ואם אין - יוצרים קבוצה פעם אחת
  דרך ה-API ושומרים בהגדרות.
- הרשימה של מי שהוסר נמשכת מסנדגריד (לתצוגה בממשק, ולא לשלוח אליהם בכלל).
"""
import logging
import os
import time

log = logging.getLogger(__name__)

UNSUB_TAG = '<%asm_group_unsubscribe_raw_url%>'
_SETTING = 'sendgrid_unsub_group_id'
_cache = {'at': 0, 'emails': None}
_fail = {'at': 0}           # יצירה שנכשלה - לא מנסים שוב לפני 10 דקות


def _sg():
    import sendgrid
    return sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))


def _get_setting(key):
    from models import Settings
    r = Settings.query.filter_by(key=key).first()
    return (r.value or '').strip() if r else ''


def _set_setting(key, value):
    from app import db
    from models import Settings
    r = Settings.query.filter_by(key=key).first()
    if not r:
        r = Settings(key=key)
        db.session.add(r)
    r.value = value
    db.session.commit()


def group_id(create=True):
    """מספר קבוצת ההסרה בסנדגריד, או None אם אין ולא הצלחנו ליצור."""
    gid = (os.environ.get('SENDGRID_UNSUBSCRIBE_GROUP_ID') or '').strip() or _get_setting(_SETTING)
    if gid.isdigit():
        return int(gid)
    if not create or not os.environ.get('SENDGRID_API_KEY') or time.time() - _fail['at'] < 600:
        return None
    try:
        import json
        res = _sg().client.asm.groups.post(request_body={
            'name': 'הודעות תפוצה - תמלול פון',
            'description': 'הודעות כלליות ועדכונים מתמלול פון (לא כולל תמלולים ומיילים שביקשתם)',
            'is_default': False})
        gid = json.loads(res.body).get('id')
        if gid:
            _set_setting(_SETTING, str(gid))
            log.info(f'broadcast: נוצרה קבוצת הסרה בסנדגריד #{gid}')
            return int(gid)
    except Exception as e:
        log.warning(f'broadcast: יצירת קבוצת הסרה בסנדגריד נכשלה: {e}')
    _fail['at'] = time.time()
    return None


def suppressed_emails(max_age=60):
    """הכתובות שהסירו את עצמן (באותיות קטנות). None = לא ידוע (אין קבוצה / סנדגריד לא ענה)."""
    if _cache['emails'] is not None and time.time() - _cache['at'] < max_age:
        return _cache['emails']
    gid = group_id(create=False)
    if not gid:
        return None
    try:
        import json
        res = _sg().client.asm.groups._(gid).suppressions.get()
        emails = {str(e).strip().lower() for e in (json.loads(res.body) or [])}
    except Exception as e:
        log.warning(f'broadcast: קריאת רשימת ההסרה מסנדגריד נכשלה: {e}')
        return _cache['emails']
    _cache.update(at=time.time(), emails=emails)
    return emails


def restore(email):
    """מחזיר כתובת לרשימת התפוצה (רק אם הלקוח ביקש)."""
    gid = group_id(create=False)
    if not gid or not email:
        return False
    try:
        _sg().client.asm.groups._(gid).suppressions._(email.strip()).delete()
    except Exception as e:
        log.warning(f'broadcast: החזרת {email} לרשימה נכשלה: {e}')
        return False
    _cache['emails'] = None
    return True


def footer_html(link):
    """שורת ההסרה שבתחתית המייל (מתחת לכרטיס)."""
    return ('<!--BCUNSUB--><div dir="rtl" style="max-width:600px;margin:0 auto;padding:4px 18px 18px;text-align:center;'
            'font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#7a8699;line-height:1.7">'
            'קיבלתם את ההודעה כי אתם רשומים בתמלול פון. לא מעוניינים לקבל עוד הודעות כאלה? '
            f'<a href="{link}" style="color:#7a8699;text-decoration:underline">להסרה מרשימת התפוצה</a>'
            '<br>ההסרה לא משפיעה על קבלת התמלולים שלכם.</div><!--/BCUNSUB-->')


def finalize(html, gid):
    """לפני שליחה: עם קבוצה - התגית של סנדגריד; בלי - מורידים את שורת ההסרה (לא קישור מת)."""
    import re
    if gid:
        return html.replace('#BCUNSUB#', UNSUB_TAG)
    return re.sub(r'<!--BCUNSUB-->.*?<!--/BCUNSUB-->', '', html, flags=re.S)
