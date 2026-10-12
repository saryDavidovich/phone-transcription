"""מספרי טלפון נוספים על לקוח אחד (למשל בעל ואשה על חשבון חיוב אחד).

עיקרון: לקוח אחד = יתרה אחת + היסטוריה אחת. מספרים נוספים (models.CustomerPhone)
מזוהים בכל מקום במערכת כמו המספר הראשי. כל קוד החיוב נשאר כמו שהיה - רק זיהוי
הלקוח לפי טלפון עובר דרך find_customer_by_phone.

משלוח: כברירת מחדל כל התמלולים נשלחים למייל/פקס של הלקוח. אם Customer.phones_separate
דלוק, מספר שהוגדר לו מייל/פקס משלו מקבל לשם את התמלולים שלו (ושולח מהמייל הזה).
"""
import re
from app import db


def normalize_phone(raw):
    """אותו נרמול כמו בשאר המערכת: ספרות בלבד, פורמט מקומי 0XXXXXXXXX."""
    phone = (raw or '').strip().replace('-', '').replace(' ', '')
    if phone.startswith('+972'):
        phone = '0' + phone[4:]
    elif phone.startswith('972'):
        phone = '0' + phone[3:]
    return phone


def find_customer_and_row(phone):
    """מחזיר (לקוח, שורת CustomerPhone או None). שורה=None כשזה המספר הראשי."""
    from models import Customer, CustomerPhone
    if not phone:
        return None, None
    c = Customer.query.filter_by(phone=phone).first()
    if c:
        return c, None
    row = CustomerPhone.query.filter_by(phone=phone).first()
    if row:
        return row.customer, row
    return None, None


def find_customer_by_phones(candidates):
    """כמו find_customer_by_phone, לרשימת צורות כתיב אפשריות של אותו מספר."""
    from models import Customer, CustomerPhone
    cands = [c for c in (candidates or []) if c]
    if not cands:
        return None
    c = Customer.query.filter(Customer.phone.in_(cands)).first()
    if c:
        return c
    row = CustomerPhone.query.filter(CustomerPhone.phone.in_(cands)).first()
    return row.customer if row else None


def find_customer_by_phone(phone):
    return find_customer_and_row(phone)[0]


def phone_taken(phone):
    """האם המספר כבר בשימוש (כמספר ראשי של לקוח או כמספר נוסף)."""
    c, _row = find_customer_and_row(phone)
    return c


def all_phones(customer):
    out = [customer.phone] if customer.phone else []
    out += [r.phone for r in customer.extra_phones]
    return out


def customer_emails(customer):
    """כל כתובות המייל המורשות של החשבון (לאימות שולח במייל נכנס)."""
    emails = set()
    if (customer.email or '').strip():
        emails.add(customer.email.strip().lower())
    for r in customer.extra_phones:
        if (r.email or '').strip():
            emails.add(r.email.strip().lower())
    return emails


def sender_is_registered(customer, sender_email):
    s = (sender_email or '').strip().lower()
    return bool(s) and s in customer_emails(customer)


def effective_contact(customer, phone):
    """(email, fax, delivery_method) שחל על שיחה/בקשה מהמספר הזה."""
    email, fax, method = customer.email, customer.fax, customer.delivery_method
    if getattr(customer, 'phones_separate', False) and phone and phone != customer.phone:
        row = next((r for r in customer.extra_phones if r.phone == phone), None)
        if row:
            if (row.email or '').strip():
                email = row.email
            if (row.fax or '').strip():
                fax = row.fax
            if row.delivery_method:
                method = row.delivery_method
    return email, fax, method


def delivery_email_for_sender(customer, sender_email):
    """לאן שולחים תמלול של בקשה שהגיעה במייל: במצב נפרד - לשולח עצמו (אם הוא אחד
    ממיילי החשבון), אחרת למייל הלקוח."""
    if getattr(customer, 'phones_separate', False) and sender_is_registered(customer, sender_email):
        return (sender_email or '').strip()
    return customer.email


def add_phone(customer, raw_phone, label=None, email=None, fax=None):
    """מוסיף מספר נוסף ללקוח. מחזיר (True/False, הודעה)."""
    from models import CustomerPhone
    phone = normalize_phone(raw_phone)
    if not re.fullmatch(r'0\d{8,9}', phone):
        return False, 'מספר הטלפון לא תקין (צריך 9-10 ספרות, למשל 0501234567)'
    other = phone_taken(phone)
    if other:
        if other.id == customer.id:
            return False, 'המספר הזה כבר משויך ללקוח הזה'
        return False, f'המספר הזה כבר קיים אצל לקוח אחר ({other.name or other.phone}). אי אפשר לחבר מספר שכבר רשום ללקוח נפרד.'
    db.session.add(CustomerPhone(customer_id=customer.id, phone=phone, label=(label or '').strip() or None,
                                 email=(email or '').strip() or None, fax=(fax or '').strip() or None))
    db.session.commit()
    return True, f'המספר {phone} חובר ללקוח'
