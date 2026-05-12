from flask import Blueprint, request, jsonify
from app import db
from models import Customer, Transaction

payment_bp = Blueprint('payment', __name__)

@payment_bp.route('/cardcom/callback', methods=['GET', 'POST'])
def cardcom_callback():
    """Cardcom calls this after successful payment"""
    amount = float(request.args.get('Amount', request.form.get('Amount', 0)))
    phone = request.args.get('Description', request.form.get('Description', ''))
    success = request.args.get('ResponseCode', request.form.get('ResponseCode', '')) == '0'

    if success and amount > 0 and phone:
        customer = Customer.query.filter_by(phone=phone).first()
        if customer:
            customer.balance += amount
            txn = Transaction(
                customer_id=customer.id,
                amount=amount,
                type='charge',
                description=f'טעינת ארנק - קארדקום'
            )
            db.session.add(txn)
            db.session.commit()
            return jsonify({'status': 'ok'})

    return jsonify({'status': 'error'}), 400
