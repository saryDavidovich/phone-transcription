import logging
from flask import Blueprint, request, jsonify

payment_bp = Blueprint('payment', __name__)
log = logging.getLogger(__name__)


@payment_bp.route('/nedarim/callback', methods=['GET', 'POST'])
def nedarim_callback():
    """
    נקרא משלוחה 200 אחרי סליקה מוצלחת.
    קורא את קובץ הלוג מימות ומעדכן יתרה.
    """
    from app import db
    from models import Customer, Transaction
    from routes.admin import get_setting
    import requests as req

    log.info(f"Nedarim callback: args={dict(request.args)}")
import logging
from flask import Blueprint, request, jsonify

payment_bp = Blueprint('payment', __name__)
log = logging.getLogger(__name__)


@payment_bp.route('/nedarim/callback', methods=['GET', 'POST'])
def nedarim_callback():
    """
    נקרא משלוחה 200 אחרי סליקה מוצלחת.
    קורא את קובץ הלוג מימות ומעדכן יתרה.
    """
    from app import db
    from models import Customer, Transaction
    from routes.admin import get_setting
    import requests as req

    log.info(f"Nedarim callback: args={dict(request.args)}")

    # אימות טוקן סודי
    expected_secret = get_setting('payment_callback_secret', '')
    if expected_secret:
        incoming_secret = (
            request.args.get('secret') or
            request.form.get('secret') or ''
        ).strip()
        if incoming_secret != expected_secret:
            log.warning(f"Nedarim callback: invalid secret, rejecting")
            return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # מספר טלפון - שלוחת API שולחת ApiPhone
    phone = (
        request.args.get('ApiPhone') or
        request.form.get('ApiPhone') or
        request.args.get('Phone') or
        request.form.get('Phone') or ''
    ).strip()

    if not phone:
        log.warning("Nedarim callback: no phone found")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # קריאת קובץ הלוג מימות
    yemot_token = get_setting('yemot_token', '')
    yemot_log_path = get_setting('yemot_log_path', 'ivr2:/199/LogCreditCardOK.ini')

    if not yemot_token:
        log.error("Nedarim callback: yemot_token not configured in settings")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    try:
        resp = req.get(
            'https://www.call2all.co.il/ym/api/GetTextFile',
            params={'token': yemot_token, 'what': yemot_log_path},
            timeout=10
        )
        resp.raise_for_status()
        contents = resp.json().get('contents', '')
        log.info(f"Yemot log file read: {len(contents)} chars")
    except Exception as e:
        log.error(f"Failed to read yemot log: {e}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # פענוח הקובץ - פורמט: Field#Value%Field#Value\nField#Value%...
    amount = 0
    approval = ''
    lines = [l.strip() for l in contents.strip().split('\n') if l.strip()]

    # סריקה מהשורה האחרונה - הסליקה האחרונה של הלקוח
    for line in reversed(lines):
        fields = {}
        for part in line.split('%'):
            if '#' in part:
                k, _, v = part.partition('#')
                fields[k.strip()] = v.strip()

        line_phone = fields.get('Phone', '')
        line_status = fields.get('Status', '')
        line_amount = fields.get('BillingSum', '0')
        line_approval = fields.get('DealSuccessfully', '')

        if line_phone == phone and line_status == 'OK':
            try:
                amount = float(line_amount)
                approval = line_approval
                log.info(f"Found transaction: phone={phone}, amount={amount}, approval={approval}")
            except ValueError:
                pass
            break

    if amount <= 0:
        log.warning(f"No valid amount found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        log.warning(f"Customer not found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # בדיקת כפילות לפי מספר אישור
    if approval:
        existing = Transaction.query.filter(
            Transaction.customer_id == customer.id,
            Transaction.description.contains(f'אישור: {approval}')
        ).first()
        if existing:
            log.warning(f"Transaction {approval} already processed, skipping")
            return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # חישוב בונוס
    bonus = _calculate_bonus(amount, get_setting)
    total_credit = amount + bonus

    customer.balance += total_credit

    desc = f'טעינת ארנק ₪{amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total_credit:.2f}'
    if approval:
        desc += f' | אישור: {approval}'

    txn = Transaction(
        customer_id=customer.id,
        amount=total_credit,
        type='charge',
        description=desc,
    )
    db.session.add(txn)
    db.session.commit()

    log.info(f"Payment OK: customer={customer.id}, +{total_credit}, balance={customer.balance}")
    return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}


def _calculate_bonus(amount, get_setting):
    bonus = 0.0
    for i in range(1, 6):
        threshold_str = get_setting(f'bonus_threshold_{i}', '')
        bonus_str = get_setting(f'bonus_amount_{i}', '')
        if not threshold_str or not bonus_str:
            break
        try:
            threshold = float(threshold_str)
            bonus_amount = float(bonus_str)
            if amount >= threshold:
                bonus = bonus_amount
        except ValueError:
            continue
    return bonus


@payment_bp.route('/pending', methods=['POST'])
def save_pending():
    from routes.admin import set_setting
    data = request.json or {}
    phone = data.get('phone', '').strip()
    amount = float(data.get('amount', 0))
    if not phone or amount <= 0:
        return jsonify({'status': 'error'}), 400
    set_setting(f'pending_payment_{phone}', str(amount))
    log.info(f"Pending payment saved: phone={phone}, amount={amount}")
    return jsonify({'status': 'ok'})

    # מספר טלפון - שלוחת API שולחת ApiPhone
    phone = (
        request.args.get('ApiPhone') or
        request.form.get('ApiPhone') or
        request.args.get('Phone') or
        request.form.get('Phone') or ''
    ).strip()

    if not phone:
        log.warning("Nedarim callback: no phone found")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # קריאת קובץ הלוג מימות
    yemot_token = get_setting('yemot_token', '')
    yemot_log_path = get_setting('yemot_log_path', 'ivr2:/199/LogCreditCardOK.ini')

    if not yemot_token:
        log.error("Nedarim callback: yemot_token not configured in settings")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    try:
        resp = req.get(
            'https://www.call2all.co.il/ym/api/GetTextFile',
            params={'token': yemot_token, 'what': yemot_log_path},
            timeout=10
        )
        resp.raise_for_status()
        contents = resp.json().get('contents', '')
        log.info(f"Yemot log file read: {len(contents)} chars")
    except Exception as e:
        log.error(f"Failed to read yemot log: {e}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # פענוח הקובץ - פורמט: Field#Value%Field#Value\nField#Value%...
    amount = 0
    approval = ''
    lines = [l.strip() for l in contents.strip().split('\n') if l.strip()]

    # סריקה מהשורה האחרונה - הסליקה האחרונה של הלקוח
    for line in reversed(lines):
        fields = {}
        for part in line.split('%'):
            if '#' in part:
                k, _, v = part.partition('#')
                fields[k.strip()] = v.strip()

        line_phone = fields.get('Phone', '')
        line_status = fields.get('Status', '')
        line_amount = fields.get('BillingSum', '0')
        line_approval = fields.get('DealSuccessfully', '')

        if line_phone == phone and line_status == 'OK':
            try:
                amount = float(line_amount)
                approval = line_approval
                log.info(f"Found transaction: phone={phone}, amount={amount}, approval={approval}")
            except ValueError:
                pass
            break

    if amount <= 0:
        log.warning(f"No valid amount found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        log.warning(f"Customer not found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # בדיקת כפילות לפי מספר אישור
    if approval:
        existing = Transaction.query.filter(
            Transaction.customer_id == customer.id,
            Transaction.description.contains(f'אישור: {approval}')
        ).first()
        if existing:
            log.warning(f"Transaction {approval} already processed, skipping")
            return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # חישוב בונוס
    bonus = _calculate_bonus(amount, get_setting)
    total_credit = amount + bonus

    customer.balance += total_credit

    desc = f'טעינת ארנק ₪{amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total_credit:.2f}'
    if approval:
        desc += f' | אישור: {approval}'

    txn = Transaction(
        customer_id=customer.id,
        amount=total_credit,
        type='charge',
        description=desc,
    )
    db.session.add(txn)
    db.session.commit()

    log.info(f"Payment OK: customer={customer.id}, +{total_credit}, balance={customer.balance}")
    return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}


def _calculate_bonus(amount, get_setting):
    bonus = 0.0
    for i in range(1, 6):
        threshold_str = get_setting(f'bonus_threshold_{i}', '')
        bonus_str = get_setting(f'bonus_amount_{i}', '')
        if not threshold_str or not bonus_str:
            break
        try:
            threshold = float(threshold_str)
            bonus_amount = float(bonus_str)
            if amount >= threshold:
                bonus = bonus_amount
        except ValueError:
            continue
    return bonus


@payment_bp.route('/pending', methods=['POST'])
def save_pending():
    from routes.admin import set_setting
    data = request.json or {}
    phone = data.get('phone', '').strip()
    amount = float(data.get('amount', 0))
    if not phone or amount <= 0:
        return jsonify({'status': 'error'}), 400
    set_setting(f'pending_payment_{phone}', str(amount))
    log.info(f"Pending payment saved: phone={phone}, amount={amount}")
    return jsonify({'status': 'ok'})
