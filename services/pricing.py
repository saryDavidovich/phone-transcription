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
