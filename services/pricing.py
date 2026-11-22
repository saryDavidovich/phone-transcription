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

DEFAULT_PRICES = {20: {'basic': 0.90, 'premium': 1.90, 'video': 1.50, 'excel': 1.50, 'phone_edit': 1.50}}
KIND_NAMES = {'basic': 'רגיל', 'premium': 'מקצועי', 'video': 'וידאו', 'excel': 'אקסל', 'phone_edit': 'הקלטה עם עריכה'}
FAX_SURCHARGE_DEFAULT = 0.30
FAX_SURCHARGE_MIN_UNITS = 4


def _setting(key, default):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value not in (None, '') else default


def unit_minutes(customer):
    """20 או 5. לקוח עם מחירון אישי (5/20) - לפיו; תלמיד בלי מחירון אישי - לפי המוסד שלו;
    מוסד (Institution) - לפי ההגדרה שלו."""
    if customer is None:
        return 20
    bu = getattr(customer, 'billing_unit', None)
    if bu in (5, 20):
        return bu
    if getattr(customer, 'institution_id', None):
        try:
            inst = customer.institution
        except Exception:
            inst = None
        if inst is not None and getattr(inst, 'billing_unit', None) == 5:
            return 5
    return 20


def kind_for_tier(tier, is_video=False):
    if tier == 'excel':
        return 'excel'
    if tier == 'phone_edit':
        return 'phone_edit'
    if tier == 'premium':
        return 'premium'
    return 'video' if is_video else 'basic'


def price_20(kind):
    if kind == 'phone_edit' and _setting('price_per_20min_phone_edit', '') in ('', None):
        return price_20('excel')                       # כל עוד לא הוגדר מחיר משלה - כמו אקסל
    try:
        return float(_setting(f'price_per_20min_{kind}', DEFAULT_PRICES[20][kind]))
    except (TypeError, ValueError):
        return DEFAULT_PRICES[20][kind]


def price_5(kind):
    """מחיר ל-5 דקות מההגדרות; כל עוד לא הוגדר - רבע ממחיר 20 הדקות."""
    v = _setting(f'price_per_5min_{kind}', '')
    if kind == 'phone_edit' and v in ('', None) and _setting('price_per_20min_phone_edit', '') in ('', None):
        return price_5('excel')
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


# ---------------------------------------------------------------- מבצע: הקלטה ראשונה ביום חינם
# בזמן המבצע, ההקלטה הראשונה של כל יום (תמלול רגיל) לא מחויבת על היחידות שבתוך "דקות החינם"
# (ברירת מחדל 20): לקוח לפי 20 דקות - יחידה אחת חינם (21 דקות = יחידה אחת בתשלום במקום 2);
# לקוח לפי 5 דקות - 4 יחידות חינם (21 דקות = יחידה אחת בתשלום במקום 5). שאר ההקלטות באותו יום - כרגיל.
# הגדרות (בממשק > הגדרות > מבצע): promo_free_on, promo_free_scope ('all' / 'marked'),
# promo_free_until ('YYYY-MM-DD', כולל, שעון ישראל), promo_free_minutes (20),
# promo_free_needs_balance ('1' = רק מי שיש לו יתרה בארנק; '0' = כל אחד, גם בלי יתרה).
# 'marked' = רק לקוחות שסומנו בכרטיס הלקוח (Customer.promo_free). לא חל על תלמידי מוסד.
# ניצול היום מסומן על ההקלטה (Recording.promo_free_seconds > 0).
def _il_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Jerusalem'))


def promo_settings():
    return {
        'on': _setting('promo_free_on', '0') == '1',
        'scope': _setting('promo_free_scope', 'marked') or 'marked',
        'until': (_setting('promo_free_until', '') or '').strip(),
        'minutes': int(float(_setting('promo_free_minutes', '20') or 20)),
        'needs_balance': _setting('promo_free_needs_balance', '1') != '0',
        'ask_first': _setting('promo_free_ask_first', '1') != '0',   # שאלה בשיחה לפני ההקלטה (ברירת מחדל: כן)
    }


def promo_active(customer):
    if customer is None or getattr(customer, 'institution_id', None) or getattr(customer, 'is_blocked', False):
        return False
    s = promo_settings()
    if not s['on'] or s['minutes'] <= 0:
        return False
    if s['until'] and _il_now().strftime('%Y-%m-%d') > s['until']:
        return False
    if s['needs_balance'] and (customer.balance or 0) < unit_price(customer, 'basic'):
        return False             # רק מי שיש לו ארנק טעון (לפחות מחיר יחידה, כמו כניסה רגילה להקלטה)
    return s['scope'] == 'all' or bool(getattr(customer, 'promo_free', False))


def _promo_used_today(customer, exclude_recording_id=None):
    from zoneinfo import ZoneInfo
    from models import Recording
    start = _il_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
    q = Recording.query.filter(Recording.customer_id == customer.id, Recording.created_at >= start,
                               Recording.promo_free_seconds > 0)
    if exclude_recording_id:
        q = q.filter(Recording.id != exclude_recording_id)
    return q.first() is not None


def promo_used_today(customer):
    """לפתיח בשיחה: הלקוח כבר מימש היום את ההקלטה החינמית - גם אם היא עוד בתמלול
    (הסימון הרגיל, promo_free_seconds, נכתב רק כשהתמלול מסתיים). מתאפס בחצות (שעון ישראל)."""
    from zoneinfo import ZoneInfo
    from models import Recording
    if customer is None:
        return False
    if _promo_used_today(customer):
        return True
    if not promo_active(customer):
        return False
    start = _il_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
    busy = ('recording', 'processing', 'queued', 'transcribing', 'pending', 'queued_maintenance', 'pending_payment')
    return Recording.query.filter(Recording.customer_id == customer.id, Recording.created_at >= start,
                                  Recording.status.in_(busy), Recording.promo_declined.isnot(True),
                                  Recording.transcription_tier.notin_(('premium', 'excel', 'phone_edit'))).first() is not None


def promo_free_units(customer, exclude_recording_id=None):
    """כמה יחידות חינם להקלטה הבאה (0 אם אין מבצע עבורו או שכבר ניצל היום)."""
    if not promo_active(customer) or _promo_used_today(customer, exclude_recording_id):
        return 0
    return max(1, promo_settings()['minutes'] // unit_minutes(customer))


def promo_seconds_left(customer, exclude_recording_id=None):
    """לשלוחות: כמה דקות (בשניות) ההקלטה הבאה חינם היום (0 = אין)."""
    return promo_free_units(customer, exclude_recording_id) * unit_minutes(customer) * 60


def basic_cost_with_promo(customer, seconds, recording_id=None):
    """עלות תמלול רגיל אחרי המבצע: (עלות, שניות_חינם). שניות_חינם > 0 מסמן שהמבצע של היום נוצל."""
    secs = int(seconds or 0)
    free_u = promo_free_units(customer, recording_id)
    if free_u <= 0 or secs <= 0:
        return cost_for(customer, secs, 'basic'), 0
    units = units_for(customer, secs)
    used_u = min(units, free_u)
    cost = round((units - used_u) * unit_price(customer, 'basic'), 2)
    return cost, max(1, min(secs, used_u * unit_minutes(customer) * 60))


def promo_fax_fee(customer, seconds, delivery_method, free_seconds):
    """הקלטה במבצע שנשלחת בפקס - חיוב הפקס הרגיל (30 אג'), גם ללקוח לפי 20 דקות."""
    if delivery_method != 'fax' or not free_seconds:
        return 0.0
    if fax_surcharge(customer, seconds, delivery_method):
        return 0.0                       # כבר מחויב בתוספת הפקס הרגילה
    try:
        return round(float(_setting('fax_surcharge_5min', FAX_SURCHARGE_DEFAULT)), 2)
    except (TypeError, ValueError):
        return FAX_SURCHARGE_DEFAULT
