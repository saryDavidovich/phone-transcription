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
    transcription_tier = data.get('transcription_tier', 'basic')

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

    transcribe_async(call_id, rec_url, customer.id, delivery_method, delivered_to, duration, transcription_tier)
    return jsonify({'ok': True, 'call_id': call_id})
@api_bp.route('/manager-message', methods=['POST'])
def receive_manager_message():
    from models import ManagerMessage
    data = request.json
    msg = ManagerMessage(
        phone           = data.get('phone', ''),
        name            = data.get('name', ''),
        email           = data.get('email', ''),
        fax             = data.get('fax', ''),
        delivery_method = data.get('delivery_method', ''),
        call_id         = data.get('call_id', ''),
        rec_url         = data.get('rec_url', ''),
        status          = 'new'
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id})
@api_bp.route('/extract-email-local', methods=['POST'])
def extract_email_local():
    import os, requests as req
    data = request.json
    rec_url = data.get('rec_url')

    if not rec_url:
        return jsonify({'local_part': ''}), 400

    try:
        # הורדת ההקלטה
        r = req.get(rec_url, timeout=30)
        r.raise_for_status()

        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(r.content)
            tmp_path = f.name

        # תמלול עם Whisper
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        with open(tmp_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language='he',
                response_format='text'
            )
        os.remove(tmp_path)

        transcript = result.strip()

        # חילוץ שם המייל עם Claude
        import anthropic
        claude = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = claude.messages.create(
            model='claude-sonnet-4-5',
            max_tokens=100,
            messages=[{
                'role': 'user',
                'content': f'''המשתמש הקליט את שם המייל שלו (החלק לפני ה-@).
התמלול הוא: "{transcript}"
החזר רק את שם המייל באנגלית קטנה, ללא רווחים, ללא @ ללא סיומת דומיין.
לדוגמה אם אמר "יוסי כהן" החזר "yossycohen".
אם אמר "david123" החזר "david123".
החזר רק את הטקסט עצמו ללא שום הסבר.'''
            }]
        )

        local_part = msg.content[0].text.strip().lower()
        local_part = ''.join(c for c in local_part if c.isalnum() or c in '._-')

        return jsonify({'local_part': local_part, 'transcript': transcript})

    except Exception as e:
        return jsonify({'local_part': '', 'error': str(e)}), 500
