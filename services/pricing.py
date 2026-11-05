"""חישוב עלות לפי תווים - יחסי לכמות, מעוגל כלפי מעלה ל-10 אגורות."""
from decimal import Decimal, ROUND_CEILING


def char_cost(char_count, unit_size, price_per_unit):
    """עלות יחסית: char_count / unit_size * price_per_unit, ואז עיגול כלפי
    מעלה ל-10 אגורות הקרובות (0.10). 0 תווים = 0. החישוב ב-Decimal כדי שלא
    יהיו טעויות נקודה צפה (למשל 2000 תווים * 0.10 / 1000 = בדיוק 0.20)."""
    try:
        chars = int(char_count or 0)
    except (TypeError, ValueError):
        chars = 0
    if chars <= 0:
        return 0.0
    try:
        size = Decimal(str(unit_size))
        if size <= 0:
            size = Decimal(1000)
    except Exception:
        size = Decimal(1000)
    try:
        price = Decimal(str(price_per_unit))
    except Exception:
        price = Decimal('0.10')
    raw = Decimal(chars) * price / size              # בשקלים
    tenths = (raw * 10).to_integral_value(rounding=ROUND_CEILING)
    return float(tenths / 10)


# ---------------------------------------------------------------------------
# חיוב לפי דקות: כל לקוח לפי 20 דקות (ברירת מחדל - המחירון הרגיל) או לפי 5 דקות
# (נבחר בכרטיס הלקוח). לכל יחידה מחיר נפרד בהגדרות, לכל סוג שירות.
# לקוח לפי 5 דקות שהתמלול שלו נשלח בפקס, וההקלטה פחות מ-4 יחידות (פחות מ-20 דקות) -
# מחויב בנוסף בתוספת פקס (ברירת מחדל 30 אג'), בתנועה נפרדת.
# ---------------------------------------------------------------------------
import math

DEFAULT_PRICES = {20: {'basic': 0.90, 'premium': 1.90, 'video': 1.50, 'excel': 1.50}}
KIND_NAMES = {'basic': 'רגיל', 'premium': 'מקצועי', 'video': 'וידאו', 'excel': 'אקסל'}
FAX_SURCHARGE_DEFAULT = 0.30
FAX_SURCHARGE_MIN_UNITS = 4


def _setting(key, default):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value not in (None, '') else default


def unit_minutes(customer):
    return 5 if customer is not None and getattr(customer, 'billing_unit', None) == 5 else 20


def kind_for_tier(tier, is_video=False):
    if tier == 'excel':
        return 'excel'
    if tier == 'premium':
        return 'premium'
    return 'video' if is_video else 'basic'


def price_20(kind):
    try:
        return float(_setting(f'price_per_20min_{kind}', DEFAULT_PRICES[20][kind]))
    except (TypeError, ValueError):
        return DEFAULT_PRICES[20][kind]


def price_5(kind):
    """מחיר ל-5 דקות מההגדרות; כל עוד לא הוגדר - רבע ממחיר 20 הדקות."""
    v = _setting(f'price_per_5min_{kind}', '')
    try:
        return float(v) if v not in ('', None) else round(price_20(kind) / 4, 2)
    except (TypeError, ValueError):
        return round(price_20(kind) / 4, 2)


def unit_price(customer, kind):
    return price_5(kind) if unit_minutes(customer) == 5 else price_20(kind)


def units_for(customer, seconds):
    """מספר היחידות לחיוב (לפחות 1)."""
    secs = int(seconds or 0)
    return max(1, math.ceil(secs / (unit_minutes(customer) * 60))) if secs > 0 else 1


def cost_for(customer, seconds, kind):
    return round(units_for(customer, seconds) * unit_price(customer, kind), 2)


def fax_surcharge(customer, seconds, delivery_method):
    if unit_minutes(customer) != 5 or delivery_method != 'fax':
        return 0.0
    if units_for(customer, seconds) >= FAX_SURCHARGE_MIN_UNITS:
        return 0.0
    try:
        return round(float(_setting('fax_surcharge_5min', FAX_SURCHARGE_DEFAULT)), 2)
    except (TypeError, ValueError):
        return FAX_SURCHARGE_DEFAULT


def charge_fax_surcharge(customer, recording_id, seconds, delivery_method):
    """מחייב את תוספת הפקס (אם חלה) בתנועה נפרדת. מחזיר את הסכום שחויב."""
    amount = fax_surcharge(customer, seconds, delivery_method)
    if not amount or customer is None:
        return 0.0
    from app import db
    from models import Transaction
    customer.balance = (customer.balance or 0) - amount
    db.session.add(Transaction(customer_id=customer.id, amount=-amount, type='debit', recording_id=recording_id,
                               description='שליחת פקס (חיוב לפי 5 דקות, פחות מ-20 דקות)'))
    db.session.commit()
    return amount


def plan_label(customer):
    return 'לפי 5 דקות' if unit_minutes(customer) == 5 else 'לפי 20 דקות'
