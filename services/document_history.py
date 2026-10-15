"""
שלוחה 8 ב-IVR: הלקוח שומע את היסטוריית המסמכים שלו ויכול לקבל כל אחד שוב.

סוגי מסמכים: תמלול הקלטה (Recording), זיהוי כתב-יד (OcrResult) וקלדנות
דיגיטלית (ManuscriptPage). השליחה החוזרת היא ליעד הנוכחי של הלקוח:
  - מייל - חינם.
  - פקס - בתשלום (ברירת מחדל 30 אג', הגדרה 'price_fax_resend'). ה-IVR קודם
    מקבל needs_confirm, משמיע ללקוח את העלות, ורק אחרי "הקש 1" שולח שוב עם
    confirm=1. החיוב נעשה רק אחרי שהפקס יצא בהצלחה.
מגבלה: עד MAX_RESENDS_PER_DAY שליחות חוזרות ללקוח ביום (לכל תהליך שרת) - כדי
שאי אפשר יהיה להציף לקוח במיילים/פקסים דרך ה-API (שעדיין פתוח בלי
API_SHARED_SECRET).
"""
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MAX_DOCS = 30
MAX_RESENDS_PER_DAY = 10
_IL = ZoneInfo('Asia/Jerusalem')
_MONTHS = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט',
           'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']
TYPE_NAMES = {'recording': 'תמלול הקלטה', 'ocr': 'זיהוי כתב יד', 'manuscript': 'קלדנות דיגיטלית'}

_resend_lock = threading.Lock()
_resend_log = {}   # customer_id -> [timestamps]


def fax_resend_price():
    from routes.admin import get_setting
    try:
        return round(float(get_setting('price_fax_resend', '0.30')), 2)
    except (TypeError, ValueError):
        return 0.30


def _spoken_date(dt):
    if not dt:
        return ''
    loc = dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(_IL)
    return f'מתאריך {loc.day} ב{_MONTHS[loc.month - 1]}, בשעה {loc.hour}:{loc.minute:02d}'


def _minutes_phrase(seconds):
    m = max(1, round((seconds or 0) / 60))
    return 'באורך דקה אחת' if m == 1 else f'באורך {m} דקות'


def list_documents(customer):
    """מחזיר רשימה (מהחדש לישן) של {'type','id','date','spoken'} - רק מסמכים שיש להם תוכן."""
    from sqlalchemy.orm import defer
    from models import Recording, OcrResult, ManuscriptPage
    docs = []
    recs = (Recording.query.options(defer(Recording.file_data))
            .filter(Recording.customer_id == customer.id,
                    Recording.status.in_(('delivered', 'transcribed')),
                    Recording.transcript.isnot(None), Recording.transcript != '')
            .order_by(Recording.created_at.desc()).limit(MAX_DOCS).all())
    for r in recs:
        docs.append({'type': 'recording', 'id': r.id, 'date': r.created_at,
                     'spoken': f'{TYPE_NAMES["recording"]} {_spoken_date(r.created_at)}, {_minutes_phrase(r.duration_seconds)}'})
    ocrs = (OcrResult.query.options(defer(OcrResult.file_data))
            .filter(OcrResult.customer_id == customer.id, OcrResult.status == 'completed',
                    OcrResult.ocr_text.isnot(None), OcrResult.ocr_text != '')
            .order_by(OcrResult.created_at.desc()).limit(MAX_DOCS).all())
    for o in ocrs:
        docs.append({'type': 'ocr', 'id': o.id, 'date': o.created_at,
                     'spoken': f'{TYPE_NAMES["ocr"]} {_spoken_date(o.created_at)}'})
    pages = (ManuscriptPage.query.options(defer(ManuscriptPage.file_data), defer(ManuscriptPage.proof_file_data),
                                          defer(ManuscriptPage.proof_final_file_data))
             .filter(ManuscriptPage.customer_id == customer.id, ManuscriptPage.status == 'done',
                     ManuscriptPage.content.isnot(None))
             .order_by(ManuscriptPage.created_at.desc()).limit(MAX_DOCS).all())
    for p in pages:
        docs.append({'type': 'manuscript', 'id': p.id, 'date': p.created_at,
                     'spoken': f'{TYPE_NAMES["manuscript"]} {_spoken_date(p.created_at)}'})
    docs.sort(key=lambda d: d['date'] or datetime.min, reverse=True)
    docs = docs[:MAX_DOCS]
    # שדות להשמעה בהקלטות (ה-IVR מרכיב: "מסמך" + מספר + סוג + "מתאריך" + תאריך עברי + אורך)
    rec_secs = {r.id: (r.duration_seconds or 0) for r in recs}
    rec_play = {r.id: yemot_play_path(r.rec_url) for r in recs}
    for d in docs:
        loc = d['date'].replace(tzinfo=ZoneInfo('UTC')).astimezone(_IL) if d['date'] else None
        d['date_ddmmyyyy'] = loc.strftime('%d/%m/%Y') if loc else None
        d['hour'] = loc.hour if loc else None
        d['minute'] = loc.minute if loc else None
        d['minutes'] = max(1, round(rec_secs.get(d['id'], 0) / 60)) if d['type'] == 'recording' else None
        # הקלטה מהטלפון יושבת בימות - ה-IVR יכול להשמיע אותה ישירות. הקלטת מייל/דרייב - לא.
        d['play_path'] = rec_play.get(d['id']) if d['type'] == 'recording' else None
        d['date'] = d['date'].isoformat() if d['date'] else None
    return docs


def yemot_play_path(rec_url):
    """'...DownloadFile?...&path=ivr2:/recordings/051.wav' -> '/recordings/051' (לנגינה בימות), אחרת None."""
    import re
    from urllib.parse import unquote
    if not rec_url or 'call2all.co.il' not in rec_url:
        return None
    m = re.search(r'[?&]path=ivr2:([^&]+)', rec_url)
    if not m:
        return None
    path = unquote(m.group(1)).strip()
    if not path.startswith('/'):
        path = '/' + path
    path = re.sub(r'\.(wav|mp3)$', '', path, flags=re.I)
    return path if re.fullmatch(r'[/\w\- ]+', path) else None


def delivery_target(customer):
    """('email'|'fax', כתובת) לפי ההגדרה הנוכחית של הלקוח, או (None, None)."""
    email = (customer.email or '').strip()
    fax = (customer.fax or '').strip()
    if customer.delivery_method == 'fax' and fax:
        return 'fax', fax
    if email:
        return 'email', email
    if fax:
        return 'fax', fax
    return None, None


def _rate_ok(customer_id):
    now = time.time()
    with _resend_lock:
        stamps = [t for t in _resend_log.get(customer_id, []) if now - t < 86400]
        if len(stamps) >= MAX_RESENDS_PER_DAY:
            _resend_log[customer_id] = stamps
            return False
        stamps.append(now)
        _resend_log[customer_id] = stamps
        return True


def _load_doc(customer, doc_type, doc_id):
    from models import Recording, OcrResult, ManuscriptPage
    model = {'recording': Recording, 'ocr': OcrResult, 'manuscript': ManuscriptPage}.get(doc_type)
    if not model:
        return None
    doc = model.query.get(doc_id)
    if not doc or doc.customer_id != customer.id:
        return None
    return doc


def _send_fax_pdf(to, pdf_bytes, hint):
    from services.transcribe import send_pdf_fax
    if not pdf_bytes:
        return False, 'בניית ה-PDF נכשלה'
    res = send_pdf_fax(to, pdf_bytes, filename_hint=hint)
    return bool(res.get('ok')), res.get('error') or ''


def _do_send(customer, doc_type, doc, via, to):
    """שולח בפועל. מחזיר (ok, error)."""
    from services import transcribe as T
    name = customer.name or customer.phone or ''
    if doc_type == 'recording':
        secs = doc.duration_seconds or 0
        if via == 'email':
            from routes.recording_proxy import recording_download_url
            T._send_email(to, doc.transcript, customer, recording_download_url(doc.id), secs,
                          source_filename=doc.source_filename, is_premium=(doc.transcription_tier == 'premium'))
            return True, ''
        return _send_fax_pdf(to, T._build_pdf_for_fax(name, f'{secs // 60}:{secs % 60:02d}', doc.transcript),
                             f'transcript_resend_{doc.id}.pdf')
    if doc_type == 'ocr':
        if via == 'email':
            from routes.email_inbound import _send_ocr_result_email
            _send_ocr_result_email(to, doc.original_filename or 'כתב יד', doc.ocr_text, doc.char_count or 0, doc.cost or 0)
            return True, ''
        docx = T._build_word_doc(name='', duration_str=f'{doc.char_count or 0} תווים', transcript_fixed=doc.ocr_text,
                                 title=f'זיהוי כתב יד - {doc.original_filename or ""}')
        return _send_fax_pdf(to, T.convert_docx_bytes_to_pdf_bytes(docx), f'ocr_resend_{doc.id}.pdf')
    if doc_type == 'manuscript':
        from routes.dictate import _send_manuscript_email, _build_manuscript_docx
        if via == 'email':
            _send_manuscript_email(to, customer.name or '', customer.phone or '', doc.id, doc.original_filename or '', doc.content)
            return True, ''
        docx = _build_manuscript_docx(customer.name or '', doc.original_filename or '', doc.content)
        return _send_fax_pdf(to, T.convert_docx_bytes_to_pdf_bytes(docx), f'manuscript_resend_{doc.id}.pdf')
    return False, 'סוג מסמך לא מוכר'


def resend(customer, doc_type, doc_id, confirm=False):
    """מחזיר dict לתשובת ה-API. status: sent / needs_confirm / no_balance / no_target /
    not_found / limit / failed."""
    from app import db
    from models import Transaction
    doc = _load_doc(customer, doc_type, doc_id)
    if not doc:
        return {'status': 'not_found'}
    via, to = delivery_target(customer)
    if not via:
        return {'status': 'no_target'}
    price = fax_resend_price() if via == 'fax' else 0.0
    balance = round(float(customer.balance or 0), 2)
    if via == 'fax' and price > 0:
        if not confirm:
            return {'status': 'needs_confirm', 'via': 'fax', 'cost': price, 'balance': balance}
        if balance < price:
            return {'status': 'no_balance', 'via': 'fax', 'cost': price, 'balance': balance}
    if not _rate_ok(customer.id):
        return {'status': 'limit'}
    try:
        ok, err = _do_send(customer, doc_type, doc, via, to)
    except Exception as e:
        ok, err = False, f'{type(e).__name__}: {e}'
    if not ok:
        log.error(f"document resend failed: customer={customer.id} {doc_type}#{doc_id} via={via}: {err}")
        return {'status': 'failed', 'via': via}
    if price > 0:
        customer.balance = round(float(customer.balance or 0) - price, 2)
        db.session.add(Transaction(
            customer_id=customer.id, amount=-price, type='debit',
            recording_id=doc.id if doc_type == 'recording' else None,
            description=f'שליחה חוזרת בפקס - {TYPE_NAMES[doc_type]} #{doc.id}'))
        db.session.commit()
    log.info(f"document resend: customer={customer.id} {doc_type}#{doc_id} via={via} to={to} cost={price}")
    return {'status': 'sent', 'via': via, 'cost': price, 'balance': round(float(customer.balance or 0), 2)}
