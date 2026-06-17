import logging
from flask import Blueprint, request, jsonify

payment_bp = Blueprint('payment', __name__)
log = logging.getLogger(__name__)


@payment_bp.route('/nedarim/callback', methods=['GET', 'POST'])
def nedarim_callback():
    """
    Webhook שנדרים פלוס / ימות המשיח שולחים אחרי סליקה מוצלחת.
    ימות שולחים את הפרמטרים כ-GET או POST.
    """
    from app import db
    from models import Customer, Transaction
    from routes.admin import get_setting

    # פרמטרים מימות/נדרים
    # Status/ResponseCode: '000' או 'OK' = הצלחה
    status = (
        request.args.get('Status') or
        request.form.get('Status') or
        request.args.get('ResponseCode') or
        request.form.get('ResponseCode') or ''
    ).upper()

    # סכום שנסלק
    amount_str = (
        request.args.get('Amount') or
        request.form.get('Amount') or
        request.args.get('BillingSum') or
        request.form.get('BillingSum') or '0'
    )

    # מספר טלפון - ימות שולחים בשדה Description או phone
    phone = (
        request.args.get('Description') or
        request.form.get('Description') or
        request.args.get('phone') or
        request.form.get('phone') or ''
    ).strip()

    # מספר אישור עסקה
    approval = (
        request.args.get('DealSuccessfully') or
        request.form.get('DealSuccessfully') or
        request.args.get('Comments') or
        request.form.get('Comments') or ''
    )

    log.info(f"Nedarim callback: status={status}, amount={amount_str}, phone={phone}, approval={approval}")
    log.info(f"Full params: {dict(request.args)} | {dict(request.form)}")

    success = status in ('000', 'OK', '0', 'SUCCESS', 'APPROVED')

    try:
        amount = float(amount_str)
    except ValueError:
        amount = 0

    if not success or amount <= 0 or not phone:
        log.warning(f"Nedarim callback rejected: success={success}, amount={amount}, phone={phone}")
        return jsonify({'status': 'error', 'reason': 'invalid params'}), 400

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        log.warning(f"Nedarim callback: customer not found for phone={phone}")
        return jsonify({'status': 'error', 'reason': 'customer not found'}), 404

    # חישוב בונוס מבצע
    bonus = _calculate_bonus(amount, get_setting)
    total_credit = amount + bonus

    customer.balance += total_credit

    desc = f'טעינת ארנק ₪{amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס מבצע ₪{bonus:.2f} = סה"כ ₪{total_credit:.2f}'
    if approval:
        desc += f' (אישור: {approval})'

    txn = Transaction(
        customer_id=customer.id,
        amount=total_credit,
        type='charge',
        description=desc,
    )
    db.session.add(txn)
    db.session.commit()

    log.info(f"Nedarim payment processed: customer={customer.id}, amount={amount}, bonus={bonus}, new_balance={customer.balance}")
    return jsonify({'status': 'ok', 'credited': total_credit, 'balance': customer.balance})


def _calculate_bonus(amount, get_setting):
    """
    מחשב בונוס מבצע לפי הגדרות ממשק הניהול.
    ניתן להגדיר מספר רמות:
    bonus_threshold_1=50   # סכום מינימום לבונוס
    bonus_amount_1=10      # סכום הבונוס
    bonus_threshold_2=100
    bonus_amount_2=25
    """
    bonus = 0.0
    for i in range(1, 6):  # עד 5 רמות בונוס
        threshold_str = get_setting(f'bonus_threshold_{i}', '')
        bonus_str = get_setting(f'bonus_amount_{i}', '')
        if not threshold_str or not bonus_str:
            break
        try:
            threshold = float(threshold_str)
            bonus_amount = float(bonus_str)
            if amount >= threshold:
                bonus = bonus_amount  # הבונוס הגבוה ביותר שמגיע ללקוח
        except ValueError:
            continue
    return bonus
