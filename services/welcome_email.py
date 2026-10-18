"""
מייל "ברוכים הבאים" - נשלח פעם אחת, מיד כשנקבעת ללקוח כתובת מייל חדשה:
כשהלקוח מגדיר ומאשר אותה בטלפון (ה-IVR קורא ל-/api/customer/update רק אחרי
אישור), או כשהמנהל מוסיף/מעדכן אותה בממשק הניהול. כתובת שלא השתנתה לא
מקבלת מייל שוב. העיצוב - המסגרת האחידה של כל המיילים (email_layout),
עם כפתור בולט לטעינת הארנק (קישור אישי לנדרים פלוס לפי הטלפון).
"""
import os
import logging
import threading
from html import escape

log = logging.getLogger(__name__)


def build_welcome_html(name, phone, balance):
    from services import email_layout as L
    first = (name or '').split()[0] if (name or '').strip() else ''
    greeting = f'שלום {escape(first)},' if first else 'שלום,'
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL as inbound
    li = 'style="margin:0 0 8px"'
    card = (L.title('ברוכים הבאים לתמלול פון')
            + f'<div style="margin-top:14px;font-size:16px;line-height:1.8;text-align:justify">{greeting}<br>'
              f'כתובת המייל שלך נשמרה בהצלחה. מעכשיו כל התמלולים והמסמכים יישלחו אליך לכתובת הזו.</div>'
            + f'<div style="margin-top:16px;background:#f3f8ff;border:1px solid {L.BOX_BORDER};border-radius:12px;'
              f'padding:14px 18px;font-size:15px;line-height:1.7">'
              f'<div style="font-weight:bold;color:{L.BLUE};margin-bottom:8px">איך משתמשים</div>'
              f'<div {li}>&#9679; לתמלול הקלטה - מתקשרים ל-{L.PHONE_NUMBER}, מקליטים, והתמלול מגיע למייל.</div>'
              f'<div {li}>&#9679; שליחת קבצים במייל - אפשר לשלוח הקלטה לתמלול, או צילום כתב יד לזיהוי, '
              f'לכתובת <b dir="ltr">{escape(inbound)}</b>, ובשורת הנושא לכתוב את מספר הטלפון שלך '
              f'(<span dir="ltr">{escape(phone or "")}</span>).</div>'
              f'<div {li}>&#9679; מסמכים קודמים - בשלוחה 8 אפשר לשמוע אותם ולקבל כל אחד שוב.</div>'
              f'<div style="margin:0">&#9679; התשלום הוא מהארנק, לפי אורך ההקלטה או הטקסט.</div></div>'
            + f'<div style="margin-top:16px;font-size:15px;color:{L.SOFT}">היתרה הנוכחית בארנק: '
              f'<b style="color:{L.INK}">&#8362;{balance:.2f}</b></div>'
            + L.button(L.topup_url(phone), 'לטעינת הארנק')
            + L.signature(can_reply=True))
    return L.wrap(card, phone=phone)


def send_welcome_email(to, name, phone, balance):
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email
    from routes.email_inbound import _is_system_inbound_address
    if not to or _is_system_inbound_address(to):
        return
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
               to_emails=to, subject='ברוכים הבאים לתמלול פון',
               html_content=build_welcome_html(name, phone, balance))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
    log.info(f'welcome email sent to {to} (phone {phone})')


def send_welcome_async(to, name, phone, balance):
    """ברקע - כדי לא לעכב את התשובה ל-IVR באמצע השיחה."""
    def run():
        try:
            send_welcome_email(to, name, phone, balance)
        except Exception as e:
            log.error(f'welcome email to {to} failed: {e}')
    threading.Thread(target=run, daemon=True).start()


def welcome_if_new(old_email, new_email, name, phone, balance):
    """שולח "ברוכים הבאים" רק אם נקבעה כתובת חדשה (שונה מהקודמת, לא ריקה).
    משמש גם את עדכון הכתובת בטלפון וגם את עדכון/הוספת לקוח בממשק הניהול."""
    new = (new_email or '').strip()
    if new and new.lower() != (old_email or '').strip().lower():
        send_welcome_async(new, name, phone, float(balance or 0))
        return True
    return False
