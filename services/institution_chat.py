"""
התכתבות מוסד <-> הנהלת המערכת, בסגנון צ'אט.

- המוסד כותב באזור המוסד ("צור קשר") -> נשמר כהודעה נכנסת ומופיע אצל המנהל בלשונית "מוסדות"
  של "הודעות למנהל" (לא בהודעות הטלפון).
- המנהל עונה מהממשק -> נשמר כהודעה יוצאת, מופיע אצל המוסד בצ'אט, ונשלח גם במייל למוסד.
- המוסד יכול לענות גם ישירות למייל: הנושא מכיל "מוסד #<מספר>", והתשובה נכנסת לאותה התכתבות
  (רק מכתובת מייל שמשויכת למוסד).
"""
import os
import re
import uuid
import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)
SUBJECT_RE = re.compile(r'מוסד\s*#\s*(\d+)')


def subject_for(inst):
    return f'הודעה מצוות תמלול פון - מוסד #{inst.id}'


def institution_emails(inst):
    out = {(inst.email or '').strip().lower(), (inst.notify_email or '').strip().lower()}
    for a in (inst.authorized_logins or []):
        if isinstance(a, dict) and a.get('email'):
            out.add(str(a['email']).strip().lower())
    out.discard('')
    return out


def add_from_institution(inst, body, via='web', sender=None):
    from app import db
    from models import InstitutionChatMessage
    body = (body or '').strip()[:5000]
    if not body:
        return None
    m = InstitutionChatMessage(institution_id=inst.id, direction='in', body=body, via=via,
                               sender=(sender or '')[:200] or None, is_read=False)
    db.session.add(m)
    db.session.commit()
    log.info(f'institution chat: new message from institution {inst.id} via {via}')
    return m


def add_from_admin(inst, body):
    """תשובת המנהל: נשמרת, מסמנת את הודעות המוסד כנקראו, ונשלחת במייל ברקע."""
    from app import db
    from models import InstitutionChatMessage
    body = (body or '').strip()[:5000]
    if not body:
        return None
    domain = (os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@tamlulphone.co.il')).split('@')[-1]
    prev = (InstitutionChatMessage.query.filter_by(institution_id=inst.id, direction='out')
            .filter(InstitutionChatMessage.message_id.isnot(None))
            .order_by(InstitutionChatMessage.created_at.desc()).first())
    m = InstitutionChatMessage(institution_id=inst.id, direction='out', body=body, via='web', is_read=False,
                               message_id=f'<inst-chat-{inst.id}-{uuid.uuid4().hex}@{domain}>')
    db.session.add(m)
    InstitutionChatMessage.query.filter_by(institution_id=inst.id, direction='in', is_read=False).update(
        {InstitutionChatMessage.is_read: True}, synchronize_session=False)
    db.session.commit()
    to = inst.notify_email or inst.email
    if to:
        from app import app as flask_app
        args = (flask_app, to, inst.id, inst.name, body, m.message_id, prev.message_id if prev else None)
        threading.Thread(target=_send_email, args=args, daemon=True).start()
    return m


def _send_email(flask_app, to, inst_id, inst_name, body, message_id, in_reply_to):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, ReplyTo, Header
        from services import email_layout as L
        from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
        base = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
        card = (L.title('הודעה מצוות תמלול פון') + L.meta(inst_name or '') + L.text_block(body)
                + (L.button(f'{base}/institution/contact', 'להתכתבות באזור המוסד') if base else '')
                + L.signature(can_reply=True))
        msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
                   to_emails=to, subject=f'הודעה מצוות תמלול פון - מוסד #{inst_id}',
                   html_content=L.wrap(card, show_circles=False))
        msg.reply_to = ReplyTo(TRANSCRIBE_INBOUND_EMAIL, 'תמלול פון')
        msg.header = Header('Message-ID', message_id)
        if in_reply_to:
            msg.header = Header('In-Reply-To', in_reply_to)
            msg.header = Header('References', in_reply_to)
        sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
        log.info(f'institution chat: reply emailed to institution {inst_id}')
    except Exception as e:
        log.error(f'institution chat: email to institution {inst_id} failed: {e}')


def handle_inbound_email(subject, sender_email, body_text, in_reply_to=None):
    """תשובה במייל של מוסד להודעה מהמנהל. מחזיר True אם טופל (גם אם נדחה), False אם זה לא מייל כזה."""
    from models import Institution, InstitutionChatMessage
    inst = None
    if in_reply_to:
        prev = InstitutionChatMessage.query.filter_by(message_id=in_reply_to).first()
        if prev:
            inst = Institution.query.get(prev.institution_id)
    if not inst:
        mt = SUBJECT_RE.search(subject or '')
        if not mt:
            return False
        inst = Institution.query.get(int(mt.group(1)))
    if not inst:
        return False
    if (sender_email or '').strip().lower() not in institution_emails(inst):
        log.warning(f'institution chat: email reply from {sender_email} rejected (not an email of institution {inst.id})')
        return True
    add_from_institution(inst, body_text or '(הודעה ריקה)', via='email', sender=sender_email)
    return True


def unread_for_institution(inst_id):
    from models import InstitutionChatMessage
    return InstitutionChatMessage.query.filter_by(institution_id=inst_id, direction='out', is_read=False).count()


def serialize(m):
    from zoneinfo import ZoneInfo
    lt = m.created_at.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('Asia/Jerusalem')) if m.created_at else None
    return {'id': m.id, 'dir': m.direction, 'body': m.body, 'via': m.via,
            'time': lt.strftime('%H:%M') if lt else '', 'date': lt.strftime('%d/%m/%Y') if lt else '',
            'read': bool(m.is_read)}
