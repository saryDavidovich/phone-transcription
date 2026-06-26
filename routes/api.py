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
        'is_blocked': customer.is_blocked,
        'default_settings': customer.default_settings or {},
        'name': customer.name or '',
    })

@api_bp.route('/customer/pending-recordings', methods=['GET'])
def get_pending_recordings():
    """
    נקרא מה-IVR בכניסה לשיחה - בודק אם יש ללקוח הקלטות ממתינות לתשלום.
    מחזיר את ההקלטה הראשונה שממתינה עם פרטי העלות.
    """
    from models import Recording
    from datetime import datetime
    import math

    phone = request.args.get('phone', '')
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'has_pending': False})

    try:
        pending = Recording.query.filter_by(
            customer_id=customer.id,
            status='pending_payment'
        ).filter(
            Recording.expires_at > datetime.utcnow()
        ).first()
    except Exception as e:
        log.warning(f"pending-recordings query error: {e}")
        return jsonify({'has_pending': False})

    if not pending:
        return jsonify({'has_pending': False})

    price_per_20min = float(_get_setting('price_per_20min_basic', '0.90'))
    units = math.ceil((pending.duration_seconds or 0) / 1200) or 1
    cost = round(units * price_per_20min, 2)
    minutes = (pending.duration_seconds or 0) // 60

    return jsonify({
        'has_pending': True,
        'minutes': minutes,
        'cost': cost,
        'balance': customer.balance,
        'enough_balance': customer.balance >= cost,
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
    if 'default_settings' in data:
        customer.default_settings = data['default_settings']
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
    transcription_tier = data.get('transcription_tier', 'basic')
    language = data.get('language', 'he')
    output_language = data.get('output_language', 'he')

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'error': 'customer not found'}), 404

    if not delivered_to:
        delivered_to = customer.email or customer.fax or ''
    if not delivery_method:
        delivery_method = customer.delivery_method or 'email'

    existing = Recording.query.filter_by(call_id=call_id).first()
    if existing:
        return jsonify({'ok': True, 'call_id': call_id})

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

    transcribe_async(call_id, rec_url, customer.id, delivery_method, delivered_to, duration, transcription_tier, language, output_language)
    return jsonify({'ok': True, 'call_id': call_id})

@api_bp.route('/alefbot-webhook', methods=['POST'])
def alefbot_webhook():
    from services.transcribe import finalize_alefbot_recording
    import requests as req
    import os
    data = request.json
    if not data:
        return jsonify({'ok': False}), 400

    status = data.get('status', '')
    call_id = data.get('client_reference', '')
    job_id = data.get('job_id', '')

    if status == 'completed' and call_id and job_id:
        api_key = os.environ.get('ALEFBOT_API_KEY')
        try:
            r = req.get(
                f'https://alef-bot.top/api/v1/transcriptions/{job_id}/artifact?format=txt',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=60
            )
            r.raise_for_status()
            transcript = r.text.strip()
            finalize_alefbot_recording(call_id, transcript)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True})

@api_bp.route('/manager-message', methods=['POST'])
def receive_manager_message():
    from models import ManagerMessage
    data = request.json
    call_id = data.get('call_id', '')
    msg = ManagerMessage.query.filter_by(call_id=call_id).first()
    if not msg:
        msg = ManagerMessage(call_id=call_id, status='new')
        db.session.add(msg)
    msg.phone = data.get('phone', '')
    msg.name = data.get('name', '')
    msg.email = data.get('email', '')
    msg.fax = data.get('fax', '')
    msg.delivery_method = data.get('delivery_method', '')
    msg.rec_url = data.get('rec_url', '')
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id})

@api_bp.route('/extract-email-local', methods=['POST'])
def extract_email_local():
    import os, requests as req
    from google import genai
    from google.genai import types as gtypes
    data = request.json
    rec_url = data.get('rec_url')

    if not rec_url:
        return jsonify({'local_part': ''}), 400

    try:
        r = req.get(rec_url, timeout=30)
        r.raise_for_status()

        client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[
                'המשתמש הקליט את שם המייל שלו (החלק לפני ה-@). החזר רק את שם המייל באנגלית קטנה, ללא רווחים, ללא @ ללא סיומת דומיין. לדוגמה אם אמר "יוסי כהן" החזר "yossycohen". החזר רק את הטקסט עצמו ללא שום הסבר.',
                gtypes.Part.from_bytes(data=r.content, mime_type='audio/wav'),
            ],
        )
        local_part = response.text.strip().lower()
        local_part = ''.join(c for c in local_part if c.isalnum() or c in '._-')
        return jsonify({'local_part': local_part})

    except Exception as e:
        return jsonify({'local_part': '', 'error': str(e)}), 500

@api_bp.route('/extract-email-domain', methods=['POST'])
def extract_email_domain():
    import os, requests as req
    from google import genai
    from google.genai import types as gtypes
    data = request.json
    rec_url = data.get('rec_url')

    if not rec_url:
        return jsonify({'local_part': ''}), 400

    try:
        r = req.get(rec_url, timeout=30)
        r.raise_for_status()

        client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[
                '''The user recorded their email domain letter by letter in English.
For example they said "G M A I L dot C O M" or "G M A I L נקודה C O M".
Return the full domain in lowercase including the dot extension.
Examples: "gmail.com", "yahoo.com", "walla.co.il"
Return ONLY the domain, nothing else.''',
                gtypes.Part.from_bytes(data=r.content, mime_type='audio/wav'),
            ],
        )
        domain = response.text.strip().lower()
        domain = ''.join(c for c in domain if c.isalnum() or c in '.-')
        return jsonify({'local_part': domain})

    except Exception as e:
        return jsonify({'local_part': '', 'error': str(e)}), 500

@api_bp.route('/get-msg/<int:msg_id>', methods=['GET'])
def get_manager_message_callid(msg_id):
    from models import ManagerMessage
    msg = ManagerMessage.query.get_or_404(msg_id)
    return jsonify({'call_id': msg.call_id, 'phone': msg.phone})
@api_bp.route('/fax-delivery-webhook', methods=['POST'])
def fax_delivery_webhook():
    """מקבל דוח מסירה מימות המשיח (SendFax deliveryUrl) ומעדכן את סטטוס הפקס."""
    from services.transcribe import handle_fax_delivery_webhook
    data = request.form.to_dict() if request.form else (request.json or {})
    handle_fax_delivery_webhook(data)
    return jsonify({'ok': True})

@api_bp.route('/manager-message-reserve', methods=['POST'])
def reserve_manager_message():
    from models import ManagerMessage
    data = request.json
    msg = ManagerMessage(
        phone   = data.get('phone', ''),
        call_id = data.get('call_id', ''),
        status  = 'new'
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({'id': msg.id})

@api_bp.route('/settings', methods=['GET'])
def get_public_settings():
    """מחזיר הגדרות מחיר ציבוריות ל-IVR"""
    from routes.admin import get_setting
    settings = {
        'price_per_20min_basic': get_setting('price_per_20min_basic', '0.90'),
        'price_per_20min_premium': get_setting('price_per_20min_premium', '1.90'),
        'price_per_20min_video': get_setting('price_per_20min_video', '1.50'),
        'price_per_1000_chars_ocr': get_setting('price_per_1000_chars_ocr', '0.10'),
        'min_balance': get_setting('min_balance', '5'),
    }
    for i in range(1, 4):
        settings[f'bonus_threshold_{i}'] = get_setting(f'bonus_threshold_{i}', '')
        settings[f'bonus_amount_{i}'] = get_setting(f'bonus_amount_{i}', '')
    return jsonify(settings)


@api_bp.route('/process-pending', methods=['POST'])
def process_pending():
    """נקרא מה-IVR כשמזוהה שיש pending ויש יתרה — מפעיל תמלול"""
    from services.transcribe import process_pending_recordings
    import threading

    data = request.get_json(silent=True) or {}
    phone = data.get('phone', '')
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'status': 'not_found'}), 404

    t = threading.Thread(target=process_pending_recordings, args=(customer.id,), daemon=True)
    t.start()
    return jsonify({'status': 'started'})
