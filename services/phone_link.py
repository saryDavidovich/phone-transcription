"""
צירוף מספר טלפון לחשבון מהאזור האישי - באימות שיחה.

1. הלקוח (מחובר באתר) כותב מספר ולוחץ "אימות בשיחה" - נפתחת בקשה ל-30 דקות.
2. מתקשרים מהמספר הזה למערכת. בכניסה ה-IVR שואל את /api/customer/<phone>, רואה phone_link
   ומשמיע: "התקבלה בקשה מהאתר לצרף את המספר הזה לחשבון. לאישור הקישו 1, לדחייה 2".
3. הקשה על 1 - POST /api/phone-link/confirm (מהשרת של ה-IVR). רק מי שמחזיק בטלפון יכול לאשר.

מספר שכבר שייך ללקוח אחר עם פעילות (יתרה, הקלטות, תשלומים, מייל) - לא מועבר אוטומטית
(status=conflict, הסבר ללקוח באתר). מספר של "לקוח ריק" שנוצר רק כי התקשרו פעם למערכת - עובר.
הפיצ'ר מופעל במתג phone_link_ivr_on; הטלפון של המנהל תמיד מורשה, לבדיקות.
"""
import re
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
ADMIN_PHONE = '0527134491'
MINUTES = 30


def enabled_for(phone=None):
    from models import Settings
    row = Settings.query.filter_by(key='phone_link_ivr_on').first()
    return bool(row and row.value == '1') or phone == ADMIN_PHONE


def normalize(raw):
    from services.customer_phones import normalize_phone
    p = normalize_phone(raw)
    return p if re.fullmatch(r'0\d{8,9}', p or '') else None


def create(customer, raw_phone):
    """(בקשה, None) או (None, הודעת שגיאה)."""
    from app import db
    from models import PhoneLinkRequest
    from services.customer_phones import all_phones
    phone = normalize(raw_phone)
    if not phone:
        return None, 'מספר הטלפון לא תקין (10 ספרות, למשל 0501234567)'
    if not enabled_for(phone) and not enabled_for(customer.phone):
        return None, 'אימות טלפון בשיחה עוד לא פתוח. כתבו לנו בצור קשר ונצרף את המספר'
    if phone in all_phones(customer):
        return None, 'המספר הזה כבר משויך לחשבון שלך'
    now = datetime.utcnow()
    for old in PhoneLinkRequest.query.filter_by(customer_id=customer.id, status='pending').all():
        old.status = 'expired'
    req = PhoneLinkRequest(customer_id=customer.id, phone=phone, status='pending', created_at=now,
                           expires_at=now + timedelta(minutes=MINUTES))
    db.session.add(req)
    db.session.commit()
    return req, None


def pending_for_phone(phone):
    from models import PhoneLinkRequest
    if not phone or not enabled_for(phone):
        return None
    return (PhoneLinkRequest.query.filter(PhoneLinkRequest.phone == phone, PhoneLinkRequest.status == 'pending',
                                          PhoneLinkRequest.expires_at > datetime.utcnow())
            .order_by(PhoneLinkRequest.id.desc()).first())


def _is_empty_placeholder(c):
    """לקוח שנוצר אוטומטית רק כי התקשרו מהמספר - בלי שום דבר ששייך למישהו."""
    from models import Recording, Transaction
    if c.institution_id or c.is_institution_self or (c.email or '').strip() or (c.balance or 0) > 0.001:
        return False
    if c.extra_phones or getattr(c, 'account', None):
        return False
    if Recording.query.filter_by(customer_id=c.id).first() or Transaction.query.filter_by(customer_id=c.id).first():
        return False
    return True


def confirm(phone, req_id, accept=True):
    """נקרא מה-IVR. מחזיר 'done' / 'rejected' / 'conflict' / 'not_found'."""
    from app import db
    from models import PhoneLinkRequest, Customer, CustomerPhone
    from services.customer_phones import find_customer_and_row
    phone = normalize(phone)
    req = PhoneLinkRequest.query.filter_by(id=req_id).with_for_update().first()
    if not req or req.status != 'pending' or req.phone != phone or req.expires_at < datetime.utcnow():
        db.session.rollback()
        return 'not_found'
    req.done_at = datetime.utcnow()
    if not accept:
        req.status = 'rejected'
        db.session.commit()
        return 'rejected'
    target = Customer.query.get(req.customer_id)
    if not target or target.is_blocked:
        req.status = 'rejected'
        db.session.commit()
        return 'rejected'
    owner, row = find_customer_and_row(phone)
    if owner and owner.id != target.id:
        if row is not None or not _is_empty_placeholder(owner):
            req.status = 'conflict'
            db.session.commit()
            log.info(f'phone link {req.id}: {phone} belongs to customer {owner.id} - conflict')
            return 'conflict'
        owner.phone = None                     # הלקוח הריק שנוצר מהשיחה - מפנה את המספר
        db.session.flush()
    if not owner or owner.id != target.id:
        if not target.phone:
            target.phone = phone
        else:
            db.session.add(CustomerPhone(customer_id=target.id, phone=phone))
    req.status = 'done'
    db.session.commit()
    log.info(f'phone link {req.id}: {phone} linked to customer {target.id}')
    return 'done'
