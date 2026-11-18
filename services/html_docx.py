"""
עורך ה-Word באתר (routes/institution_portal.editor_*): ניקוי ה-HTML שהדפדפן שולח, המרה לטקסט פשוט,
והמרה לקובץ Word אמיתי (python-docx) - עם אותה כותרת ממותגת כמו בכל המערכת.

רק רשימה סגורה של תגיות ותכונות עוברת (בלי סקריפטים, אירועים או קישורים מסוכנים) - ה-HTML נשמר
אצלנו ומוצג שוב בעורך, ולכן הניקוי כאן הוא שכבת האבטחה.
"""
import html
import re
from html.parser import HTMLParser

ALLOWED = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'br', 'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'sub', 'sup',
           'span', 'font', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'hr', 'blockquote', 'a', 'mark'}
VOID = {'br', 'hr'}
DROP_CONTENT = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'head', 'title',
                'button', 'input', 'select', 'textarea', 'form'}
BLOCK = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'li', 'blockquote', 'td', 'th'}
STYLE_PROPS = {'color', 'background-color', 'font-size', 'font-family', 'text-align', 'direction', 'line-height',
               'font-weight', 'font-style', 'text-decoration', 'text-decoration-line', 'margin-right', 'padding-right'}
_SAFE_VAL = re.compile(r'^[#a-zA-Z0-9 .,%()\-\'"֐-׿]{1,80}$')
MAX_HTML = 3_000_000


def _clean_style(st):
    out = []
    for part in (st or '').split(';'):
        if ':' not in part:
            continue
        k, v = part.split(':', 1)
        k, v = k.strip().lower(), v.strip()
        if k in STYLE_PROPS and _SAFE_VAL.match(v) and 'url' not in v.lower() and 'expression' not in v.lower():
            out.append(f'{k}: {v}')
    return '; '.join(out)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.drop = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop += 1
            return
        if self.drop or tag not in ALLOWED:
            return
        a = []
        for k, v in attrs:
            k = (k or '').lower()
            v = v or ''
            if k == 'style':
                v = _clean_style(v)
                if v:
                    a.append(f'style="{html.escape(v, quote=True)}"')
            elif k in ('dir',) and v.lower() in ('rtl', 'ltr', 'auto'):
                a.append(f'dir="{v.lower()}"')
            elif k == 'align' and v.lower() in ('right', 'left', 'center', 'justify'):
                a.append(f'align="{v.lower()}"')
            elif k in ('colspan', 'rowspan') and v.isdigit() and int(v) < 50:
                a.append(f'{k}="{v}"')
            elif k == 'href' and tag == 'a' and re.match(r'^(https?://|mailto:)', v.strip(), re.I):
                a.append(f'href="{html.escape(v.strip(), quote=True)}" rel="noopener" target="_blank"')
            elif k == 'class' and v in ('page-break', 'ts', 'speaker'):
                a.append(f'class="{v}"')
            elif k == 'data-t' and re.fullmatch(r'\d{1,6}(\.\d{1,2})?', v):
                a.append(f'data-t="{v}"')
            elif k == 'color' and tag == 'font' and _SAFE_VAL.match(v):
                a.append(f'color="{html.escape(v, quote=True)}"')
        self.out.append(f'<{tag}{" " + " ".join(a) if a else ""}>')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and self.stack and self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag not in ALLOWED or tag in VOID:
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f'</{t}>')
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f'</{self.stack.pop()}>')
        return ''.join(self.out)


def sanitize(raw):
    p = _Sanitizer()
    p.feed((raw or '')[:MAX_HTML])
    p.close()
    return p.result()


def text_to_html(text):
    """תמלול רגיל -> HTML התחלתי לעורך: פסקה לכל קטע, ושורה ריקה נשמרת."""
    paras = re.split(r'\n\s*\n', (text or '').strip())
    out = []
    for para in paras:
        lines = [html.escape(x) for x in para.split('\n')]
        out.append('<p>' + '<br>'.join(lines) + '</p>')
    return ''.join(out) or '<p><br></p>'


# ------------------------------------------------------------------ טקסט פשוט
class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.li_n = []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')
        elif tag in ('ol', 'ul'):
            self.li_n.append(0 if tag == 'ol' else None)
        elif tag == 'li':
            self.parts.append('\n')
            if self.li_n and self.li_n[-1] is not None:
                self.li_n[-1] += 1
                self.parts.append(f'{self.li_n[-1]}. ')
            else:
                self.parts.append('• ')
        elif tag in ('td', 'th'):
            self.parts.append('\t')
        elif tag in BLOCK or tag in ('tr', 'hr', 'table'):
            self.parts.append('\n\n' if tag in ('p', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'table') else '\n')

    def handle_endtag(self, tag):
        if tag in ('ol', 'ul') and self.li_n:
            self.li_n.pop()
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data.replace('\xa0', ' '))


def to_text(clean_html):
    p = _Text()
    p.feed(clean_html or '')
    t = ''.join(p.parts)
    t = re.sub(r'[ \t]+\n', '\n', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


# ------------------------------------------------------------------ Word
class _Node:
    __slots__ = ('tag', 'attrs', 'kids', 'text')

    def __init__(self, tag, attrs=None, text=None):
        self.tag, self.attrs, self.kids, self.text = tag, dict(attrs or {}), [], text


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, attrs)
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].kids.append(_Node('#text', text=data))


def _style(attrs):
    out = {}
    for part in (attrs.get('style') or '').split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            out[k.strip().lower()] = v.strip()
    return out


def _color(v):
    v = (v or '').strip().lower()
    m = re.fullmatch(r'#([0-9a-f]{6})', v) or re.fullmatch(r'#([0-9a-f]{3})', v)
    if m:
        h = m.group(1)
        return h if len(h) == 6 else ''.join(c * 2 for c in h)
    m = re.fullmatch(r'rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)', v)
    if m:
        if m.group(4) is not None and float(m.group(4)) == 0:
            return None
        return '%02x%02x%02x' % tuple(min(255, int(m.group(i))) for i in (1, 2, 3))
    names = {'red': 'ff0000', 'blue': '0000ff', 'green': '008000', 'black': '000000', 'yellow': 'ffff00',
             'orange': 'ffa500', 'purple': '800080', 'gray': '808080', 'grey': '808080'}
    return names.get(v)


_HL = {'ffff00': 'YELLOW', 'fef08a': 'YELLOW', 'fde68a': 'YELLOW', '00ff00': 'BRIGHT_GREEN', 'bbf7d0': 'BRIGHT_GREEN',
       '00ffff': 'TURQUOISE', 'bae6fd': 'TURQUOISE', 'ff00ff': 'PINK', 'fbcfe8': 'PINK', 'ff0000': 'RED', 'fecaca': 'RED',
       'c7d2fe': 'TURQUOISE', 'e5e7eb': 'GRAY_25', 'd9d9d9': 'GRAY_25'}


def _nearest_highlight(hexv):
    if hexv in _HL:
        return _HL[hexv]
    r, g, b = int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
    opts = {'YELLOW': (255, 255, 0), 'BRIGHT_GREEN': (0, 255, 0), 'TURQUOISE': (0, 255, 255), 'PINK': (255, 0, 255),
            'RED': (255, 0, 0), 'GRAY_25': (217, 217, 217), 'BLUE': (0, 0, 255)}
    return min(opts, key=lambda k: sum((a - b2) ** 2 for a, b2 in zip(opts[k], (r, g, b))))


def _font_pt(v):
    v = (v or '').strip().lower()
    m = re.fullmatch(r'([\d.]+)(pt|px)?', v)
    if not m:
        return {'x-small': 9, 'small': 10, 'medium': 12, 'large': 14, 'x-large': 18, 'xx-large': 24}.get(v)
    n = float(m.group(1))
    return round(n * 0.75, 1) if m.group(2) == 'px' else n


def build_docx(base_docx_bytes, clean_html, marker):
    """מחליף את פסקת ה-marker במסמך הבסיס (עם הכותרת הממותגת) בתוכן העשיר מה-HTML."""
    import io
    from copy import deepcopy
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_COLOR_INDEX
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document(io.BytesIO(base_docx_bytes))
    anchor = None
    for p in doc.paragraphs:
        if marker in p.text:
            anchor = p
            break
    if anchor is None:
        return base_docx_bytes
    font_name = None
    for r in anchor.runs:
        font_name = r.font.name or font_name
    base_size = None
    for r in anchor.runs:
        base_size = r.font.size or base_size
    body_pPr = deepcopy(anchor._p.pPr) if anchor._p.pPr is not None else None

    tree = _Tree()
    tree.feed(clean_html or '')

    def new_par(before=True):
        el = OxmlElement('w:p')
        anchor._p.addprevious(el)
        from docx.text.paragraph import Paragraph
        par = Paragraph(el, anchor._parent)
        if body_pPr is not None:
            el.insert(0, deepcopy(body_pPr))
        return par

    def set_rtl_par(par, align=None, rtl=True):
        pPr = par._p.get_or_add_pPr()
        if rtl and pPr.find(qn('w:bidi')) is None:
            pPr.insert(0, OxmlElement('w:bidi'))
        if not rtl:
            b = pPr.find(qn('w:bidi'))
            if b is not None:
                pPr.remove(b)
        if align:
            jc = pPr.find(qn('w:jc'))
            if jc is None:
                jc = OxmlElement('w:jc')
                pPr.append(jc)
            # בפסקת RTL וורד מפרש right/left לפי כיוון הקריאה: start = ימין הפיזי
            val = {'right': 'start' if rtl else 'right', 'left': 'end' if rtl else 'left', 'center': 'center',
                   'justify': 'both'}.get(align)
            if val:
                jc.set(qn('w:val'), val)

    def run(par, text, fmt):
        r = par.add_run(text)
        f = r.font
        if font_name:
            f.name = fmt.get('family') or font_name
            rPr = r._r.get_or_add_rPr()
            rf = rPr.find(qn('w:rFonts'))
            if rf is None:
                rf = OxmlElement('w:rFonts')
                rPr.insert(0, rf)
            for k in ('w:cs', 'w:ascii', 'w:hAnsi'):
                rf.set(qn(k), fmt.get('family') or font_name)
        size = fmt.get('size') or (base_size.pt if base_size else None)
        if size:
            f.size = Pt(size)
            rPr = r._r.get_or_add_rPr()
            szcs = OxmlElement('w:szCs')
            szcs.set(qn('w:val'), str(int(round(size * 2))))
            rPr.append(szcs)
        if fmt.get('b'):
            f.bold = True
            rPr = r._r.get_or_add_rPr()
            rPr.append(OxmlElement('w:bCs'))
        if fmt.get('i'):
            f.italic = True
            rPr = r._r.get_or_add_rPr()
            rPr.append(OxmlElement('w:iCs'))
        if fmt.get('u'):
            f.underline = True
        if fmt.get('s'):
            f.strike = True
        if fmt.get('sup'):
            f.superscript = True
        if fmt.get('sub'):
            f.subscript = True
        if fmt.get('color'):
            f.color.rgb = RGBColor.from_string(fmt['color'].upper())
        if fmt.get('bg'):
            f.highlight_color = getattr(WD_COLOR_INDEX, _nearest_highlight(fmt['bg']))
        if fmt.get('rtl', True):
            rPr = r._r.get_or_add_rPr()
            rPr.append(OxmlElement('w:rtl'))
        return r

    def inline_fmt(node, fmt):
        f = dict(fmt)
        t = node.tag
        if t in ('b', 'strong'):
            f['b'] = True
        elif t in ('i', 'em'):
            f['i'] = True
        elif t == 'u':
            f['u'] = True
        elif t in ('s', 'strike', 'del'):
            f['s'] = True
        elif t == 'sup':
            f['sup'] = True
        elif t == 'sub':
            f['sub'] = True
        elif t == 'mark':
            f['bg'] = 'ffff00'
        elif t == 'a':
            f['u'] = True
            f['color'] = '1d4ed8'
        st = _style(node.attrs)
        if node.tag == 'font' and node.attrs.get('color'):
            c = _color(node.attrs['color'])
            if c:
                f['color'] = c
        if st.get('color') and _color(st['color']):
            f['color'] = _color(st['color'])
        if st.get('background-color'):
            c = _color(st['background-color'])
            if c and c != 'ffffff':
                f['bg'] = c
        if st.get('font-size') and _font_pt(st['font-size']):
            f['size'] = _font_pt(st['font-size'])
        if st.get('font-weight') in ('bold', '700', '800', '900'):
            f['b'] = True
        if st.get('font-style') == 'italic':
            f['i'] = True
        if 'underline' in (st.get('text-decoration') or st.get('text-decoration-line') or ''):
            f['u'] = True
        if 'line-through' in (st.get('text-decoration') or st.get('text-decoration-line') or ''):
            f['s'] = True
        if st.get('font-family'):
            fam = st['font-family'].split(',')[0].strip().strip('\'"')
            if fam and fam.lower() not in ('inherit', 'sans-serif', 'serif', 'monospace'):
                f['family'] = fam
        return f

    def emit_inline(par, node, fmt):
        for k in node.kids:
            if k.tag == '#text':
                txt = re.sub(r'[ \t\r\n]+', ' ', k.text)
                if txt:
                    run(par, txt, fmt)
            elif k.tag == 'br':
                par.add_run().add_break()
            elif k.tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'ul', 'ol', 'table', 'hr', 'li'):
                emit_inline(par, k, fmt)       # בלוק בתוך שורה (נדיר) - משטחים
            else:
                emit_inline(par, k, inline_fmt(k, fmt))

    def par_props(node):
        st = _style(node.attrs)
        align = st.get('text-align') or node.attrs.get('align')
        rtl = (node.attrs.get('dir') or st.get('direction') or 'rtl') != 'ltr'
        return (align if align in ('right', 'left', 'center', 'justify') else None), rtl

    HSIZE = {'h1': 20, 'h2': 16, 'h3': 14, 'h4': 13}

    def emit_block(node, fmt, list_info=None):
        t = node.tag
        if t == '#text':
            if node.text.strip():
                par = new_par()
                set_rtl_par(par)
                run(par, re.sub(r'\s+', ' ', node.text), fmt)
            return
        if t == 'hr':
            par = new_par()
            if node.attrs.get('class') == 'page-break':
                par.add_run().add_break(WD_BREAK.PAGE)
            else:
                pPr = par._p.get_or_add_pPr()
                bdr = OxmlElement('w:pBdr')
                bot = OxmlElement('w:bottom')
                for k, v in (('w:val', 'single'), ('w:sz', '6'), ('w:space', '1'), ('w:color', 'BFBFBF')):
                    bot.set(qn(k), v)
                bdr.append(bot)
                pPr.append(bdr)
            return
        if t in ('ul', 'ol'):
            n = 0
            for k in node.kids:
                if k.tag == 'li':
                    n += 1
                    emit_block(k, fmt, ('•' if t == 'ul' else f'{n}.'))
                elif k.tag != '#text' or k.text.strip():
                    emit_block(k, fmt)
            return
        if t == 'table':
            rows = []

            def collect(nd):
                for k in nd.kids:
                    if k.tag == 'tr':
                        rows.append([c for c in k.kids if c.tag in ('td', 'th')])
                    elif k.tag in ('thead', 'tbody'):
                        collect(k)
            collect(node)
            rows = [r for r in rows if r]
            if not rows:
                return
            cols = max(len(r) for r in rows)
            par = new_par()
            tbl = doc.add_table(rows=len(rows), cols=cols)
            try:
                tbl.style = 'Table Grid'
            except Exception:
                pass
            tblPr = tbl._tbl.tblPr
            bv = OxmlElement('w:bidiVisual')
            tblPr.append(bv)
            for ri, r in enumerate(rows):
                for ci, cell in enumerate(r):
                    c = tbl.cell(ri, ci)
                    cp = c.paragraphs[0]
                    set_rtl_par(cp)
                    emit_inline(cp, cell, dict(fmt, b=True) if cell.tag == 'th' else fmt)
            par._p.addprevious(tbl._tbl)
            par._p.getparent().remove(par._p)
            return
        if t in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'li', 'root'):
            # div שמכיל בלוקים - מפרקים
            if any(k.tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'table', 'blockquote', 'hr') for k in node.kids) \
                    and t in ('div', 'root', 'blockquote', 'li', 'p'):
                buf = _Node('p', node.attrs)
                for k in node.kids:
                    if k.tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'table', 'blockquote', 'hr'):
                        if buf.kids:
                            emit_block(buf, fmt, list_info)
                            buf = _Node('p', node.attrs)
                            list_info = None
                        emit_block(k, fmt)
                    else:
                        buf.kids.append(k)
                if buf.kids and any(k.tag != '#text' or k.text.strip() for k in buf.kids):
                    emit_block(buf, fmt, list_info)
                return
            par = new_par()
            align, rtl = par_props(node)
            set_rtl_par(par, align, rtl)
            f = inline_fmt(node, dict(fmt, rtl=rtl))
            if t in HSIZE:
                f.update(b=True, size=HSIZE[t])
                par.paragraph_format.space_before = Pt(10)
                par.paragraph_format.keep_with_next = True
            if t == 'blockquote':
                par.paragraph_format.right_indent = Pt(24) if rtl else None
                f['i'] = True
            if list_info:
                par.paragraph_format.right_indent = Pt(18) if rtl else None
                run(par, list_info + ' ', f)
            st = _style(node.attrs)
            lh = st.get('line-height')
            if lh:
                try:
                    par.paragraph_format.line_spacing = float(lh)
                except ValueError:
                    pass
            emit_inline(par, node, f)
            return
        # תגית שורה ברמה העליונה
        par = new_par()
        set_rtl_par(par)
        emit_inline(par, _wrap(node), fmt)

    def _wrap(node):
        w = _Node('span')
        w.kids = [node]
        return w

    emit_block(tree.root, {})
    anchor._p.getparent().remove(anchor._p)
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()
