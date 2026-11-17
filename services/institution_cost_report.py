"""
דוח עלויות למוסד (Excel) - כמו דוח העלויות של לקוח, אבל ברמת המוסד:
  סיכום | תנועות ביתרת המוסד | שימוש לפי תלמיד | תמלולי המוסד עצמו.

מקורות:
- טעינות: InstitutionChargeLog (נדרים פלוס = success, עדכון ידני של המנהל = manual).
- תמלולים של המוסד עצמו (לשונית "יצירת תמלול"): InstitutionUpload שחויבו.
- זיכויים לתלמידים / החזרות מתלמידים: Transaction של התלמידים עם התיאורים של המוסד.
- שימוש התלמידים: כל החיובים של כל תלמיד במוסד בתקופה (מהיתרה שלו).
"""
import io
import logging
from datetime import datetime

from services.cost_report import IL, _local, parse_range, category, clean_description

log = logging.getLogger(__name__)

INST_CREDIT_DESCS = ('זיכוי ע"י המוסד', 'זיכוי פתיחה ע"י המוסד', 'זיכוי חודשי אוטומטי ע"י המוסד')
INST_TAKE_PREFIX = 'הורדת יתרה ע"י המוסד'


def _period(date_from, date_to):
    f = datetime.strptime(date_from, '%Y-%m-%d').strftime('%d/%m/%Y') if date_from else ''
    t = datetime.strptime(date_to, '%Y-%m-%d').strftime('%d/%m/%Y') if date_to else datetime.now(IL).strftime('%d/%m/%Y')
    return f'{f} - {t}' if f else f'עד {t}'


def _in_range(q, col, start, end):
    if start:
        q = q.filter(col >= start)
    if end:
        q = q.filter(col < end)
    return q


def collect(inst, date_from=None, date_to=None, group_id=None):
    """group_id: דוח לקבוצה אחת - רק התלמידים שלה (בלי טעינות ותמלולים של המוסד עצמו)."""
    from models import InstitutionChargeLog, InstitutionUpload, Transaction, Customer, StudentGroup
    start, end = parse_range(date_from, date_to)
    events = []     # (dt, סוג, פירוט, סכום ביתרת המוסד)

    groups = {g.id: g.name for g in StudentGroup.query.filter_by(institution_id=inst.id).all()}
    charges = [] if group_id else _in_range(InstitutionChargeLog.query.filter(
        InstitutionChargeLog.institution_id == inst.id,
        InstitutionChargeLog.status.in_(('success', 'manual'))), InstitutionChargeLog.created_at, start, end).all()
    for c in charges:
        events.append((c.created_at, 'טעינה' if c.status == 'success' else 'עדכון ידני',
                       'טעינת יתרה בכרטיס אשראי (נדרים פלוס)' if c.status == 'success' else 'עדכון יתרה ע"י מנהל המערכת',
                       round(c.amount or 0, 2)))

    uploads = [] if group_id else _in_range(InstitutionUpload.query.filter(
        InstitutionUpload.institution_id == inst.id, InstitutionUpload.cost.isnot(None),
        db_or_charged()), InstitutionUpload.created_at, start, end).order_by(InstitutionUpload.created_at).all()
    for u in uploads:
        mins = max(1, round((u.duration_seconds or 0) / 60))
        events.append((u.created_at, 'תמלול המוסד',
                       f'{"תמלול מקצועי" if u.tier == "premium" else "תמלול"} "{u.original_filename or ""}" ({mins} דקות)',
                       -round(u.cost or 0, 2)))

    sq = Customer.query.filter(Customer.institution_id == inst.id)
    if group_id:
        sq = sq.filter(Customer.student_group_id == group_id)
    students = sq.all()
    sid = [s.id for s in students]
    by_id = {s.id: s for s in students}
    st_rows = {}
    if sid:
        txns = _in_range(Transaction.query.filter(Transaction.customer_id.in_(sid)),
                         Transaction.created_at, start, end).order_by(Transaction.created_at).all()
        for t in txns:
            s = by_id.get(t.customer_id)
            name = (s.student_display_name or s.name or s.phone or f'#{s.id}') if s else ''
            d = t.description or ''
            row = st_rows.setdefault(t.customer_id, {'name': name, 'number': getattr(s, 'student_number', '') or '',
                                                     'phone': s.phone if s else '', 'credited': 0.0, 'used': 0.0,
                                                     'group': groups.get(getattr(s, 'student_group_id', None), '') if s else '',
                                                     'self_loaded': 0.0, 'count': 0})
            if d in INST_CREDIT_DESCS:
                events.append((t.created_at, 'זיכוי לתלמיד', f'{name}: {d}', -round(t.amount or 0, 2)))
                row['credited'] += t.amount or 0
            elif d.startswith(INST_TAKE_PREFIX):
                events.append((t.created_at, 'החזרה מתלמיד', f'{name}: הורדת יתרה (חזר למוסד)', round(-(t.amount or 0), 2)))
                row['credited'] += t.amount or 0
            elif t.type == 'charge' and (t.amount or 0) > 0:
                row['self_loaded'] += t.amount
            elif (t.amount or 0) < 0:
                row['used'] += -(t.amount or 0)
                row['count'] += 1
        for s in students:
            r = st_rows.get(s.id)
            if r is not None:
                r['balance'] = round(s.balance or 0, 2)
    events.sort(key=lambda e: e[0] or datetime.min)
    return events, st_rows, uploads, groups


def db_or_charged():
    from app import db
    from models import InstitutionUpload
    # charged=True (חדש), או תמלול ישן שהסתיים לפני שהיה הסימון
    return db.or_(InstitutionUpload.charged.is_(True),
                  db.and_(InstitutionUpload.charged.is_(None), InstitutionUpload.status == 'done'))


def build_xlsx(inst, date_from=None, date_to=None, group_id=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    events, st_rows, uploads, groups = collect(inst, date_from, date_to, group_id)
    group_name = groups.get(group_id) if group_id else None
    head_fill = PatternFill('solid', fgColor='0A58CA')
    thin = Side(style='thin', color='C9D3E0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '₪#,##0.00;-₪#,##0.00'

    def header(ws, cols, widths):
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = head_fill
            c.alignment = Alignment(horizontal='center')
            c.border = border
        ws.freeze_panes = ws.cell(row=ws.max_row + 1, column=1).coordinate
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def sum_of(kind):
        return round(sum(e[3] for e in events if e[1] == kind), 2)

    loads = sum_of('טעינה') + sum_of('עדכון ידני')
    own = -sum_of('תמלול המוסד')
    credited = -sum_of('זיכוי לתלמיד')
    returned = sum_of('החזרה מתלמיד')
    students_used = round(sum(r['used'] for r in st_rows.values()), 2)

    wb = Workbook()
    ws = wb.active
    ws.title = 'סיכום'
    ws.sheet_view.rightToLeft = True
    ws.column_dimensions['A'].width = 38
    ws.column_dimensions['B'].width = 18
    ws['A1'] = 'דוח עלויות למוסד - תמלול פון'
    ws['A1'].font = Font(bold=True, size=16, color='0A58CA')
    r = 3
    for k, v in (('מוסד', inst.name or ''), ('קבוצה', group_name or 'כל התלמידים'), ('תקופה', _period(date_from, date_to)),
                 ('הופק בתאריך', datetime.now(IL).strftime('%d/%m/%Y %H:%M'))):
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r, column=2, value=v)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='יתרת המוסד בתקופה').font = Font(bold=True, size=13)
    r += 1
    rows = [('טעינות יתרה', loads), ('תמלולים של המוסד עצמו', -own),
            ('זיכויים לתלמידים', -credited), ('החזרות מתלמידים', returned),
            ('סה"כ שינוי ביתרת המוסד', round(loads - own - credited + returned, 2)), None,
            ('שימוש התלמידים (מהיתרה שלהם)', students_used)]
    for item in rows:
        if item is None:
            r += 1
            continue
        k, v = item
        c1 = ws.cell(row=r, column=1, value=k)
        c2 = ws.cell(row=r, column=2, value=round(v or 0, 2))
        c2.number_format = money
        c1.border = c2.border = border
        if k.startswith('סה"כ'):
            c1.font = c2.font = Font(bold=True)
        r += 1
    r += 1
    c1 = ws.cell(row=r, column=1, value='יתרת המוסד הנוכחית')
    c2 = ws.cell(row=r, column=2, value=round(inst.balance or 0, 2))
    c1.font = c2.font = Font(bold=True, color='0A58CA')
    c2.number_format = money

    d = wb.create_sheet('תנועות המוסד')
    d.sheet_view.rightToLeft = True
    header(d, ['תאריך', 'שעה', 'סוג', 'פירוט', 'סכום'], [12, 8, 16, 60, 13])
    for dt, kind, desc, amt in events:
        lt = _local(dt)
        d.append([lt.strftime('%d/%m/%Y') if lt else '', lt.strftime('%H:%M') if lt else '', kind,
                  clean_description(desc), amt])
        for col in range(1, 6):
            d.cell(row=d.max_row, column=col).border = border
        c = d.cell(row=d.max_row, column=5)
        c.number_format = money
        c.font = Font(color='108A5C' if amt > 0 else 'C0392B')
    if events:
        tr = d.max_row + 1
        d.cell(row=tr, column=4, value='סה"כ בתקופה').font = Font(bold=True)
        c = d.cell(row=tr, column=5, value=f'=SUM(E2:E{tr - 1})')
        c.font = Font(bold=True)
        c.number_format = money

    s = wb.create_sheet('שימוש לפי תלמיד')
    s.sheet_view.rightToLeft = True
    header(s, ['תלמיד', 'מספר תלמיד', 'טלפון', 'קבוצה', 'זוכה ע"י המוסד', 'טען בעצמו', 'פעולות בתשלום', 'סה"כ שימוש', 'יתרה נוכחית'],
           [26, 12, 14, 16, 15, 13, 14, 13, 13])
    for row in sorted(st_rows.values(), key=lambda x: -x['used']):
        s.append([row['name'], row['number'], row['phone'], row.get('group') or '', round(row['credited'], 2),
                  round(row['self_loaded'], 2), row['count'], round(row['used'], 2), row.get('balance', 0)])
        for col in range(1, 10):
            s.cell(row=s.max_row, column=col).border = border
        for col in (5, 6, 8, 9):
            s.cell(row=s.max_row, column=col).number_format = money
    if st_rows:
        tr = s.max_row + 1
        s.cell(row=tr, column=1, value='סה"כ').font = Font(bold=True)
        for col, letter in ((5, 'E'), (6, 'F'), (7, 'G'), (8, 'H')):
            c = s.cell(row=tr, column=col, value=f'=SUM({letter}2:{letter}{tr - 1})')
            c.font = Font(bold=True)
            if col != 7:
                c.number_format = money

    if groups and not group_id:
        g = wb.create_sheet('לפי קבוצה')
        g.sheet_view.rightToLeft = True
        header(g, ['קבוצה', 'תלמידים פעילים בתקופה', 'זוכה ע"י המוסד', 'סה"כ שימוש'], [24, 20, 16, 14])
        agg = {}
        for row in st_rows.values():
            a = agg.setdefault(row.get('group') or 'בלי קבוצה', [0, 0.0, 0.0])
            a[0] += 1
            a[1] += row['credited']
            a[2] += row['used']
        for name, (n, cr, us) in sorted(agg.items(), key=lambda x: -x[1][2]):
            g.append([name, n, round(cr, 2), round(us, 2)])
            for col in range(1, 5):
                g.cell(row=g.max_row, column=col).border = border
            for col in (3, 4):
                g.cell(row=g.max_row, column=col).number_format = money

    if not group_id:
        u = wb.create_sheet('תמלולי המוסד')
        u.sheet_view.rightToLeft = True
        header(u, ['תאריך', 'קובץ', 'סוג', 'דקות', 'עלות'], [16, 44, 12, 8, 12])
    for x in uploads:
        lt = _local(x.created_at)
        u.append([lt.strftime('%d/%m/%Y %H:%M') if lt else '', x.original_filename or '',
                  'מקצועי' if x.tier == 'premium' else 'רגיל', max(1, round((x.duration_seconds or 0) / 60)),
                  round(x.cost or 0, 2)])
        u.cell(row=u.max_row, column=5).number_format = money

    for sheet in wb.worksheets[1:]:
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.print_title_rows = '1:1'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def filename(inst, group_name=None):
    return f'דוח עלויות - {inst.name or inst.id}' + (f' - {group_name}' if group_name else '') + '.xlsx'
