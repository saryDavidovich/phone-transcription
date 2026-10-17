"""
עיצוב אחיד לכל המיילים ללקוחות ("עיצוב ה" שנבחר):
פס כחול עליון עם שם השירות והטלפון, כרטיס לבן מעוגל עם התוכן, ומתחתיו שני
עיגולים - "לטעינת יתרה" (קישור אישי לנדרים פלוס, רק כשידוע הטלפון) ו"חדש:
מסמכים קודמים בשלוחה 8".

הכל ב-inline style ובטבלאות - כך מיילים מוצגים נכון ב-Gmail/Outlook ובתוכנות
שחוסמות CSS חיצוני ותמונות (גם ציור המסמך בעיגול בנוי מקווים, לא מתמונה).
"""
import os
import re
from html import escape
from urllib.parse import quote

BLUE = '#0a58ca'
INK = '#0b1f3a'
SOFT = '#43546b'
YELLOW = '#ffcc00'
PAGE = '#eef4fb'
PHONE_NUMBER = '03-3131795'


def topup_url(phone=None):
    """קישור לדף הטעינה בנדרים פלוס. עם טלפון - ממולא מראש; בלי טלפון - דף ריק
    שבו הלקוח מקליד את מספר הטלפון שלו בעצמו."""
    base = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    if not base:
        return ''
    phone = (phone or '').strip()
    return f'{base}/payment/nedarim/topup-link/{quote(phone)}' if phone else f'{base}/payment/nedarim/topup-link'


# --- חלקים לשימוש בתוך הכרטיס ---

def badge(premium):
    """תווית בולטת של סוג התמלול."""
    if premium:
        return (f'<div style="display:inline-block;background:{YELLOW};color:{INK};border:2px solid #e0b000;'
                f'font-weight:bold;font-size:15px;padding:5px 16px;border-radius:20px">&#9733; תמלול מקצועי</div>')
    return (f'<div style="display:inline-block;background:#e3edfc;color:{BLUE};border:2px solid {BLUE};'
            f'font-weight:bold;font-size:15px;padding:5px 16px;border-radius:20px">תמלול רגיל</div>')


def title(text, color=BLUE):
    return f'<div style="font-size:24px;font-weight:bold;color:{color};margin-top:12px">{text}</div>'


def meta(text):
    return f'<div style="font-size:15px;color:{SOFT};margin-top:4px">{text}</div>'


def text_block(text, escape_text=True):
    """גוף טקסט ארוך (תמלול/זיהוי) - מיושר לשני הצדדים, שומר ירידות שורה."""
    body = escape(text or '') if escape_text else (text or '')
    return (f'<div style="margin-top:16px;line-height:1.85;font-size:16px;text-align:justify;'
            f'text-justify:inter-word;white-space:pre-wrap;color:{INK}">{body}</div>')


def button(href, label):
    return (f'<div style="margin-top:20px"><a href="{escape(href, quote=True)}" style="display:inline-block;'
            f'background:{YELLOW};color:{INK};text-decoration:none;font-weight:bold;font-size:17px;'
            f'padding:13px 30px;border-radius:30px">{label}</a></div>')


def signature(can_reply=False):
    reply = 'ניתן להשיב למייל זה<br>' if can_reply else ''
    return (f'<div style="margin-top:18px;font-size:14px;color:{SOFT};line-height:1.7">'
            f'{reply}לשירותכם,<br>צוות תמלול פון</div>')


def _circles(phone):
    link = topup_url(phone)
    balance = ''
    if link:
        balance = (f'<td style="width:150px;padding:6px;vertical-align:top;text-align:center">'
                   f'<a href="{escape(link, quote=True)}" style="text-decoration:none;color:{INK}">'
                   f'<div style="width:56px;height:56px;line-height:56px;border-radius:50%;background:#ffffff;'
                   f'color:{BLUE};font-weight:bold;font-size:24px;margin:0 auto">&#8362;</div>'
                   f'<div style="font-size:14px;margin-top:6px;font-weight:bold;color:{BLUE};'
                   f'text-decoration:underline">לטעינת יתרה</div></a></td>')
    line = f'height:2px;background:{BLUE};margin-bottom:4px'
    docs = (f'<td style="width:150px;padding:6px;vertical-align:top;text-align:center">'
            f'<div style="width:56px;height:56px;border-radius:50%;background:#ffffff;margin:0 auto;'
            f'padding-top:13px;box-sizing:border-box">'
            f'<div style="width:22px;height:28px;border:2px solid {BLUE};border-radius:3px;margin:0 auto;'
            f'box-sizing:border-box;padding:5px 3px 0">'
            f'<div style="{line}"></div><div style="{line}"></div>'
            f'<div style="height:2px;background:{BLUE};width:60%"></div></div></div>'
            f'<div style="font-size:14px;margin-top:6px;color:{INK}">חדש: מסמכים קודמים<br>בשלוחה 8</div></td>')
    return (f'<table role="presentation" align="center" cellpadding="0" cellspacing="0" '
            f'style="margin:18px auto 0;text-align:center"><tr>{balance}{docs}</tr></table>')


BOX_BORDER = '#cfe0fb'
_SIDE_STRIPE = re.compile(r'border-(?:right|left)\s*:\s*\d+px\s+solid\s+#[0-9a-fA-F]{3,6}')


def wrap(card_html, phone=None, show_circles=True):
    """עוטף את תוכן המייל במסגרת האחידה. card_html - מה שנכנס לתוך הכרטיס הלבן.
    תיבות צבעוניות נשארות כמו שהן, אבל הפס הצדדי העבה שלהן מוחלף במסגרת
    שלמה ודקה בכחול בהיר - בלי צד אחד בולט."""
    card_html = _SIDE_STRIPE.sub(f'border:1px solid {BOX_BORDER}', card_html or '')
    circles = _circles(phone) if show_circles else ''
    return (f'<div dir="rtl" style="max-width:600px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;'
            f'color:{INK};background:{PAGE};text-align:right">'
            f'<div style="background:{BLUE};padding:16px 24px;color:#ffffff">'
            f'<span style="font-size:22px;font-weight:bold">תמלול פון</span>'
            f'<span style="float:left;font-size:14px;color:#cfe0fb;padding-top:5px">{PHONE_NUMBER}</span></div>'
            f'<div style="padding:20px 18px">'
            f'<div style="background:#ffffff;border-radius:18px;padding:22px;'
            f'box-shadow:0 2px 6px rgba(10,88,202,.12);line-height:1.7;font-size:15px">{card_html}</div>'
            f'{circles}</div></div>')


def wrap_existing(html, phone=None, show_circles=True):
    """למיילים קיימים: מכניס את ה-HTML הקיים כמו שהוא לתוך הכרטיס. מוריד את
    max-width/margin של העטיפה החיצונית שלו כדי שלא תצר את הכרטיס."""
    inner = re.sub(r'max-width:\s*\d+px;?\s*margin:\s*(?:0 )?auto;?', '', html or '', count=1)
    return wrap(inner, phone=phone, show_circles=show_circles)
