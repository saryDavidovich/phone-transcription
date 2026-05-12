"""routes/transcribe.py - endpoint ידני לבדיקות"""
from flask import Blueprint, request, jsonify
from services.transcribe_service import transcribe_async
transcribe_bp = Blueprint('transcribe', __name__)

@transcribe_bp.route('/manual', methods=['POST'])
def manual():
    data    = request.json or {}
    call_id = data.get('call_id', 'manual-test')
    rec_url = data.get('url', '')
    if not rec_url:
        return jsonify({'error': 'url נדרש'}), 400
    transcribe_async(call_id, rec_url, 'manual')
    return jsonify({'status': 'processing', 'call_id': call_id})
