"""
דוח עלויות ללקוח (כרטיס לקוח בממשק הניהול): קובץ Excel עם סיכום ופירוט של כל
התנועות בתקופה - טעינות, זיכויים וחיובים לפי סוג שירות. אפשר להוריד או לשלוח
ללקוח במייל.
"""
import io
import os
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IL = ZoneInfo('Asia/Jerusalem')


def _local(dt):
    return dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(IL) if dt else None


def category(t):
    """(סוג תנועה, קטגוריית שירות) לתצוגה."""
    d = t.description or ''
    if t.type == 'charge':
        return 'טעינה', 'טעינת ארנק'
    if t.type == 'credit':
        return 'זיכוי', 'זיכוי'
    if t.type in ('manuscript_dictation', 'manuscript_free'):
        return 'חיוב', 'קלדנות דיגיטלית'
    if t.type == 'manuscript_proofing':
        return 'חיוב', 'הגהה'
    if 'אקסל' in d:
        return 'חיוב', 'טבלת אקסל'
    if 'OCR' in d or 'כתב יד' in d:
        return 'חיוב', 'זיהוי כתב יד'
    if 'שליחת פקס' in d and '5 דקות' in d:
        return 'חיוב', 'תוספת פקס (חיוב לפי 5 דקות)'
    if 'פקס' in d:
        return 'חיוב', 'שליחה חוזרת בפקס'
    if 'תמלול' in d:
        return 'חיוב', 'תמלול הקלטה'
    return ('חיוב' if (t.amount or 0) < 0 else 'זיכוי'), 'אחר'


def clean_description(text):
    """בדוח ללקוח לא מופיעה המילה "הקראה" - השירות נקרא "קלדנות דיגיטלית"
    (גם בתנועות ישנות שנשמרו עם הנוסח הקודם)."""
    import re
    t = (text or '').replace('הקראת כתב יד', 'קלדנות דיגיטלית').replace('הקראת כתב-יד', 'קלדנות דיגיטלית')
    return re.sub(r'הקרא(ה|ת)', 'קלדנות דיגיטלית', t)


def parse_range(date_from, date_to):
    """'YYYY-MM-DD' (שעון ישראל) -> (start_utc, end_utc) לשאילתה; None = בלי גבול."""
    def to_utc(s, end=False):
        if not s:
            return None
        try:
            d = datetime.strptime(s, '%Y-%m-%d')
        except ValueError:
            return None
        if end:
            d += timedelta(days=1)
        return d.replace(tzinfo=IL).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
    return to_utc(date_from), to_utc(date_to, end=True)


def load_transactions(customer_id, date_from=None, date_to=None):
    from models import Transaction
    start, end = parse_range(date_from, date_to)
    q = Transaction.query.filter(Transaction.customer_id == customer_id)
    if start:
        q = q.filter(Transaction.created_at >= start)
    if end:
        q = q.filter(Transaction.created_at < end)
    return q.order_by(Transaction.created_at.asc()).all()


def period_label(txns, date_from, date_to):
    f = datetime.strptime(date_from, '%Y-%m-%d').strftime('%d/%m/%Y') if date_from else (
        _local(txns[0].created_at).strftime('%d/%m/%Y') if txns else '')
    t = datetime.strptime(date_to, '%Y-%m-%d').strftime('%d/%m/%Y') if date_to else datetime.now(IL).strftime('%d/%m/%Y')
    return f'{f} - {t}' if f else f'עד {t}'


def build_xlsx(customer, txns, date_from=None, date_to=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill('solid', fgColor='0A58CA')
    thin = Side(style='thin', color='C9D3E0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '₪#,##0.00;-₪#,##0.00'

    loads = sum(t.amount for t in txns if t.type == 'charge')
    credits = sum(t.amount for t in txns if category(t)[0] == 'זיכוי' and t.type != 'charge')
    usage = {}
    for t in txns:
        kind, cat = category(t)
        if kind == 'חיוב':
            usage[cat] = usage.get(cat, 0) + (t.amount or 0)
    total_usage = sum(usage.values())

    wb = Workbook()
    ws = wb.active
    ws.title = 'סיכום'
    ws.sheet_view.rightToLeft = True
    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 18
    ws['A1'] = 'דוח עלויות - תמלול פון'
    ws['A1'].font = Font(bold=True, size=16, color='0A58CA')
    rows = [('לקוח', customer.name or ''), ('טלפון', customer.phone or ''),
            ('תקופה', period_label(txns, date_from, date_to)),
            ('הופק בתאריך', datetime.now(IL).strftime('%d/%m/%Y %H:%M'))]
    r = 3
    for k, v in rows:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r, column=2, value=v)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='סכומים בתקופה').font = Font(bold=True, size=13)
    r += 1
    summary = [('טעינות ארנק', loads), ('זיכויים', credits)]
    summary += [(f'חיוב: {cat}', amt) for cat, amt in sorted(usage.items(), key=lambda x: x[1])]
    summary += [('סה"כ חיובים', total_usage)]
    for k, v in summary:
        c1 = ws.cell(row=r, column=1, value=k)
        c2 = ws.cell(row=r, column=2, value=round(v or 0, 2))
        c2.number_format = money
        c1.border = c2.border = border
        if k.startswith('סה"כ'):
            c1.font = c2.font = Font(bold=True)
        r += 1
    r += 1
    c1 = ws.cell(row=r, column=1, value='יתרה נוכחית בארנק')
    c2 = ws.cell(row=r, column=2, value=round(customer.balance or 0, 2))
    c1.font = c2.font = Font(bold=True, color='0A58CA')
    c2.number_format = money

    d = wb.create_sheet('פירוט')
    d.sheet_view.rightToLeft = True
    headers = ['תאריך', 'שעה', 'סוג', 'שירות', 'פירוט', 'סכום']
    d.append(headers)
    for cell in d[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal='center')
        cell.border = border
    d.freeze_panes = 'A2'
    for t in txns:
        lt = _local(t.created_at)
        kind, cat = category(t)
        d.append([lt.strftime('%d/%m/%Y') if lt else '', lt.strftime('%H:%M') if lt else '',
                  kind, cat, clean_description(t.description), round(t.amount or 0, 2)])
        row = d.max_row
        for col in range(1, 7):
            d.cell(row=row, column=col).border = border
        amt = d.cell(row=row, column=6)
        amt.number_format = money
        amt.font = Font(color='108A5C' if (t.amount or 0) > 0 else 'C0392B')
    if txns:
        tr = d.max_row + 1
        d.cell(row=tr, column=5, value='סה"כ בתקופה').font = Font(bold=True)
        c = d.cell(row=tr, column=6, value=f'=SUM(F2:F{tr - 1})')
        c.font = Font(bold=True)
        c.number_format = money
    for i, w in enumerate([12, 8, 9, 20, 60, 12], start=1):
        d.column_dimensions[get_column_letter(i)].width = w
    d.page_setup.orientation = 'landscape'
    d.page_setup.fitToWidth = 1
    d.page_setup.fitToHeight = 0
    d.sheet_properties.pageSetUpPr.fitToPage = True
    d.print_title_rows = '1:1'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {'loads': loads, 'credits': credits, 'usage': total_usage, 'count': len(txns)}


def filename(customer):
    return f'דוח עלויות - {customer.phone or customer.id}.xlsx'


def send_email(to, customer, xlsx, totals, period):
    from services.transcribe import _is_system_inbound_address
    from services import email_layout as L
    from html import escape as _esc
    if _is_system_inbound_address(to):
        raise ValueError('לא שולחים לכתובת המערכת')
    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email
    first = (customer.name or '').split()[0] if customer.name else ''
    body = (f'מצורף דוח העלויות שלך לתקופה {period}.\n\n'
            f'טעינות: ₪{totals["loads"]:.2f}\n'
            f'חיובים: ₪{abs(totals["usage"]):.2f}\n'
            f'יתרה נוכחית: ₪{(customer.balance or 0):.2f}\n\n'
            'בקובץ המצורף - סיכום לפי סוג שירות ופירוט מלא של כל התנועות.')
    card = (L.title(f'שלום {_esc(first)}, דוח העלויות שלך' if first else 'דוח העלויות שלך')
            + L.meta(_esc(period)) + L.text_block(body) + L.signature(can_reply=True))
    html = L.wrap(card, phone=customer.phone)
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=f'תמלול פון - דוח עלויות {period}', html_content=html)
    msg.attachment = Attachment(FileContent(base64.b64encode(xlsx).decode()), FileName(filename(customer)),
                                FileType('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                                Disposition('attachment'))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
    log.info(f'cost report emailed to customer {customer.id} ({totals["count"]} transactions)')
