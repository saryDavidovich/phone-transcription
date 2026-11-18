"""
קובצי Word של אזור המוסד - עם מיתוג המוסד (לוגו + שם בכותרת העליונה), מימין לשמאל.
משמש להורדת תמלול בודד, להורדה מרוכזת (ZIP) ולקובץ מאוחד אחד.
"""
import io
import zipfile
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

FONT = 'David'


def _rtl(p):
    ppr = p._p.get_or_add_pPr()
    if ppr.find(qn('w:bidi')) is None:
        ppr.append(OxmlElement('w:bidi'))
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    return p


def _run(p, text, size=12, bold=False, color=None):
    r = p.add_run(text)
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.name = FONT
    rpr = r._r.get_or_add_rPr()
    fonts = rpr.find(qn('w:rFonts'))
    if fonts is None:
        fonts = OxmlElement('w:rFonts')
        rpr.append(fonts)
    for a in ('w:cs', 'w:ascii', 'w:hAnsi'):
        fonts.set(qn(a), FONT)
    rpr.append(OxmlElement('w:rtl'))
    cs = OxmlElement('w:szCs')
    cs.set(qn('w:val'), str(int(size * 2)))
    rpr.append(cs)
    if bold:
        rpr.append(OxmlElement('w:bCs'))
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    return r


def _logo_bytes(inst):
    try:
        return inst.logo_data if inst is not None and inst.logo_mime else None
    except Exception:
        return None


def new_document(inst):
    """מסמך ריק עם הכותרת העליונה של המוסד (לוגו + שם + שורה שנייה)."""
    doc = Document()
    sec = doc.sections[0]
    sec.right_margin = sec.left_margin = Cm(2.2)
    sec.top_margin = Cm(2.4)
    style = doc.styles['Normal']
    style.font.name = FONT
    style.font.size = Pt(12)
    if inst is not None:
        hp = sec.header.paragraphs[0]
        _rtl(hp)
        logo = _logo_bytes(inst)
        if logo:
            try:
                hp.add_run().add_picture(io.BytesIO(logo), height=Cm(1.5))
                hp.add_run('  ')
            except Exception:
                pass
        _run(hp, (getattr(inst, 'brand_title', None) or inst.name or '').strip(), size=14, bold=True, color='0E7A55')
        sub = (getattr(inst, 'brand_subtitle', None) or '').strip()
        if sub:
            _run(_rtl(sec.header.add_paragraph()), sub, size=9.5, color='64708A')
        # קו מפריד דק מתחת לכותרת
        last = sec.header.paragraphs[-1]._p.get_or_add_pPr()
        bdr = OxmlElement('w:pBdr')
        bottom = OxmlElement('w:bottom')
        for k, v in (('w:val', 'single'), ('w:sz', '6'), ('w:space', '4'), ('w:color', 'C9D3E3')):
            bottom.set(qn(k), v)
        bdr.append(bottom)
        last.append(bdr)
    return doc


def add_transcript(doc, title, transcript, meta=None, page_break=False):
    if page_break:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    _run(_rtl(doc.add_paragraph()), title or 'תמלול', size=16, bold=True)
    if meta:
        _run(_rtl(doc.add_paragraph()), meta, size=10, color='64708A')
    for line in (transcript or '').split('\n'):
        p = _rtl(doc.add_paragraph())
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.25
        _run(p, line)


def to_bytes(doc):
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def brand_of(inst):
    """כותרת המוסד לקובץ (לוגו + שם + שורה שנייה)."""
    if inst is None:
        return None
    return {'logo': _logo_bytes(inst), 'title': (getattr(inst, 'brand_title', None) or inst.name or '').strip(),
            'subtitle': (getattr(inst, 'brand_subtitle', None) or '').strip()}


def single(inst, title, transcript, meta=None):
    """אותו קובץ Word כמו בכל המערכת (services/transcribe._build_word_doc), עם כותרת המוסד."""
    from services.transcribe import _build_word_doc
    return _build_word_doc('', '', transcript, title=title or 'תמלול', brand=brand_of(inst),
                           items=[(title or 'תמלול', meta or '', transcript or '', 'פרטים')])


def merged(inst, items, heading=None):
    """items: [(title, meta, transcript)] - קובץ אחד, כל תמלול בעמוד חדש."""
    from services.transcribe import _build_word_doc
    return _build_word_doc('', '', '', brand=brand_of(inst), cover_title=heading or 'תמלולים',
                           items=[(t or 'תמלול', m or '', tx or '', 'פרטים') for t, m, tx in items])


def minutes_label(seconds):
    m = max(1, round((seconds or 0) / 60))
    return 'דקה אחת' if m == 1 else f'{m} דקות'


def safe_name(s, limit=80):
    bad = '\\/:*?"<>|\n\r\t'
    s = ''.join('_' if c in bad else c for c in (s or '')).strip(' .')
    return (s or 'תמלול')[:limit]


def zip_of(inst, items):
    """items: [(file_base_name, title, meta, transcript)] -> bytes של ZIP עם קובץ Word לכל אחד."""
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for base, title, meta, transcript in items:
            name = safe_name(base)
            n, k = name, 2
            while n in used:
                n = f'{name} ({k})'
                k += 1
            used.add(n)
            z.writestr(f'{n}.docx', single(inst, title, transcript, meta))
    return buf.getvalue()
