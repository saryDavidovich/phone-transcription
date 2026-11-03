"""
טבלאות אקסל מהקלטה (שלוחה 1 ← 3 בטלפון).

הלקוח מגדיר "תבנית": שם (מוקלט בקולו ומושמע לו בתפריט) ורשימת עמודות שהוא אומר
בקול ("1 שם, 2 טלפון, 3 סכום"). אחר כך הוא מקליט ומקריא את הנתונים בדיבור חופשי,
והמערכת:
  1. מתמללת את ההקלטה (אותו תמלול גימיני של תמלול רגיל - _gemini_from_url).
  2. מבקשת מגימיני לסדר את הטקסט לשורות לפי העמודות, במבנה JSON קבוע (response_schema)
     - כל עמודה מקבלת מפתח קבוע c1, c2... כך שגימיני חייב להחזיר בדיוק את העמודות.
     פרט שלא מתאים לאף עמודה נכנס ל"הערות נוספות"; ערך לא בטוח מסומן.
  3. "המשך טבלה קיימת" - השורות החדשות מתווספות לשורות ששמורות בתבנית;
     "טבלה חדשה" - השורות הקודמות נמחקות.
  4. בונה קובץ Excel (מימין לשמאל, כותרות, סכומים, תאים מסומנים, גיליון תמלול)
     ושולח ליעד הקבוע של הלקוח: מייל - הקובץ עצמו; פקס - הטבלה כ-PDF.
  5. מחייב לפי price_per_20min_excel (הגדרות ניהול), רק אחרי שהטבלה מוכנה.
"""
import io
import os
import re
import json
import math
import base64
import logging
from datetime import datetime

log = logging.getLogger(__name__)

GEMINI_MODEL = 'gemini-3.5-flash'
MAX_TEMPLATES = 9
MAX_COLUMNS = 20
NOTES_COLUMN = 'הערות נוספות'

_SUM_HINT = re.compile(r'סכום|מחיר|תשלום|שולם|כמות|סה"?כ|שקל|₪|עלות|הכנס|הוצא|תרומ|יתרה', re.IGNORECASE)
_PHONE_HINT = re.compile(r'טלפון|פלאפון|נייד|פקס|ת"?ז|זהות|מיקוד|חשבון|מספר', re.IGNORECASE)


# ---------------------------------------------------------------------------
# גימיני
# ---------------------------------------------------------------------------

def _client():
    from google import genai
    return genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))


def _download(url):
    import requests
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def _clean_columns(items):
    out = []
    for c in items:
        c = re.sub(r'^\s*(\d+|[א-י]\'?)\s*[.):\-–]?\s*', '', str(c))      # מספר / אות בתחילת השורה
        c = re.sub(r'\s+', ' ', c).strip(' .,:;-–"\'*•')[:60]
        if c and c not in out:
            out.append(c)
    return out[:MAX_COLUMNS]


def parse_columns_from_audio(rec_url):
    """הקלטת "1 שם, 2 טלפון, 3 כתובת..." -> ['שם', 'טלפון', 'כתובת'].
    טקסט פשוט, עמודה בכל שורה (כמו שאר הקריאות הקצרות לגימיני במערכת) - בלי JSON."""
    from google.genai import types as gtypes
    audio = _download(rec_url)
    log.info(f'excel columns: audio {len(audio)} bytes, starts with {audio[:4]!r}')
    if len(audio) < 1000 or audio[:4] != b'RIFF':
        log.error(f'excel columns: the recording did not download as WAV: {audio[:200]!r}')
        return []
    prompt = ('המשתמש מקריא רשימת שמות של עמודות לטבלת אקסל, בדרך כלל עם מספר לפני כל עמודה '
              '(למשל: "אחת שם, שתיים טלפון, שלוש כתובת, ארבע סכום").\n'
              'החזר את שמות העמודות בלבד, לפי הסדר, כל עמודה בשורה נפרדת, בעברית ובכתיב תקני, '
              'בלי המספרים ובלי שום טקסט נוסף. אל תוסיף עמודות שלא נאמרו.\n'
              'אם לא נאמרו שמות עמודות בכלל - החזר את המילה: ריק')
    # הלקוח חייב להישמר במשתנה: אובייקט זמני נסגר לפני שהבקשה נשלחת
    # ("Cannot send a request, as the client has been closed")
    client = _client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt, gtypes.Part.from_bytes(data=audio, mime_type='audio/wav')],
        config=gtypes.GenerateContentConfig(thinking_config=gtypes.ThinkingConfig(thinking_budget=0)),
    )
    text = (resp.text or '').strip()
    log.info(f'excel columns: gemini answered {text[:300]!r}')
    if not text or text == 'ריק':
        return []
    lines = [x for x in re.split(r'[\n,،]+', text) if x.strip()]
    return _clean_columns(lines)


def transcribe_name(rec_url):
    """תמלול קצר של שם התבנית (לממשק הניהול ולשם הקובץ)."""
    from google.genai import types as gtypes
    audio = _download(rec_url)
    # הלקוח חייב להישמר במשתנה: אובייקט זמני נסגר לפני שהבקשה נשלחת
    # ("Cannot send a request, as the client has been closed")
    client = _client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=['תמלל את המילים שנאמרו (שם של טבלה, מילה עד כמה מילים). החזר רק את הטקסט, בלי הסברים.',
                  gtypes.Part.from_bytes(data=audio, mime_type='audio/wav')],
        config=gtypes.GenerateContentConfig(thinking_config=gtypes.ThinkingConfig(thinking_budget=0)),
    )
    return re.sub(r'\s+', ' ', (resp.text or '')).strip(' ."\'')[:150]


def rows_from_transcript(transcript, columns):
    """מסדר את התמלול לשורות לפי העמודות. מחזיר (rows, uncertain):
    rows - [{עמודה: ערך}], uncertain - [set(עמודות לא בטוחות) לכל שורה]."""
    from google.genai import types as gtypes
    keys = [f'c{i + 1}' for i in range(len(columns))]
    mapping = '\n'.join(f'{k} = {c}' for k, c in zip(keys, columns))
    props = {k: {'type': 'STRING'} for k in keys}
    props['notes'] = {'type': 'STRING'}
    props['uncertain'] = {'type': 'ARRAY', 'items': {'type': 'STRING'}}
    schema = {'type': 'OBJECT', 'properties': {'rows': {'type': 'ARRAY', 'items': {
        'type': 'OBJECT', 'properties': props, 'required': keys}}}, 'required': ['rows']}
    today = datetime.now().strftime('%d/%m/%Y')
    prompt = f"""לפניך תמלול של הקלטה שבה אדם מקריא נתונים לטבלה. סדר את הנתונים לשורות.
העמודות (המפתח בתשובה = שם העמודה):
{mapping}

כללים:
- כל רשומה/פריט שהוקרא = שורה אחת, לפי סדר ההקראה. אל תמציא שורות או נתונים.
- שבץ כל פרט בעמודה המתאימה לו לפי המשמעות, גם אם נאמר בסדר אחר.
- מספרים שנאמרו במילים - כתוב בספרות (מאה ושמונים = 180). טלפונים - ספרות רצופות.
- תאריכים יחסיים ("היום", "מחר") - לפי התאריך {today}. תאריך עברי - השאר כפי שנאמר.
- תיקון שנאמר בדיבור ("לא, הסכום הוא 200") - כתוב רק את הערך המתוקן.
- פרט שלא נאמר - השאר ריק (""). פרט שלא מתאים לאף עמודה - כתוב ב-notes.
- ערך שלא שמעת בבירור או שאינך בטוח בו - הוסף את המפתח שלו (למשל "c2") לרשימה uncertain.
- הפקודות "שורה חדשה" / "הבא" / "סוף שורה" אינן נתונים - הן רק סימן למעבר שורה.

התמלול:
{transcript}"""
    # הלקוח חייב להישמר במשתנה: אובייקט זמני נסגר לפני שהבקשה נשלחת
    # ("Cannot send a request, as the client has been closed")
    client = _client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt],
        config=gtypes.GenerateContentConfig(
            response_mime_type='application/json',
            response_schema=schema,
            thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
        ),
    )
    data = json.loads(resp.text or '{}')
    rows, uncertain = [], []
    for r in (data.get('rows') or []):
        row = {c: str(r.get(k) or '').strip() for k, c in zip(keys, columns)}
        notes = str(r.get('notes') or '').strip()
        if notes:
            row[NOTES_COLUMN] = notes
        if not any(row.values()):
            continue
        rows.append(row)
        uncertain.append([columns[keys.index(k)] for k in (r.get('uncertain') or []) if k in keys])
    return rows, uncertain


# ---------------------------------------------------------------------------
# בניית הקבצים
# ---------------------------------------------------------------------------

def _as_number(value, column):
    """'180' / '1,250.5' / '₪180' -> מספר; טלפון / ת"ז / מספר שמתחיל ב-0 נשארים טקסט."""
    v = str(value).strip().replace('₪', '').replace('ש"ח', '').replace(',', '').strip()
    if not v or _PHONE_HINT.search(column or ''):
        return None
    if re.fullmatch(r'0\d+', v):
        return None
    if re.fullmatch(r'-?\d+(\.\d+)?', v):
        n = float(v)
        return int(n) if n.is_integer() else n
    return None


def build_xlsx(title, columns, rows, uncertain=None, transcript='', new_from=0):
    """rows: [{עמודה: ערך}]. uncertain: [[עמודות]] במקביל לשורות (רק לשורות החדשות).
    new_from: אינדקס השורה הראשונה שנוספה בהקלטה הזו (לסימון קל)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.comments import Comment
    from openpyxl.utils import get_column_letter

    cols = list(columns)
    if any(NOTES_COLUMN in r for r in rows) and NOTES_COLUMN not in cols:
        cols.append(NOTES_COLUMN)
    uncertain = uncertain or []

    wb = Workbook()
    ws = wb.active
    ws.title = 'טבלה'
    ws.sheet_view.rightToLeft = True
    thin = Side(style='thin', color='C9D3E0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill('solid', fgColor='0A58CA')
    warn_fill = PatternFill('solid', fgColor='FFF2CC')
    empty_fill = PatternFill('solid', fgColor='FDE2E1')
    new_fill = PatternFill('solid', fgColor='EEF4FB')

    ws.append(['#'] + cols)
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    ws.freeze_panes = 'A2'

    numeric_cols = set()
    for i, r in enumerate(rows):
        values = [i + 1]
        for c in cols:
            raw = r.get(c, '')
            n = _as_number(raw, c)
            values.append(n if n is not None else raw)
            if n is not None:
                numeric_cols.add(c)
        ws.append(values)
        excel_row = i + 2
        unc = uncertain[i - new_from] if 0 <= i - new_from < len(uncertain) else []
        for j, c in enumerate(cols):
            cell = ws.cell(row=excel_row, column=j + 2)
            cell.border = border
            cell.alignment = Alignment(vertical='top', wrap_text=True, horizontal='right')
            if isinstance(cell.value, str) and re.fullmatch(r'0\d+', cell.value or ''):
                cell.number_format = '@'
            if c in unc:
                cell.fill = warn_fill
                cell.comment = Comment('לא נשמע בבירור - כדאי לבדוק', 'תמלול פון')
            elif c != NOTES_COLUMN and (cell.value is None or cell.value == ''):
                cell.fill = empty_fill
            elif i >= new_from > 0:
                cell.fill = new_fill
        num_cell = ws.cell(row=excel_row, column=1)
        num_cell.border = border
        num_cell.alignment = Alignment(horizontal='center', vertical='top')

    # שורת סה"כ לעמודות של סכומים/כמויות
    sum_cols = [c for c in cols if c in numeric_cols and _SUM_HINT.search(c)]
    if rows and sum_cols:
        total_row = len(rows) + 2
        ws.cell(row=total_row, column=1, value='סה"כ').font = Font(bold=True)
        for j, c in enumerate(cols):
            if c in sum_cols:
                letter = get_column_letter(j + 2)
                cell = ws.cell(row=total_row, column=j + 2, value=f'=SUM({letter}2:{letter}{total_row - 1})')
                cell.font = Font(bold=True)
                cell.border = border

    # רוחב עמודות לפי התוכן
    ws.column_dimensions['A'].width = 6
    for j, c in enumerate(cols):
        longest = max([len(str(c))] + [len(str(r.get(c, ''))) for r in rows] or [8])
        ws.column_dimensions[get_column_letter(j + 2)].width = max(10, min(55, longest + 3))

    # מקרא
    legend_row = len(rows) + (4 if sum_cols and rows else 3)
    ws.cell(row=legend_row, column=2, value='מקרא:').font = Font(bold=True)
    ws.cell(row=legend_row + 1, column=2, value='לא נשמע בבירור - כדאי לבדוק').fill = warn_fill
    ws.cell(row=legend_row + 2, column=2, value='לא נאמר בהקלטה').fill = empty_fill
    if new_from > 0:
        ws.cell(row=legend_row + 3, column=2, value='שורות שנוספו בהקלטה האחרונה').fill = new_fill

    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = '1:1'

    if transcript:
        t = wb.create_sheet('תמלול ההקלטה')
        t.sheet_view.rightToLeft = True
        t.column_dimensions['A'].width = 110
        t['A1'] = f'התמלול המלא של ההקלטה האחרונה ({title})'
        t['A1'].font = Font(bold=True)
        for k, para in enumerate([p for p in transcript.split('\n') if p.strip()]):
            cell = t.cell(row=k + 3, column=1, value=para)
            cell.alignment = Alignment(wrap_text=True, vertical='top', horizontal='right')

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_to_pdf(xlsx_bytes, table_only=True):
    """המרת הטבלה ל-PDF (לשליחה בפקס) עם LibreOffice - כמו convert_docx_bytes_to_pdf_bytes.
    table_only - בלי גיליון התמלול (בפקס כל עמוד עולה כסף)."""
    import shutil
    if table_only:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(xlsx_bytes))
            for name in list(wb.sheetnames)[1:]:
                del wb[name]
            buf = io.BytesIO()
            wb.save(buf)
            xlsx_bytes = buf.getvalue()
        except Exception as e:
            log.warning(f'excel pdf: could not drop transcript sheet: {e}')
    import subprocess
    import tempfile
    import uuid
    from services.transcribe import _fax_pdf_semaphore
    if not shutil.which('soffice'):
        log.error('excel pdf: soffice not found')
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f'{uuid.uuid4().hex}.xlsx')
        with open(src, 'wb') as f:
            f.write(xlsx_bytes)
        with _fax_pdf_semaphore:
            r = subprocess.run(['soffice', '--headless', f'-env:UserInstallation=file://{tmp}/lo',
                                '--convert-to', 'pdf', '--outdir', tmp, src],
                               capture_output=True, text=True, timeout=120)
        out = os.path.splitext(src)[0] + '.pdf'
        if r.returncode != 0 or not os.path.exists(out):
            log.error(f'excel pdf convert failed: {r.stderr[:300]}')
            return None
        with open(out, 'rb') as f:
            return f.read()


def safe_filename(title):
    t = re.sub(r'[\\/:*?"<>|\n\r\t]+', ' ', title or '').strip()
    return (t or 'טבלה')[:60]


# ---------------------------------------------------------------------------
# שליחה
# ---------------------------------------------------------------------------

def send_excel_email(to, customer, title, xlsx_bytes, rows_total, rows_new, minutes):
    from services.transcribe import _is_system_inbound_address
    from services import email_layout as L
    from html import escape as _esc
    if _is_system_inbound_address(to):
        log.error(f'excel email: blocked sending to the system address ({to})')
        return False
    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email
    first = (customer.name or '').split()[0] if customer is not None and getattr(customer, 'name', None) else ''
    added = f'נוספו {rows_new} שורות' if rows_new != rows_total else f'{rows_total} שורות'
    card = (L.title(f'שלום {_esc(first)}, הטבלה מוכנה' if first else 'הטבלה מוכנה')
            + L.meta(f'{_esc(title)} · {added} · סה"כ בטבלה {rows_total} שורות')
            + L.text_block('קובץ האקסל מצורף למייל.\n'
                           'תאים בצהוב - ערך שלא נשמע בבירור, כדאי לבדוק. '
                           'תאים באדום בהיר - פרט שלא נאמר בהקלטה.\n'
                           'בלשונית "תמלול ההקלטה" שבקובץ נמצא התמלול המלא.')
            + L.signature())
    html = L.wrap(card, phone=getattr(customer, 'phone', None))
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=f'טבלת אקסל - {title}', html_content=html)
    msg.attachment = Attachment(
        FileContent(base64.b64encode(xlsx_bytes).decode()),
        FileName(f'{safe_filename(title)}.xlsx'),
        FileType('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        Disposition('attachment'))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
    log.info(f'excel email sent to {to} ({rows_total} rows)')
    return True


def send_excel_fax(to, xlsx_bytes, hint):
    from services.transcribe import send_pdf_fax
    pdf = xlsx_to_pdf(xlsx_bytes)
    if not pdf:
        return {'ok': False, 'campaign_id': None, 'error': 'בניית ה-PDF של הטבלה נכשלה'}
    return send_pdf_fax(to, pdf, filename_hint=hint)


# ---------------------------------------------------------------------------
# עיבוד הקלטה (נקרא מ-services.transcribe._process כש-tier='excel')
# ---------------------------------------------------------------------------

def template_columns(t):
    try:
        return [c for c in json.loads(t.columns_json or '[]') if c]
    except Exception:
        return []


def template_rows(t):
    try:
        rows = json.loads(t.rows_json or '[]')
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def process_excel_recording(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds):
    from app import db
    from models import Recording, Customer, Transaction, ExcelTemplate
    from services import transcribe as T
    from services.recording_alerts import notify_recording_error

    rec = Recording.query.filter_by(call_id=call_id).first()
    if not rec:
        return
    tmpl = ExcelTemplate.query.get(rec.excel_template_id) if rec.excel_template_id else None
    if not tmpl or tmpl.customer_id != customer_id:
        notify_recording_error(call_id, 'הקלטת אקסל: התבנית לא נמצאה (אולי נמחקה) - לא בוצע תמלול')
        return
    columns = template_columns(tmpl)
    if not columns:
        notify_recording_error(call_id, 'הקלטת אקסל: לתבנית אין עמודות')
        return
    title = tmpl.name or 'טבלה'
    price = float(T._get_setting('price_per_20min_excel', '1.50'))

    # בדיקת יתרה מקדימה (לפי אורך ההקלטה) - לפני שמשלמים לגימיני
    if not duration_seconds:
        try:
            from services import audio_prep
            duration_seconds = int(audio_prep.probe_duration(rec_url) or 0)
        except Exception:
            duration_seconds = 0
    customer = Customer.query.get(customer_id)
    if duration_seconds and customer:
        cost_pre = round(math.ceil(duration_seconds / 1200) * price, 2)
        if customer.balance < cost_pre:
            T._save_pending_payment(rec=rec, customer=customer, duration_seconds=duration_seconds,
                                    price_per_20min=price, delivery_method=delivery_method,
                                    delivered_to=delivered_to, transcription_tier='excel',
                                    language='he', output_language='he')
            return

    transcript, actual_duration, _is_video = T._gemini_from_url(rec_url, 'he', 'he')
    if actual_duration:
        duration_seconds = actual_duration
    db.session.remove()
    if not transcript:
        notify_recording_error(call_id, 'הקלטת אקסל: התמלול לא החזיר טקסט')
        return
    new_rows, uncertain = rows_from_transcript(transcript, columns)
    log.info(f'{call_id}: excel - {len(new_rows)} rows from template {tmpl.id}')

    rec = Recording.query.filter_by(call_id=call_id).first()
    tmpl = ExcelTemplate.query.get(rec.excel_template_id)
    customer = Customer.query.get(customer_id)
    if not new_rows:
        notify_recording_error(call_id, 'הקלטת אקסל: לא זוהו נתונים לטבלה בהקלטה (לא בוצע חיוב)')
        return

    units = max(1, math.ceil((duration_seconds or 0) / 1200))
    cost = round(units * price, 2)
    if customer.balance < cost:
        T._save_pending_payment(rec=rec, customer=customer, duration_seconds=duration_seconds,
                                price_per_20min=price, delivery_method=delivery_method,
                                delivered_to=delivered_to, transcription_tier='excel',
                                language='he', output_language='he')
        return

    old_rows = template_rows(tmpl) if rec.excel_mode == 'continue' else []
    all_rows = old_rows + new_rows
    xlsx = build_xlsx(title, columns, all_rows, uncertain, transcript, new_from=len(old_rows))

    tmpl.rows_json = json.dumps(all_rows, ensure_ascii=False)
    tmpl.rows_updated_at = datetime.utcnow()
    rec.transcript = transcript
    rec.summary = ''
    rec.excel_file = xlsx
    rec.status = 'transcribed'
    rec.cost = cost
    rec.duration_seconds = duration_seconds
    customer.balance -= cost
    db.session.add(Transaction(customer_id=customer_id, amount=-cost, type='debit',
                               description=f'טבלת אקסל "{title}" - {duration_seconds // 60} דקות, {len(new_rows)} שורות',
                               recording_id=rec.id))
    db.session.commit()

    try:
        if delivery_method == 'email' and delivered_to:
            send_excel_email(delivered_to, customer, title, xlsx, len(all_rows), len(new_rows), duration_seconds // 60)
        elif delivery_method == 'fax' and delivered_to:
            res = send_excel_fax(delivered_to, xlsx, f'table_{rec.id}.pdf')
            if res.get('ok') and res.get('campaign_id'):
                T._save_fax_campaign(call_id, res['campaign_id'])
            else:
                T._update_fax_status(call_id, status='error', note=res.get('error') or '')
    except Exception as e:
        log.error(f'{call_id}: excel delivery failed: {e}', exc_info=True)
        notify_recording_error(call_id, f'הקלטת אקסל: השליחה ללקוח נכשלה ({type(e).__name__})', mark_error=False)
        return
    rec = Recording.query.filter_by(call_id=call_id).first()
    if rec:
        rec.status = 'delivered'
        db.session.commit()
