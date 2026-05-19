from flask import Blueprint, request, jsonify
from app import db
from models import Customer, Recording, Settings
from services.transcribe import transcribe_async
import uuid

api_bp = Blueprint('api', __name__)

@api_bp.route('/customer/<phone>', methods=['GET'])
def get_customer(phone):
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()
    return jsonify({
        'phone': customer.phone,
        'balance': customer.balance,
        'email': customer.email or '',
        'fax': customer.fax or '',
        'delivery_method': customer.delivery_method or 'email',
        'is_blocked': customer.is_blocked
    })

@api_bp.route('/customer/update', methods=['POST'])
def update_customer():
    data = request.json
    phone = data.get('phone')
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'error': 'not found'}), 404
    if 'email' in data:
        customer.email = data['email']
    if 'fax' in data:
        customer.fax = data['fax']
    if 'delivery_method' in data:
        customer.delivery_method = data['delivery_method']
    db.session.commit()
    return jsonify({'ok': True})

@api_bp.route('/transcribe', methods=['POST'])
def transcribe():
    data = request.json
    phone = data.get('phone')
    rec_url = data.get('rec_url')
    call_id = data.get('call_id', str(uuid.uuid4()))
    duration = data.get('duration', 0)
    delivery_method = data.get('delivery_method', 'email')
    delivered_to = data.get('delivered_to', '')

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'error': 'customer not found'}), 404

    if not delivered_to:
        delivered_to = customer.email or customer.fax or ''
    if not delivery_method:
        delivery_method = customer.delivery_method or 'email'

    rec = Recording(
        call_id=call_id,
        customer_id=customer.id,
        duration_seconds=duration,
        status='processing',
        delivery_method=delivery_method,
        delivered_to=delivered_to
    )
    db.session.add(rec)
    db.session.commit()

    transcribe_async(call_id, rec_url, customer.id, delivery_method, delivered_to, duration)

    return jsonify({'ok': True, 'call_id': call_id})
