"""
מיילים לאחרים (שלוחה 1 ← 6).

הלקוח מקליט הודעה בטלפון ובוחר איש קשר (או מקיש כתובת חדשה). ההודעה מתומללת
(Gemini, כמו תמלול רגיל) ונשלחת במייל לנמען: התמלול בגוף המייל + קישור להאזנה
להקלטה המקורית. בשם השולח ובנושא מופיע ממי ההודעה, וכתובת התשובה (Reply-To) היא
כתובת המערכת - כשהנמען עונה, התשובה מזוהה לפי "(מס׳ 123456)" בנושא, נשמרת בחשבון
הלקוח (EmailReply) ומוקראת לו בטלפון. הלקוח יכול לענות לתגובה (הקלטה), וכך הלאה -
שרשור אחד עם אותו מספר (thread_ref).

חיוב: לפי יחידות של 5 דקות (הגדרה price_per_5min_email_out), מראש - לפני התמלול.
נכשל התמלול או השליחה - החיוב מוחזר.

אנשי קשר (CustomerContact): בטלפון (הוספה, עריכת שם / כתובת, מחיקה), במייל למערכת
עם הנושא "אנשי קשר" (הוספה ומחיקה), ובכרטיס הלקוח בממשק הניהול.
"""
import logging
import math
import os
import re
import secrets
import threading
from datetime import datetime

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')
# מספר השרשור בנושא: "הודעה מאת משה (מס׳ 482193)" / "תשובה מאת ..." (וגם הפורמט הישן [#M3A867C])
REF_RE = re.compile(r'(?:הודעה|תשובה) מאת .*?\(מס[׳\'"]?\s*(\d{6})\)|\[#(M[A-Z0-9]{6})\]')


def ref_in(subject):
    m = REF_RE.search(subject or '')
    return (m.group(1) or m.group(2)) if m else None


def _new_ref():
    from models import OutgoingEmail
    for _ in range(20):
        ref = str(secrets.randbelow(900000) + 100000)
        if not OutgoingEmail.query.filter((OutgoingEmail.ref == ref) | (OutgoingEmail.thread_ref == ref)).first():
            return ref
    return 'M' + secrets.token_hex(3).upper()
DEFAULT_PRICE = '0.30'
MAX_CONTACTS = 99
DEFAULT_DAILY_LIMIT = '30'          # מקסימום מיילים לאנשי קשר ללקוח ביום (הגנה מפני ניצול לספאם)
REPLY_TTS_MAX = 1500                # תגובה ארוכה מוקראת עד כאן


def is_yemot_url(url):
    """רק הקלטות מימות (ה-IVR שלנו) - לא כתובת שרירותית (הגנה מ-SSRF / שימוש לרעה)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url or '')
    except Exception:
        return False
    return u.scheme == 'https' and (u.hostname or '').lower() in ('www.call2all.co.il', 'call2all.co.il')


def daily_limit():
    try:
        return int(_setting('email_out_daily_limit', DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        return int(DEFAULT_DAILY_LIMIT)


def _one_line(v):
    return re.sub(r'[\r\n\t]+', ' ', str(v or '')).strip()


# ---------------------------------------------------------------------------
# הגדרות ומחיר
# ---------------------------------------------------------------------------
def _setting(key, default):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value not in (None, '') else default


def enabled():
    return _setting('email_out_enabled', '0') == '1'


def unit_price():
    try:
        return float(_setting('price_per_5min_email_out', DEFAULT_PRICE))
    except (TypeError, ValueError):
        return float(DEFAULT_PRICE)


def cost_for(seconds):
    units = max(1, math.ceil((seconds or 0) / 300))
    return round(units * unit_price(), 2)


def sender_name(customer):
    return _one_line(customer.name)[:80] or customer.phone


# ---------------------------------------------------------------------------
# אנשי קשר
# ---------------------------------------------------------------------------
def normalize_email(v):
    return (v or '').strip().strip('<>').lower()


def contacts(customer):
    from models import CustomerContact
    return (CustomerContact.query.filter_by(customer_id=customer.id)
            .order_by(CustomerContact.id.asc()).all())


def add_contact(customer, email, name=''):
    """מוסיף (או מעדכן שם של) איש קשר. מחזיר (contact, שגיאה)."""
    from app import db
    from models import CustomerContact
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        return None, 'כתובת מייל לא תקינה'
    name = (name or '').strip()[:120]
    existing = CustomerContact.query.filter_by(customer_id=customer.id, email=email).first()
    if existing:
        if name and existing.name != name:
            existing.name = name
            db.session.commit()
        return existing, None
    if CustomerContact.query.filter_by(customer_id=customer.id).count() >= MAX_CONTACTS:
        return None, f'אפשר לשמור עד {MAX_CONTACTS} אנשי קשר'
    c = CustomerContact(customer_id=customer.id, email=email, name=name or email.split('@')[0])
    db.session.add(c)
    db.session.commit()
    return c, None


def delete_contact(customer, contact_id):
    from app import db
    from models import CustomerContact
    c = CustomerContact.query.filter_by(id=contact_id, customer_id=customer.id).first()
    if c:
        db.session.delete(c)
        db.session.commit()
    return bool(c)


def parse_contact_lines(text):
    """שורות 'שם - כתובת' / 'כתובת שם' / רק כתובת -> [(שם, מייל)]."""
    out = []
    for line in (text or '').splitlines():
        m = re.search(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', line)
        if not m:
            continue
        name = (line[:m.start()] + ' ' + line[m.end():]).strip(' \t-–:,;<>()"\'')
        out.append((name, m.group(0)))
    return out


def update_contact(customer, contact_id, name=None, email=None):
    from app import db
    from models import CustomerContact
    c = CustomerContact.query.filter_by(id=int(contact_id or 0), customer_id=customer.id).first()
    if not c:
        return None, 'איש הקשר לא נמצא'
    if email is not None:
        email = normalize_email(email)
        if not EMAIL_RE.match(email):
            return None, 'כתובת מייל לא תקינה'
        c.email = email
    if name:
        c.name = name.strip()[:120]
    db.session.commit()
    return c, None


def find_contact_by_text(customer, text):
    """איש קשר לפי כתובת מייל או שם (למחיקה במייל)."""
    t = (text or '').strip().strip(':-–').strip()
    if not t:
        return None
    m = re.search(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', t)
    for c in contacts(customer):
        if m and c.email == normalize_email(m.group(0)):
            return c
        if not m and (c.name or '').strip() == t:
            return c
    return None


# ---------------------------------------------------------------------------
# שליחת מייל
# ---------------------------------------------------------------------------
def _send(to_email, subject, html, from_name=None, reply_to=None):
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email, ReplyTo
    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
                                from_name or 'תמלול פון'),
               to_emails=to_email, subject=subject, html_content=html)
    if reply_to:
        msg.reply_to = ReplyTo(reply_to)
    resp = sg.send(msg)
    if getattr(resp, 'status_code', 202) >= 300:
        raise RuntimeError(f'SendGrid {resp.status_code}')


def _recording_wav_seconds(rec_url):
    """אורך ההקלטה בשניות (מוריד את הקובץ - הודעות קצרות). None - לא ניתן לקרוא."""
    import io
    import wave
    import requests
    try:
        r = requests.get(rec_url, timeout=90)
        r.raise_for_status()
        with wave.open(io.BytesIO(r.content)) as w:
            return int(round(w.getnframes() / float(w.getframerate() or 8000)))
    except Exception as e:
        log.warning(f'email_out: לא נמדד אורך ההקלטה: {e}')
        return None


def outgoing_html(name, phone, transcript, listen_url):
    from html import escape
    paras = ''.join(f'<p style="margin:0 0 10px">{escape(p)}</p>'
                    for p in (transcript or '').split('\n') if p.strip())
    listen = (f'<p style="margin:18px 0"><a href="{escape(listen_url)}" style="background:#2563eb;color:#fff;'
              f'text-decoration:none;padding:10px 22px;border-radius:8px;font-weight:700;display:inline-block">'
              f'🎧 להאזנה להודעה המקורית</a></p>') if listen_url else ''
    return f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#111827;line-height:1.8">
<p style="color:#6b7280;font-size:14px;margin:0 0 4px">הודעה קולית שהוקלטה בטלפון ותומללה</p>
<h2 style="margin:0 0 14px;color:#1d4ed8">הודעה מאת {escape(name)}</h2>
<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:16px;font-size:16px">{paras or '(לא זוהה טקסט בהקלטה)'}</div>
{listen}
<p style="color:#374151;font-size:14px">אפשר להשיב למייל הזה - התשובה תגיע אל {escape(name)} ותוקרא לו בטלפון.</p>
<p style="color:#9ca3af;font-size:12px;margin-top:20px">נשלח באמצעות מערכת תמלול פון 03-3131795 · התמלול אוטומטי וייתכנו בו טעויות</p>
</div>'''


def send_from_phone(phone, rec_url, call_id, contact_id=None, email=None, reply_id=None):
    """נקרא מה-IVR אחרי ההקלטה. מחזיר dict: ok / error ('balance', 'recipient', ...) / cost.
    reply_id - מענה לתגובה שהתקבלה (אותו שרשור, לכתובת ששלחה את התגובה)."""
    from app import app, db
    from models import Customer, CustomerContact, OutgoingEmail, Recording, Transaction, EmailReply
    from services.customer_phones import find_customer_by_phone
    customer = find_customer_by_phone(phone)
    if not customer:
        return {'ok': False, 'error': 'customer'}
    if not is_yemot_url(rec_url):
        log.warning(f'email_out: נדחתה כתובת הקלטה שאינה מימות: {str(rec_url)[:80]}')
        return {'ok': False, 'error': 'recording'}
    from datetime import timedelta
    today = OutgoingEmail.query.filter(OutgoingEmail.customer_id == customer.id,
                                       OutgoingEmail.created_at > datetime.utcnow() - timedelta(hours=24)).count()
    if today >= daily_limit():
        log.warning(f'email_out: לקוח {customer.id} הגיע למכסה היומית ({today})')
        return {'ok': False, 'error': 'limit'}
    to_name = None
    thread_ref = None
    if reply_id:
        rep = EmailReply.query.filter_by(id=int(reply_id), customer_id=customer.id).first()
        parent = OutgoingEmail.query.get(rep.outgoing_id) if rep else None
        if not rep or not parent or not EMAIL_RE.match(rep.from_email or ''):
            return {'ok': False, 'error': 'recipient'}
        to_email = rep.from_email
        to_name = parent.to_name if parent.to_email == to_email else None
        thread_ref = parent.thread_ref or parent.ref
    elif contact_id:
        c = CustomerContact.query.filter_by(id=int(contact_id), customer_id=customer.id).first()
        if not c:
            return {'ok': False, 'error': 'recipient'}
        to_email, to_name = c.email, c.name
    else:
        to_email = normalize_email(email)
        if not EMAIL_RE.match(to_email):
            return {'ok': False, 'error': 'recipient'}
        c, _err = add_contact(customer, to_email)       # כתובת חדשה נשמרת כאיש קשר
        to_name = c.name if c else None

    secs = _recording_wav_seconds(rec_url)
    if secs is None:
        return {'ok': False, 'error': 'recording'}
    cost = cost_for(secs)
    if (customer.balance or 0) < cost:
        return {'ok': False, 'error': 'balance', 'cost': cost}

    maintenance_on = _setting('maintenance_mode', '0') == '1'
    # מזהה ייחודי לכל מייל: באותה שיחה אפשר לשלוח כמה (ומזהה השיחה כבר שמור בהקלטות אחרות)
    rec = Recording(customer_id=customer.id, call_id=f'{call_id or "mail"}-m{secrets.token_hex(4)}',
                    duration_seconds=secs, status='queued_maintenance' if maintenance_on else 'transcribing',
                    delivery_method='email',
                    delivered_to=to_email, rec_url=rec_url, transcription_tier='email_out',
                    language='he', output_language='he', cost=cost, source_filename='מייל לאיש קשר')
    db.session.add(rec)
    db.session.flush()
    ref = _new_ref()
    out = OutgoingEmail(customer_id=customer.id, recording_id=rec.id, ref=ref, thread_ref=thread_ref or ref,
                        in_reply_to_id=int(reply_id) if reply_id else None,
                        to_email=to_email, to_name=to_name, duration_seconds=secs, cost=cost)
    db.session.add(out)
    customer.balance -= cost
    db.session.add(Transaction(customer_id=customer.id, amount=-cost, type='debit', recording_id=rec.id,
                               description=f'מייל לאיש קשר ({to_email}), {max(1, math.ceil(secs / 300)) * 5} דק׳'))
    if maintenance_on:
        out.status = 'queued'      # מצב תחזוקה - יישלח כשהמצב יכובה (resume_queued)
    db.session.commit()
    if not maintenance_on:
        threading.Thread(target=_process, args=(app, out.id), daemon=True).start()
    log.info(f'email_out: {out.ref} ללקוח {customer.id} -> {to_email}, {secs}s, חויב {cost}')
    return {'ok': True, 'cost': cost, 'to': to_email, 'id': out.id}


def resume_queued():
    """כיבוי מצב תחזוקה: שולח את המיילים לאנשי קשר שהוקלטו בזמן התחזוקה. מחזיר כמה."""
    from app import app, db
    from models import OutgoingEmail, Recording
    ids = [o.id for o in OutgoingEmail.query.filter_by(status='queued').all()]
    for oid in ids:
        o = OutgoingEmail.query.get(oid)
        o.status = 'processing'
        rec = Recording.query.get(o.recording_id) if o.recording_id else None
        if rec and rec.status == 'queued_maintenance':
            rec.status = 'transcribing'
    db.session.commit()
    for oid in ids:
        threading.Thread(target=_process, args=(app, oid), daemon=True).start()
    return len(ids)


def _process(app, out_id):
    from app import db
    from models import OutgoingEmail, Recording, Customer, Transaction
    from services.job_tracker import tracked
    with app.app_context(), tracked('email_out', label=f'מייל לאיש קשר #{out_id}'):
        out = OutgoingEmail.query.get(out_id)
        rec = Recording.query.get(out.recording_id) if out else None
        if not out or not rec:
            return
        try:
            from services.transcribe import _gemini_from_url
            text, _dur, _vid = _gemini_from_url(rec.rec_url, language='he', output_language='he')
            if not (text or '').strip():
                raise RuntimeError('התמלול לא החזיר טקסט')
            customer = Customer.query.get(out.customer_id)
            from routes.recording_proxy import recording_download_url
            from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
            name = sender_name(customer)
            kind = 'תשובה' if out.in_reply_to_id else 'הודעה'
            _send(out.to_email, f'{kind} מאת {name} (מס׳ {out.thread_ref or out.ref})',
                  outgoing_html(name, customer.phone, text, recording_download_url(rec.id)),
                  from_name=f'{name} (דרך תמלול פון)', reply_to=TRANSCRIBE_INBOUND_EMAIL)
            out.transcript = text
            out.status = 'sent'
            out.sent_at = datetime.utcnow()
            rec.transcript = text
            rec.status = 'delivered'
            db.session.commit()
            log.info(f'email_out: {out.ref} נשלח ל-{out.to_email}')
        except Exception as e:
            db.session.rollback()
            out = OutgoingEmail.query.get(out_id)
            rec = Recording.query.get(out.recording_id)
            customer = Customer.query.get(out.customer_id)
            out.status, out.error = 'failed', str(e)[:500]
            if out.cost and customer:      # החזר החיוב
                customer.balance += out.cost
                db.session.add(Transaction(customer_id=customer.id, amount=out.cost, type='credit',
                                           recording_id=rec.id if rec else None,
                                           description=f'החזר - מייל לאיש קשר לא נשלח ({out.to_email})'))
            if rec:
                rec.status = 'error'
            db.session.commit()
            from services.recording_alerts import notify_recording_error
            notify_recording_error(rec.call_id if rec else '', f'מייל לאיש קשר לא נשלח ({out.to_email}): {e}. '
                                                               'החיוב הוחזר ללקוח.', mark_error=False)


# ---------------------------------------------------------------------------
# תגובות של הנמענים
# ---------------------------------------------------------------------------
def handle_reply(subject, sender_email, body_text):
    """מייל נכנס שהוא תשובה למייל ששלחנו בשם לקוח ([#M...] בנושא). שומר את התגובה
    בחשבון הלקוח. מחזיר True אם זוהה וטופל."""
    ref = ref_in(subject)
    if not ref:
        return False
    from app import db
    from models import OutgoingEmail, EmailReply
    outs = (OutgoingEmail.query.filter((OutgoingEmail.thread_ref == ref) | (OutgoingEmail.ref == ref))
            .order_by(OutgoingEmail.created_at.desc()).all())
    if not outs:
        return False
    # רק מי שהמייל נשלח אליו יכול להגיב (אחרת כל מי שמנחש מספר שרשור היה "מדבר" אל הלקוח)
    sender = normalize_email(sender_email)
    if sender not in {normalize_email(o.to_email) for o in outs}:
        log.warning(f'email_out: תגובה לשרשור {ref} מכתובת שלא נשלח אליה ({sender}) - לא נשמרה כתגובה')
        return False
    out = outs[0]
    db.session.add(EmailReply(outgoing_id=out.id, customer_id=out.customer_id,
                              from_email=normalize_email(sender_email), body=(body_text or '').strip()[:5000]))
    db.session.commit()
    log.info(f'email_out: תגובה ל-{out.ref} מ-{sender_email} נשמרה ללקוח {out.customer_id}')
    return True


def new_replies(customer):
    return [r for r in recent_replies(customer) if r['new']]


def recent_replies(customer, limit=15):
    """תגובות לשמיעה בטלפון: קודם החדשות (מהישנה לחדשה), ואחריהן שכבר נשמעו (מהחדשה לישנה)."""
    from models import EmailReply, OutgoingEmail
    new = (EmailReply.query.filter_by(customer_id=customer.id, heard_at=None)
           .order_by(EmailReply.created_at.asc()).limit(limit).all())
    old = (EmailReply.query.filter(EmailReply.customer_id == customer.id, EmailReply.heard_at.isnot(None))
           .order_by(EmailReply.created_at.desc()).limit(max(0, limit - len(new))).all())
    rows = new + old
    outs = {o.id: o for o in OutgoingEmail.query.filter(OutgoingEmail.id.in_([r.outgoing_id for r in rows])).all()} if rows else {}
    names = {c.email: c.name for c in contacts(customer)}
    res = []
    for r in rows:
        o = outs.get(r.outgoing_id)
        who = names.get(r.from_email) or (o.to_name if o and o.to_email == r.from_email else None) or r.from_email or ''
        body = (r.body or '')
        if len(body) > REPLY_TTS_MAX:
            body = body[:REPLY_TTS_MAX] + ' ... התגובה ארוכה, ההמשך במייל'
        res.append({'id': r.id, 'from': who, 'body': body, 'new': r.heard_at is None,
                    'can_reply': bool(EMAIL_RE.match(r.from_email or ''))})
    return res


def mark_replies_heard(customer, ids):
    from app import db
    from models import EmailReply
    if not ids:
        return
    (EmailReply.query.filter(EmailReply.customer_id == customer.id, EmailReply.id.in_(ids))
     .update({'heard_at': datetime.utcnow()}, synchronize_session=False))
    db.session.commit()


# ---------------------------------------------------------------------------
# מייל "אנשי קשר" מהלקוח למערכת
# ---------------------------------------------------------------------------
CONTACTS_SUBJECT_RE = re.compile(r'^\s*((?:re|fw|fwd)\s*:\s*)*(?P<phone>[0-9][0-9\-\s]{7,14}[0-9])?\s*אנשי\s*קשר', re.I)


def contacts_subject(subject):
    """נושא של פקודת אנשי קשר: "<טלפון> אנשי קשר" (כמו בשליחת קובץ לתמלול).
    מחזיר (True, טלפון או None) / (False, None)."""
    m = CONTACTS_SUBJECT_RE.match(subject or '')
    if not m:
        return False, None
    ph = re.sub(r'\D', '', m.group('phone') or '')
    return True, (ph or None)


def contacts_mailto(phone):
    """קישור לטיוטה מוכנה: נושא "<טלפון> אנשי קשר" וגוף לדוגמה."""
    from urllib.parse import quote
    from routes.email_inbound import TRANSCRIBE_INBOUND_EMAIL
    body = 'משה כהן - moshe@gmail.com\nמחק - old@gmail.com\n'
    return f'mailto:{TRANSCRIBE_INBOUND_EMAIL}?subject={quote(f"{phone} אנשי קשר")}&body={quote(body)}'


DELETE_RE = re.compile(r'^\s*(מחק|מחיקה|למחוק|הסר|הסרה)\b[\s:\-–]*(.*)$')


def handle_contacts_email(customer, body_text, reply_to_email):
    """מייל עם הנושא "אנשי קשר": כל שורה עם כתובת - הוספה (או עדכון שם); שורה שמתחילה
    ב"מחק" - מחיקה לפי כתובת או שם. בסוף נשלחת ללקוח הרשימה המעודכנת."""
    from app import db
    added, removed, errors = [], [], []
    for line in (body_text or '').splitlines():
        if not line.strip():
            continue
        m = DELETE_RE.match(line)
        if m:
            c = find_contact_by_text(customer, m.group(2))
            if c:
                removed.append(c.name or c.email)
                db.session.delete(c)
                db.session.commit()
            elif m.group(2).strip():
                errors.append(f'{m.group(2).strip()} - לא נמצא למחיקה')
            continue
        for name, email in parse_contact_lines(line):
            c, err = add_contact(customer, email, name)
            (added if c else errors).append(f'{email}' + (f' - {err}' if err else ''))
    send_contacts_summary(customer, reply_to_email, added=added, errors=errors, removed=removed)
    return len(added) + len(removed)


def send_contacts_summary(customer, to_email, added=None, errors=None, removed=None):
    from html import escape
    if not to_email:
        return
    rows = ''.join(f'<li>{escape(c.name or "")} - <span dir="ltr">{escape(c.email)}</span></li>' for c in contacts(customer))
    note = ''
    if added:
        note += f'<p style="color:#047857">נוספו / עודכנו: {escape(", ".join(added))}</p>'
    if removed:
        note += f'<p style="color:#047857">נמחקו: {escape(", ".join(removed))}</p>'
    if errors:
        note += f'<p style="color:#b91c1c">לא בוצע: {escape(", ".join(errors))}</p>'
    html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto;line-height:1.8">
<h2 style="color:#1d4ed8">אנשי הקשר שלכם לשליחת מיילים מהטלפון</h2>{note}
<ol style="padding-right:20px">{rows or '<li>עדיין אין אנשי קשר</li>'}</ol>
<div style="background:#eff6ff;border-right:4px solid #3b82f6;padding:12px 14px;border-radius:8px;font-size:14px;color:#1f2937">
<b>איך מעדכנים במייל:</b> שולחים מייל לכתובת המערכת מהמייל הרשום שלכם. בשורת הנושא - <b>מספר הטלפון שלכם ואחריו המילים אנשי קשר</b>
(למשל: <span dir="rtl" style="white-space:nowrap">{customer.phone} אנשי קשר</span>), בדיוק כמו בשליחת קובץ לתמלול. בגוף המייל שורה לכל פעולה:<br>
• להוספה: שם, מקף וכתובת - למשל <span dir="rtl">משה כהן - moshe@gmail.com</span><br>
• למחיקה: המילה מחק ואחריה הכתובת או השם - למשל <span dir="rtl">מחק - moshe@gmail.com</span><br>
<a href="{contacts_mailto(customer.phone)}" style="display:inline-block;margin:10px 0 6px;background:#2563eb;color:#fff;text-decoration:none;padding:9px 20px;border-radius:8px;font-weight:700">✉️ לטיוטה מוכנה</a><br>
אפשר גם בטלפון: שלוחה 1, אחר כך 6 ואז 3 - הוספה, שינוי שם או כתובת, ומחיקה.</div>
<p style="color:#6b7280;font-size:13px">מערכת תמלול פון 03-3131795</p></div>'''
    try:
        from services.email_layout import wrap_existing
        _send(to_email, 'תמלול פון - אנשי הקשר שלכם', wrap_existing(html, phone=customer.phone))
    except Exception as e:
        log.error(f'email_out: שליחת רשימת אנשי הקשר נכשלה: {e}')
