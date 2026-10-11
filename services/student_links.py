"""
שיוך לקוח קיים כתלמיד של מוסד - באישור הלקוח - וכללי הטעינה העצמית של תלמידים.

זרימה: המוסד מקליד טלפון של לקוח שכבר קיים -> נוצרת StudentLinkRequest ונשלח
ללקוח מייל עם קישור אישור/דחייה (אם אין לו מייל - מנהל המערכת מאשר ידנית,
needs_admin). רק אחרי אישור הלקוח הופך לתלמיד. היתרה והיסטוריית התנועות שלו
נשמרות, והיתרה הקיימת נחשבת כסף שלו (Customer.self_funded) - המוסד לא יכול
להוריד אותה.
"""
import os
import secrets
import logging
from datetime import datetime, timedelta
from html import escape

from app import db

log = logging.getLogger(__name__)

LINK_TTL_DAYS = 7


# --------------------------------------------------------------------------
# טעינה עצמית של תלמידים
# --------------------------------------------------------------------------
def global_self_topup_enabled():
    """המתג המרכזי בהגדרות הניהול - 'מעלה' את מתג המוסד. ברירת מחדל: כבוי."""
    from routes.admin import get_setting
    return get_setting('allow_student_self_topup', '0') == '1'


def institution_self_topup_on(inst):
    """מתג המוסד עצמו (ברירת מחדל: מותר) - בלי קשר למתג המרכזי."""
    return inst is not None and inst.allow_student_topup is not False


def student_self_topup_allowed(customer):
    """האם מותר ללקוח הזה לטעון יתרה בעצמו. לקוח רגיל (לא תלמיד מוסד) - תמיד.
    תלמיד מוסד - רק אם המתג המרכזי דלוק וגם המוסד לא כיבה."""
    if not customer or not customer.institution_id or customer.is_institution_self:
        return True
    return global_self_topup_enabled() and institution_self_topup_on(customer.institution)


def institution_withdrawable(student):
    """כמה המוסד יכול להוריד לתלמיד: רק חלק המוסד ביתרה, לא כסף שהתלמיד טען/הביא."""
    return max(0.0, round(float(student.balance or 0) - float(student.self_funded or 0), 2))


# --------------------------------------------------------------------------
# בקשות שיוך
# --------------------------------------------------------------------------
def _base_url():
    return os.environ.get('APP_BASE_URL', '').rstrip('/')


def link_url(req):
    return f"{_base_url()}/student-link/{req.token}"


def _send_mail(to, subject, html):
    from routes.email_inbound import _is_system_inbound_address
    if not to or _is_system_inbound_address(to):
        return False
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email
        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        sg.send(Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
                     to_emails=to, subject=subject, html_content=html))
        return True
    except Exception as e:
        log.error(f'student link mail error to {to}: {e}')
        return False


def send_link_email(req):
    """מייל אישור ללקוח. מחזיר True אם נשלח."""
    customer, inst = req.customer, req.institution
    to = (customer.email or '').strip()
    if not to:
        return False
    iname = escape(inst.name or 'מוסד')
    bal = float(customer.balance or 0)
    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#111827">
<h2 style="color:#1d4ed8">בקשה לצירופך כתלמיד של {iname}</h2>
<p>שלום{(" " + escape(customer.name)) if customer.name else ""},</p>
<p>המוסד <b>{iname}</b> מבקש לצרף אותך כתלמיד שלו במערכת תמלול פון (המספר {escape(customer.phone or "")}).</p>
<p><b>מה זה אומר:</b></p>
<ul>
<li>המוסד יוכל להוסיף לך יתרה, להגביל את השימוש שלך, ולראות את הפעילות שלך כתלמיד (תמלולים, קבצים ותנועות יתרה).</li>
<li>היתרה שיש לך היום (₪{bal:.2f}) נשארת <b>שלך</b> - המוסד לא יוכל להוריד אותה.</li>
<li>היסטוריית השימוש שלך נשמרת.</li>
</ul>
<p style="text-align:center;margin:20px 0"><a href="{link_url(req)}" style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;display:inline-block">לצפייה ואישור או דחייה</a></p>
<p style="color:#6b7280;font-size:13px">אם לא ביקשת זאת או אינך מכיר את המוסד - פשוט דחה או התעלם מההודעה, ושום דבר לא ישתנה. הקישור תקף {LINK_TTL_DAYS} ימים.<br>מערכת תמלול פון 03-3131795</p>
</div>'''
    ok = _send_mail(to, f'בקשה לצירופך כתלמיד של {inst.name} - תמלול פון', html)
    if ok:
        req.sent_to = to
        db.session.commit()
    return ok


def request_link(inst, customer, display_name=None):
    """מבקש שיוך של לקוח קיים למוסד. מחזיר (קוד, הודעה, בקשה). קודים:
    sent / needs_admin / already_pending / already_yours / other_institution / is_institution."""
    from models import StudentLinkRequest, ManagerMessage
    if customer.is_institution_self:
        return 'is_institution', 'המספר הזה שייך לחשבון של מוסד ולא ללקוח רגיל', None
    if customer.institution_id == inst.id:
        return 'already_yours', 'הלקוח כבר רשום אצלכם כתלמיד', None
    if customer.institution_id:
        return 'other_institution', 'הלקוח הזה כבר משויך למוסד אחר', None

    now = datetime.utcnow()
    existing = StudentLinkRequest.query.filter_by(
        institution_id=inst.id, customer_id=customer.id, status='pending').first()
    if existing and existing.expires_at and existing.expires_at > now:
        return 'already_pending', 'כבר נשלחה ללקוח בקשת שיוך והיא ממתינה לאישורו', existing
    if existing:
        existing.status = 'expired'

    req = StudentLinkRequest(
        institution_id=inst.id, customer_id=customer.id, token=secrets.token_urlsafe(32),
        display_name=(display_name or customer.name or '').strip() or None,
        status='pending', expires_at=now + timedelta(days=LINK_TTL_DAYS))
    db.session.add(req)
    db.session.commit()

    if (customer.email or '').strip():
        send_link_email(req)
        return 'sent', f'ללקוח קיים במערכת נשלח מייל לאישור השיוך ({customer.email})', req

    req.needs_admin = True
    db.session.add(ManagerMessage(
        phone=customer.phone or '—', name=customer.name or None, source='student_link',
        transcript=(f'המוסד "{inst.name}" מבקש לצרף כתלמיד את הלקוח {customer.name or ""} ({customer.phone}), '
                    f'שאין לו כתובת מייל לאישור. יש לאשר/לדחות ידנית: {_base_url()}/admin/student-links')))
    db.session.commit()
    return 'needs_admin', 'ללקוח אין כתובת מייל, ולכן הבקשה הועברה לאישור מנהל המערכת', req


def _notify_institution(req, approved, by_admin=False):
    inst, customer = req.institution, req.customer
    to = (inst.notify_email or inst.email or '').strip()
    who = escape(req.display_name or customer.name or customer.phone or '')
    if approved:
        subject, line = f'{who} אישר/ה את הצטרפותו כתלמיד', f'הלקוח <b>{who}</b> אישר את הצירוף ומופיע עכשיו ברשימת התלמידים שלכם.'
    else:
        subject, line = f'{who} דחה/תה את בקשת השיוך', f'הלקוח <b>{who}</b> דחה את בקשת השיוך כתלמיד.'
    _send_mail(to, subject + ' - תמלול פון',
               f'<div dir="rtl" style="font-family:Arial,sans-serif"><p>{line}</p><p style="color:#6b7280;font-size:13px">מערכת תמלול פון</p></div>')


def approve_link(req, by_admin=False):
    """מאשר: הלקוח הופך לתלמיד. מחזיר (True/False, הודעה)."""
    from models import Customer
    if req.status != 'pending':
        return False, 'הבקשה כבר טופלה'
    if req.expires_at and req.expires_at < datetime.utcnow():
        req.status = 'expired'
        db.session.commit()
        return False, 'תוקף הבקשה פג - יש לבקש מהמוסד לשלוח בקשה חדשה'
    customer = Customer.query.get(req.customer_id)
    if not customer or customer.institution_id or customer.is_institution_self:
        req.status = 'cancelled'
        req.responded_at = datetime.utcnow()
        db.session.commit()
        return False, 'הלקוח כבר משויך למוסד, ולכן הבקשה בוטלה'
    from routes.institution_students import _generate_student_number
    customer.institution_id = req.institution_id
    customer.student_number = _generate_student_number()
    customer.student_display_name = req.display_name or customer.name
    if not customer.name and req.display_name:
        customer.name = req.display_name
    # היתרה שהייתה לו כלקוח עצמאי - כסף שלו, המוסד לא יכול להוריד אותה
    customer.self_funded = round(max(0.0, float(customer.balance or 0)), 2)
    req.status = 'approved'
    req.responded_at = datetime.utcnow()
    db.session.commit()
    try:
        _notify_institution(req, True, by_admin)
    except Exception as e:
        log.error(f'student link notify error: {e}')
    return True, 'הצירוף אושר'


def decline_link(req):
    if req.status != 'pending':
        return False, 'הבקשה כבר טופלה'
    req.status = 'declined'
    req.responded_at = datetime.utcnow()
    db.session.commit()
    try:
        _notify_institution(req, False)
    except Exception as e:
        log.error(f'student link notify error: {e}')
    return True, 'הבקשה נדחתה'
