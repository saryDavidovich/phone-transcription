"""טעינת יתרה של לקוח פרטי באייפרם נדרים פלוס (האזור האישי).

נדרים שולחים שני עדכונים על אותה עסקה, בסדר לא מובטח: ה-CallBack של העסקה וה-Webhook של המוסד
(routes/payment.py). שניהם מגיעים לכאן, ונעילת שורה על רשומת החיוב מבטיחה זיכוי אחד בלבד."""
import logging
import re
import threading

log = logging.getLogger(__name__)


MARKER_RE = re.compile(r'TP-(\d{1,10})\b')


def marker(charge_id):
    """נכתב בהערות העסקה בנדרים - כך ה-Webhook של המוסד מזהה את רשומת החיוב (אין בו Param2)."""
    return f'TP-{charge_id}'


def credit_charge(charge_id, data, source):
    """מזכה את הלקוח לפי רשומת החיוב (אם עוד לא זוכה). data = גוף העדכון מנדרים.
    source: 'callback' (CallBack של העסקה: TransactionId = מזהה ההקמה, ID = מזהה ההסטוריה),
            'webhook' (Webhook המוסד: TransactionId = מזהה ההסטוריה), 'auto' (תשובת חיוב בודד: TransactionId = הסטוריה)."""
    from app import db
    from models import CustomerChargeLog, Customer, Transaction
    charge = CustomerChargeLog.query.filter_by(id=charge_id).populate_existing().with_for_update().first()
    if not charge:
        return 'not_found'
    receipt = str(data.get('ReceiptData') or '').strip()
    if charge.status == 'success':
        if receipt.startswith('http') and not charge.receipt_url:
            charge.receipt_url = receipt[:500]
            db.session.commit()
        else:
            db.session.rollback()
        return 'already'
    if str(data.get('Status') or '').upper() != 'OK':
        if charge.status == 'pending' and source in ('callback', 'auto'):
            charge.status = 'failed'
        db.session.commit()
        return 'not_ok'
    if source == 'callback':
        created = str(data.get('TransactionId') or '').strip()
        hist = str(data.get('ID') or '').strip()
        if charge.kind != 'auto' and charge.provider_ref and created and str(charge.provider_ref) != created:
            db.session.rollback()
            log.warning(f'customer charge {charge_id}: TransactionId mismatch ({created} != {charge.provider_ref})')
            return 'mismatch'
    else:
        hist = str(data.get('TransactionId') or data.get('ID') or '').strip()
    try:
        amt = data.get('Amount')
        if amt not in (None, '') and abs(float(str(amt).replace(',', '')) - float(charge.amount)) > 0.01:
            db.session.rollback()
            log.warning(f'customer charge {charge_id}: Amount mismatch ({amt} != {charge.amount})')
            return 'mismatch'
    except ValueError:
        pass
    c = Customer.query.filter_by(id=charge.customer_id).populate_existing().with_for_update().first()
    if not c:
        db.session.rollback()
        return 'no_customer'
    from routes.payment import _calculate_bonus_public
    bonus = _calculate_bonus_public(charge.amount)
    total = round(charge.amount + bonus, 2)
    c.balance = round((c.balance or 0) + total, 2)
    charge.status = 'success'
    if hist:
        charge.history_id = hist[:64]
    if receipt.startswith('http'):
        charge.receipt_url = receipt[:500]
    what = 'טעינה אוטומטית' if charge.kind == 'auto' else 'טעינת יתרה באשראי (האזור האישי)'
    desc = f'{what} ₪{charge.amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total:.2f}'
    if data.get('Confirmation'):
        desc += f' | אישור: {data.get("Confirmation")}'
    # [נדרים:<הסטוריה>] - אותו תיוג שה-Webhook בודק למניעת זיכוי כפול
    desc += f' [נדרים:{hist or charge.provider_ref}]'
    if charge.provider_ref and hist and str(charge.provider_ref) != hist:
        desc += f' [הקמה:{charge.provider_ref}]'
    db.session.add(Transaction(customer_id=c.id, amount=total, type='charge', description=desc))
    db.session.commit()
    log.info(f'customer {c.id} topped up +{total} (charge {charge_id}, via {source})')
    _release_pending(c.id)
    return 'credited'


# --------------------------------------------------------------------------- קבלות
TAMAL_URL = 'https://matara.pro/nedarimplus/Reports/Tamal3.aspx'
_TAG_RE = re.compile(r'\[נדרים:([^\]]+)\]')


def _receipt_api(mosad, key, tx_id):
    """(קישור, הודעת נדרים). ShowInvoice, ואם לא - הסטוריית העסקאות (InvoiceData2)."""
    import requests
    msg = ''
    try:
        r = requests.get(TAMAL_URL, params={'Action': 'ShowInvoice', 'MosadId': mosad, 'ApiPassword': key,
                                            'TransactionId': tx_id}, timeout=20)
        try:
            d = r.json()
        except ValueError:
            d = {'Message': (r.text or '')[:200]}
        m = str(d.get('Message') or '').strip()
        if str(d.get('Result') or d.get('Status') or '').upper() == 'OK' and m.startswith('http'):
            return m, ''
        msg = m[:200]
    except Exception as e:
        msg = f'ShowInvoice: {e}'[:200]
    if tx_id.isdigit():
        try:
            r = requests.get(MANAGE_URL, params={'Action': 'GetHistoryJson', 'MosadId': mosad, 'ApiPassword': key,
                                                 'LastId': str(int(tx_id) - 1), 'MaxId': '1'}, timeout=20)
            rows = r.json()
            rows = rows if isinstance(rows, list) else (rows.get('data') or rows.get('Data') or [])
            for row in rows:
                if str(row.get('TransactionId')) != tx_id:
                    continue
                for k in ('ReceiptData', 'InvoiceLink', 'KabalaLink'):
                    v = str(row.get(k) or '')
                    if v.startswith('http'):
                        return v, ''
                uuid = str(row.get('InvoiceData2') or '').strip()
                if uuid:
                    return f'https://docs.matara.pro/r/{uuid}', ''
                msg = msg or 'אין קבלה לעסקה בנדרים'
        except Exception as e:
            log.info(f'receipt history lookup {tx_id}: {e}')
    return None, msg


MANAGE_URL = 'https://matara.pro/nedarimplus/Reports/Manage3.aspx'


def receipt_url(tx):
    """קישור לקבלה של טעינה (Transaction של הלקוח). (url, None) או (None, הסבר)."""
    import requests
    from app import db
    from models import CustomerChargeLog, Settings
    from services.nedarim_messages import _credentials
    m = _TAG_RE.search(tx.description or '')
    if not m or (tx.amount or 0) <= 0:
        return None, 'לטעינה הזו אין קבלה במערכת (למשל זיכוי ידני או בונוס)'
    ref = m.group(1).strip()
    charge = (CustomerChargeLog.query.filter_by(customer_id=tx.customer_id, history_id=ref).first()
              or CustomerChargeLog.query.filter_by(customer_id=tx.customer_id, provider_ref=ref).first())
    if charge and charge.receipt_url:
        return charge.receipt_url, None
    mosad, key = _credentials()
    if not mosad or not key:
        return None, 'הפקת קבלות עוד לא הוגדרה. כתבו לנו ונשלח את הקבלה'
    # כל המזהים האפשריים של העסקה: הסטוריה (מה-CallBack / Webhook), מזהה ההקמה, והתיוג בתנועה
    ids = []
    for v in ((charge.history_id if charge else None), ref, (charge.provider_ref if charge else None)):
        v = str(v or '').strip()
        if v and v not in ids and not v.startswith('keva:'):
            ids.append(v)
    hm = re.search(r'\[הקמה:([^\]]+)\]', tx.description or '')
    if hm and hm.group(1) not in ids:
        ids.append(hm.group(1))
    url, last = None, ''
    for tid in ids:
        url, msg = _receipt_api(mosad, key, tid)
        if url:
            break
        last = msg or last
    if not url:
        row = Settings.query.filter_by(key='nedarim_receipt_type').first()
        kind = row.value if row and row.value in ('400', '320', '405') else ''
        if kind and ids:
            try:
                r = requests.get(TAMAL_URL, params={'Action': 'TamalCreate', 'MosadId': mosad, 'ApiPassword': key,
                                                    'TransactionId': ids[0], 'TamalType': kind}, timeout=30)
                log.info(f'receipt create for {ids[0]}: {(r.text or "")[:120]}')
                url, last = _receipt_api(mosad, key, ids[0])
            except Exception as e:
                last = str(e)[:200]
    if not url:
        log.warning(f'receipt for tx {tx.id} (ids {ids}): {last}')
        return None, ('לא הצלחנו למשוך את הקבלה מנדרים' + (f' (נדרים השיבו: {last})' if last else '') +
                      '. נסו שוב בעוד דקה, או כתבו לנו ונשלח אותה')
    if charge:
        charge.receipt_url = url[:500]
        db.session.commit()
    return url, None


def _release_pending(customer_id):
    def run():
        import time
        from app import app as flask_app
        time.sleep(3)
        with flask_app.app_context():
            try:
                from services.transcribe import process_pending_recordings
                process_pending_recordings(customer_id)
            except Exception:
                log.exception('customer topup: pending recordings failed')
            try:
                from routes.email_inbound import process_pending_ocr
                process_pending_ocr(customer_id)
            except Exception:
                log.exception('customer topup: pending ocr failed')
    threading.Thread(target=run, daemon=True).start()
