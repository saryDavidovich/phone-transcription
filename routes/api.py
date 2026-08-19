from flask import current_app, Blueprint, request, jsonify
from app import db
from models import Customer, Recording, Settings, CallLog
from services.transcribe import transcribe_async
from datetime import datetime
import uuid

api_bp = Blueprint('api', __name__)

@api_bp.route('/call/start', methods=['POST'])
def call_start():
    """נקרא מה-IVR ברגע שנכנסת שיחה חדשה. אידמפוטנטי - קריאה חוזרת עם אותו
    call_id (למשל אם ה-IVR קורא לזה כמה פעמים באותה שיחה) לא יוצרת רשומה כפולה."""
    data = request.get_json(force=True, silent=True) or {}
    call_id = (data.get('call_id') or '').strip()
    phone = (data.get('phone') or '').strip()
    if not call_id:
        return jsonify({'error': 'call_id required'}), 400
    existing = CallLog.query.filter_by(call_id=call_id).first()
    if existing:
        return jsonify({'status': 'exists'})
    log = CallLog(call_id=call_id, phone=phone, started_at=datetime.utcnow())
    db.session.add(log)
    db.session.commit()
    return jsonify({'status': 'ok'})

@api_bp.route('/call/end', methods=['POST'])
def call_end():
    """נקרא מה-IVR כשהשיחה מסתיימת (ניתוק ע"י המתקשר, יציאה, וכו')."""
    data = request.get_json(force=True, silent=True) or {}
    call_id = (data.get('call_id') or '').strip()
    if not call_id:
        return jsonify({'error': 'call_id required'}), 400
    log = CallLog.query.filter_by(call_id=call_id).first()
    if not log:
        log = CallLog(call_id=call_id, phone=data.get('phone', ''), started_at=datetime.utcnow())
        db.session.add(log)
    if not log.ended_at:
        log.ended_at = datetime.utcnow()
        log.duration_seconds = max(0, int((log.ended_at - log.started_at).total_seconds()))
    db.session.commit()
    return jsonify({'status': 'ok'})


@api_bp.route('/customer/<phone>', methods=['GET'])
def get_customer(phone):
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()
    return jsonify({
        'phone': customer.phone,
        'balance': round(max(customer.balance, 0), 2),
        'email': customer.email or '',
        'fax': customer.fax or '',
        'delivery_method': customer.delivery_method or 'email',
        'is_blocked': customer.is_blocked,
        'default_settings': customer.default_settings or {},
        'name': customer.name or '',
    })

@api_bp.route('/customer/by-student/<student_number>', methods=['GET'])
def get_customer_by_student(student_number):
    """נקרא משלוחה 7 ב-IVR (ivr.js, כניסה עם מספר תלמיד) - מיועד בעיקר
    לתלמידים שאין להם טלפון אישי בכלל (לא רק כאלה שמתקשרים מטלפון שאינו
    שלהם). מחזיר מזהה "טלפון" קבוע שהתלמיד רשום תחתיו, כדי שהמשך השיחה
    ימשיך לזהות אותו נכון (כולל חיוב, הקלטות, יתרה) בדיוק כמו כל לקוח -
    כל שאר המערכת מזהה הכל לפי phone, וזו הדרך הכי פשוטה לעשות שימוש
    חוזר בכל הצנרת הקיימת בלי לשנות אותה בכל מקום.

    אם לתלמיד אין טלפון רשום כלל (המקרה השכיח) - יוצרים לו כאן, בפעם
    הראשונה בלבד, מזהה קבוע וייחודי מתוך מספר התלמיד עצמו (למשל
    "stu482915"), ושומרים אותו בשדה phone. זה לא מספר טלפון אמיתי -
    שימו לב שהוא עלול להופיע ככה בעמודת "טלפון" בייצוא לאקסל / כרטיס
    התלמיד בממשק ניהול המוסד. בפעמים הבאות שהתלמיד יתקשר בשלוחה 7 הוא
    כבר ימצא את אותו מזהה קבוע.

    אוכף כאן גם את שעות השימוש המותרות שהמוסד הגדיר (אם הוגדרו) -
    מגבלת הש"ח לתלמיד (max_usage_per_student) עדיין לא נאכפת בשום מקום
    במערכת כרגע ולכן לא נבדקת כאן."""
    customer = Customer.query.filter_by(student_number=student_number).first()
    if not customer:
        return jsonify({'error': 'not_found'}), 404

    if not customer.phone:
        customer.phone = f"stu{customer.student_number}"
        db.session.commit()

    institution = customer.institution
    if institution and institution.allowed_hours_start and institution.allowed_hours_end:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo('Asia/Jerusalem')).strftime('%H:%M')
        start, end = institution.allowed_hours_start, institution.allowed_hours_end
        in_range = (start <= now_local <= end) if start <= end else (now_local >= start or now_local <= end)
        if not in_range:
            return jsonify({
                'error': 'outside_allowed_hours',
                'allowed_hours_start': start,
                'allowed_hours_end': end,
            }), 403

    return jsonify({'phone': customer.phone})


@api_bp.route('/customer/pending-recordings', methods=['GET'])
def get_pending_recordings():
    """
    נקרא מה-IVR בכניסה לשיחה - בודק אם יש ללקוח הקלטות ממתינות לתשלום.
    מחזיר את ההקלטה הראשונה שממתינה עם פרטי העלות.
    """
    from models import Recording
    from datetime import datetime
    from routes.admin import get_setting
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
        current_app.logger.warning(f"pending-recordings query error: {e}")
        return jsonify({'has_pending': False})

    if not pending:
        return jsonify({'has_pending': False})

    price_per_20min = float(get_setting('price_per_20min_basic', '0.90'))
    units = math.ceil((pending.duration_seconds or 0) / 1200) or 1
    cost = round(units * price_per_20min, 2)
    minutes = (pending.duration_seconds or 0) // 60

    return jsonify({
        'has_pending': True,
        'minutes': minutes,
        'cost': cost,
        'balance': round(max(customer.balance, 0), 2),
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
    from models import Recording
    import requests as req
    import os
    data = request.json
    if not data:
        return jsonify({'ok': False}), 400

    current_app.logger.info(f"AlefBot webhook received: {data}")

    # אלף בוט שולח event_type ו-job_id
    # payload אפשרי: {"event_type": "transcription.completed", "job_id": "...", "status": "completed"}
    # אלף בוט שולח לפעמים 'event_type' ולפעמים 'event'
    event_type = data.get('event_type', '') or data.get('event', '')
    job_id = data.get('job_id', '') or data.get('id', '')
    status = data.get('status', '')

    is_completed = (event_type == 'transcription.completed') or (status == 'completed')

    if is_completed and job_id:
        # מצא את ה-call_id לפי job_id בבסיס הנתונים
        rec = Recording.query.filter_by(alefbot_job_id=job_id).first()
        if not rec:
            current_app.logger.warning(f"AlefBot webhook: no recording found for job_id={job_id}")
            return jsonify({'ok': True})

        call_id = rec.call_id
        api_key = os.environ.get('ALEFBOT_API_KEY')
        try:
            # קבל את הטקסט מ-artifact — plain_text
            r = req.get(
                f'https://alef-bot.top/api/v1/transcriptions/{job_id}/artifact?format=txt',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=60
            )
            r.raise_for_status()
            transcript = r.text.strip()
            current_app.logger.info(f"AlefBot artifact fetched: {len(transcript)} chars for job {job_id}")
            finalize_alefbot_recording(call_id, transcript)
        except Exception as e:
            current_app.logger.error(f"AlefBot webhook error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True})

@api_bp.route('/manager-message', methods=['POST'])
def receive_manager_message():
    from models import ManagerMessage
    from services.transcribe import transcribe_manager_message_async
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
    if msg.rec_url:
        transcribe_manager_message_async(msg.id, msg.rec_url)
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
            config=gtypes.GenerateContentConfig(
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0)
            ),
        )
        local_part = response.text.strip().lower()
        local_part = ''.join(c for c in local_part if c.isalnum() or c in '._-')
        try:
            current_app.logger.info(
                f"extract-email-local usage: thoughts={response.usage_metadata.thoughts_token_count or 0}, "
                f"total={response.usage_metadata.total_token_count}"
            )
        except Exception:
            pass
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
            config=gtypes.GenerateContentConfig(
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0)
            ),
        )
        domain = response.text.strip().lower()
        domain = ''.join(c for c in domain if c.isalnum() or c in '.-')
        try:
            current_app.logger.info(
                f"extract-email-domain usage: thoughts={response.usage_metadata.thoughts_token_count or 0}, "
                f"total={response.usage_metadata.total_token_count}"
            )
        except Exception:
            pass
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
    from routes.email_inbound import process_pending_ocr
    import threading

    data = request.get_json(silent=True) or {}
    phone = data.get('phone', '')
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return jsonify({'status': 'not_found'}), 404

    def _run_both(customer_id):
        process_pending_recordings(customer_id)
        process_pending_ocr(customer_id)

    t = threading.Thread(target=_run_both, args=(customer.id,), daemon=True)
    t.start()
    return jsonify({'status': 'started'})
