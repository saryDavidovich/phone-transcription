"""טעינת יתרה של לקוח פרטי באייפרם נדרים פלוס (האזור האישי).

נדרים שולחים שני עדכונים על אותה עסקה, בסדר לא מובטח: ה-CallBack של העסקה וה-Webhook של המוסד
(routes/payment.py). שניהם מגיעים לכאן, ונעילת שורה על רשומת החיוב מבטיחה זיכוי אחד בלבד."""
import logging
import threading

log = logging.getLogger(__name__)


def credit_charge(charge_id, data, source):
    """מזכה את הלקוח לפי רשומת החיוב (אם עוד לא זוכה). data = גוף העדכון מנדרים."""
    from app import db
    from models import CustomerChargeLog, Customer, Transaction
    charge = CustomerChargeLog.query.filter_by(id=charge_id).populate_existing().with_for_update().first()
    if not charge:
        return 'not_found'
    if charge.status == 'success':
        db.session.rollback()
        return 'already'
    if str(data.get('Status') or '').upper() != 'OK':
        if charge.status == 'pending' and source == 'callback':
            charge.status = 'failed'
        db.session.commit()
        return 'not_ok'
    tx_id = str(data.get('TransactionId') or data.get('ID') or '').strip()
    if charge.provider_ref and tx_id and str(charge.provider_ref) != tx_id:
        db.session.rollback()
        log.warning(f'customer charge {charge_id}: TransactionId mismatch ({tx_id} != {charge.provider_ref})')
        return 'mismatch'
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
    if tx_id and not charge.provider_ref:
        charge.provider_ref = tx_id
    desc = f'טעינת יתרה באשראי (האזור האישי) ₪{charge.amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total:.2f}'
    if data.get('Confirmation'):
        desc += f' | אישור: {data.get("Confirmation")}'
    desc += f' [נדרים:{tx_id or charge.provider_ref}]'
    db.session.add(Transaction(customer_id=c.id, amount=total, type='charge', description=desc))
    db.session.commit()
    log.info(f'customer {c.id} topped up +{total} (charge {charge_id}, via {source})')
    _release_pending(c.id)
    return 'credited'


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
