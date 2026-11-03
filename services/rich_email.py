"""
תוכן מעוצב להודעה תפוצתית: העורך בממשק (contenteditable) מייצר HTML, וכאן הוא
מנוקה לרשימה סגורה של תגיות וסגנונות (כדי שלא ייכנס קוד/סקריפט ושהמייל ייראה
נכון ב-Gmail/Outlook), ועטוף בעיצוב האחיד של המיילים (services/email_layout).
"""
import re
from html import escape
from html.parser import HTMLParser

from services import email_layout as L

ALLOWED_TAGS = {'b', 'strong', 'i', 'em', 'u', 'br', 'p', 'div', 'span', 'h2', 'h3',
                'ul', 'ol', 'li', 'a', 'font', 'hr', 'blockquote'}
VOID_TAGS = {'br', 'hr'}
ALLOWED_STYLE = {'color', 'background-color', 'text-align', 'font-weight', 'font-style',
                 'text-decoration', 'text-decoration-line', 'font-size', 'padding', 'border-radius', 'display',
                 'margin-top', 'margin-bottom', 'background'}
_SAFE_VALUE = re.compile(r'^[#\w\s.,%()-]+$')
_COLOR = re.compile(r'^(#[0-9a-fA-F]{3,8}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|[a-zA-Z]+)$')
# גדלים שהעורך מייצר (font size 1-7) -> פיקסלים
_FONT_SIZES = {'1': '12px', '2': '14px', '3': '16px', '4': '18px', '5': '22px', '6': '28px', '7': '34px'}


def _clean_style(style):
    out = []
    for part in (style or '').split(';'):
        if ':' not in part:
            continue
        k, v = part.split(':', 1)
        k, v = k.strip().lower(), v.strip()
        if k in ALLOWED_STYLE and v and _SAFE_VALUE.match(v) and 'url' not in v.lower() and 'expression' not in v.lower():
            out.append(f'{k}:{v}')
    return ';'.join(out)


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = 0          # בתוך script/style - מדלגים על הכל

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object', 'embed', 'head', 'title'):
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        a = dict(attrs)
        styles = []
        if a.get('style'):
            styles.append(_clean_style(a['style']))
        if tag == 'font':                       # <font color size> מהעורך -> span עם style
            if a.get('color') and _COLOR.match(a['color']):
                styles.append(f'color:{a["color"]}')
            if a.get('size') in _FONT_SIZES:
                styles.append(f'font-size:{_FONT_SIZES[a["size"]]}')
            tag = 'span'
        if a.get('align') in ('right', 'left', 'center', 'justify'):
            styles.append(f'text-align:{a["align"]}')
        if tag == 'a':
            href = (a.get('href') or '').strip()
            if not re.match(r'^(https?://|mailto:|tel:)', href, re.I):
                href = ''
            styles.insert(0, f'color:{L.BLUE}')
            attr = f' href="{escape(href, quote=True)}" target="_blank"' if href else ''
        else:
            attr = ''
        if tag == 'h2':
            styles.insert(0, f'font-size:22px;color:{L.BLUE};margin:14px 0 6px')
        if tag == 'h3':
            styles.insert(0, f'font-size:18px;color:{L.INK};margin:12px 0 4px')
        if tag == 'blockquote':
            styles.insert(0, 'margin:10px 0;padding:8px 14px;background:#eef4fb;border-radius:10px')
        if tag in ('ul', 'ol'):
            styles.insert(0, 'margin:6px 0;padding-right:22px')
        if tag == 'hr':
            styles.insert(0, 'border:0;border-top:1px solid #cfe0fb;margin:14px 0')
        st = ';'.join(s for s in styles if s)
        self.out.append(f'<{tag}{attr}' + (f' style="{escape(st, quote=True)}"' if st else '') + '>')
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe', 'object', 'embed', 'head', 'title'):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == 'font':
            tag = 'span'
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f'</{t}>')
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data))

    def result(self):
        while self.stack:
            self.out.append(f'</{self.stack.pop()}>')
        return ''.join(self.out)


def clean_html(html):
    c = _Cleaner()
    c.feed(html or '')
    c.close()
    return c.result()


def is_rich(body):
    return bool(re.search(r'<\s*(b|strong|i|em|u|br|p|div|span|h2|h3|ul|ol|li|a|font|hr|blockquote)\b', body or '', re.I))


def body_block(body):
    """גוף ההודעה: HTML מהעורך (מנוקה), או טקסט רגיל (הודעות ישנות) עם ירידות שורה."""
    if not is_rich(body):
        return L.text_block(body)
    return (f'<div style="margin-top:16px;line-height:1.85;font-size:16px;color:{L.INK};'
            f'text-align:right;direction:rtl">{clean_html(body)}</div>')


def plain_text(body):
    """לבדיקת "האם ריק" - הטקסט בלי תגיות."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body or '')).replace('&nbsp;', ' ').strip()
