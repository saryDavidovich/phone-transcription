"""routes/deliver.py - ניהול שליחה ידנית"""
from flask import Blueprint, request, jsonify
from services.database import get_call, set_delivery_preference
from services.deliver_service import deliver
deliver_bp = Blueprint('deliver', __name__)

@deliver_bp.route('/resend', methods=['POST'])
def resend():
    data       = request.json or {}
    call_id    = data.get('call_id')
    dest       = data.get('destination', 'email')
    address    = data.get('address', '')
    call_data  = get_call(call_id)
    if not call_data:
        return jsonify({'error': 'שיחה לא נמצאה'}), 404
    set_delivery_preference(call_id, dest, address)
    call_data['destination']    = dest
    call_data['target_address'] = address
    deliver(call_data)
    return jsonify({'status': 'sent'})
