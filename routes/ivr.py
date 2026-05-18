from flask import Blueprint, request
from app import db
from models import Customer, Recording, Settings, CallSession
from services.transcribe import transcribe_async
import uuid, logging, os

ivr_bp = Blueprint('ivr', __name__)
log = logging.getLogger(__name__)

BASE_URL = 'https://web-production-90272.up.railway.app/ivr'

def get_param(key, default=''):
    return request.args.get(key) or request.form.get(key) or default

def get_setting(key, default=''):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def set_step(call_id, step):
    session = CallSession.query.filter_by(call_id=call_id).first()
    if session:
        session.step = step
    else:
        session = CallSession(call_id=call_id, step=step)
        db.session.add(session)
    db.session.commit()

def get_step(call_id):
    session = CallSession.query.filter_by(call_id=call_id).first()
    return session.step if session else ''

def r(text, next_step=None, input_len=1, timeout=10, record=None):
    text = text.replace('.', ' ').replace('-', ' ')
    if record:
        response = f'read=t-{text}=rec,,record,,,{record},no,yes,{BASE_URL}/incoming'
    elif next_step:
        response = f'read=t-{text}=Digits,,1,{input_len},{timeout},Number,yes,{BASE_URL}/incoming'
    else:
        response = f'id_list_message=t-{text}'
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

def set_step(call_id, step):
    call_sessions[call_id] = step

def get_step(call_id):
    return call_sessions.get(call_id, '')

@ivr_bp.route('/incoming', methods=['GET', 'POST'])
def incoming():
    log.info(f"GET params: {dict(request.args)}")
    log.info(f"POST params: {dict(request.form)}")
    phone = get_param('ApiPhone')
    call_id = get_param('ApiCallId', str(uuid.uuid4()))
    log.info(f"Call from {phone}, id={call_id}")

    step = get_step(call_id)
    log.info(f"Current step: {step}")

    if step == 'main_choice':
        return main_choice(call_id)
    elif step == 'handle_options':
        return handle_options(call_id)
    elif step == 'wallet_or_continue':
        return wallet_or_continue(call_id)
    elif step == 'recording_done':
        return recording_done(call_id)
    elif step == 'choose_delivery':
        return choose_delivery(call_id)
    elif step == 'handle_wallet':
        return handle_wallet(call_id)
    elif step == 'process_topup':
        return process_topup(call_id)
    elif step == 'confirm_topup':
        return confirm_topup(call_id)
    elif step == 'handle_update':
        return handle_update(call_id)
    elif step == 'save_email':
        return save_email(call_id)
    elif step == 'save_fax':
        return save_fax(call_id)

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()

    if customer.is_blocked:
        return r('מצטערים חשבונך חסום לפרטים פנה לשירות לקוחות')

    balance_msg = f'יתרתך היא {customer.balance:.2f} שקל ' if customer.balance > 0 else ''

    set_step(call_id, 'main_choice')
    return r(
        f'שלום וברוכים הבאים למערכת התמלול '
        f'{balance_msg}'
        f'להתחלת הקלטה הקש 1 '
        f'לתפריט אפשרויות הקש 2',
        'main_choice'
    )

def main_choice(call_id):
    choice = get_param('Digits')
    if choice == '1':
        return check_balance(call_id)
    elif choice == '2':
        return options_menu(call_id)
    else:
        set_step(call_id, 'main_choice')
        return r('בחירה לא חוקית להתחלת הקלטה הקש 1 לתפריט אפשרויות הקש 2', 'main_choice')

def options_menu(call_id):
    set_step(call_id, 'handle_options')
    return r(
        'לטעינת ארנק הקש 1 '
        'לעדכון פרטים הקש 2 '
        'להסבר על המערכת הקש 3 '
        'לחזרה הקש 0',
        'handle_options'
    )

def handle_options(call_id):
    choice = get_param('Digits')
    if choice == '1':
        return wallet_menu(call_id)
    elif choice == '2':
        return update_details(call_id)
    elif choice == '3':
        explanation = get_setting('system_explanation', 'מערכת זו מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס העלות היא לפי אורך ההקלטה')
        set_step(call_id, 'handle_options')
        return r(explanation, 'handle_options')
    elif choice == '0':
        set_step(call_id, 'main_choice')
        return r('להתחלת הקלטה הקש 1 לתפריט אפשרויות הקש 2', 'main_choice')
    else:
        set_step(call_id, 'handle_options')
        return r('בחירה לא חוקית', 'handle_options')

def check_balance(call_id):
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    min_balance = float(get_setting('min_balance', '0'))
    if not customer or customer.balance <= min_balance:
        set_step(call_id, 'wallet_or_continue')
        return r('יתרתך נמוכה למעבר לטעינת ארנק הקש 1 להמשך ללא תשלום הקש 2', 'wallet_or_continue')
    return start_recording(call_id)

def wallet_or_continue(call_id):
    choice = get_param('Digits')
    if choice == '1':
        set_step(call_id, 'process_topup')
        return r('הקש את הסכום בשקלים ולאחר מכן הקש סולמית', 'process_topup', input_len=6)
    elif choice == '2':
        return start_recording(call_id)
    else:
        set_step(call_id, 'wallet_or_continue')
        return r('בחירה לא חוקית', 'wallet_or_continue')

def start_recording(call_id):
    max_sec = int(get_setting('max_recording_seconds', '1800'))
    set_step(call_id, 'recording_done')
    return r('השאר את הודעתך לאחר הצליל לסיום הקש סולמית או נתק', 'recording_done', record=max_sec)

def recording_done(call_id):
    phone = get_param('ApiPhone')
    rec_url = get_param('RecordingUrl')
    duration = int(get_param('Duration', '0'))

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return r('שגיאה נסה שוב')

    cost_per_half_hour = float(get_setting('cost_per_half_hour', '0'))
    if duration > 0 and cost_per_half_hour > 0:
        cost = (duration / 1800) * cost_per_half_hour
        customer.balance = max(0, customer.balance - cost)

    rec = Recording(
        call_id=call_id,
        customer_id=customer.id,
        duration_seconds=duration,
        status='processing',
        delivery_method=customer.delivery_method or 'email',
        delivered_to=customer.email or customer.fax or ''
    )
    db.session.add(rec)
    db.session.commit()

    if rec_url:
        if customer.email or customer.fax:
            transcribe_async(
                call_id, rec_url, customer.id,
                customer.delivery_method or 'email',
                customer.email or customer.fax or '',
                duration
            )
            return r('ההקלטה התקבלה התמלול ישלח אליך בקרוב שיחה טובה')
        else:
            set_step(call_id, 'choose_delivery')
            return r('ההקלטה התקבלה לשליחה למייל הקש 1 לפקס הקש 2', 'choose_delivery')
    return r('ההקלטה התקבלה תודה')

def choose_delivery(call_id):
    choice = get_param('Digits', '1')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer:
        customer.delivery_method = 'email' if choice == '1' else 'fax'
        db.session.commit()
    dest = 'מייל' if choice == '1' else 'פקס'
    return r(f'תודה התמלול ישלח ל{dest} שיחה טובה')

def wallet_menu(call_id):
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    balance = customer.balance if customer else 0
    set_step(call_id, 'handle_wallet')
    return r(
        f'יתרתך היא {balance:.2f} שקל '
        f'לשמיעת יתרה הקש 1 '
        f'לטעינה הקש 2 '
        f'לחזרה הקש 0',
        'handle_wallet'
    )

def handle_wallet(call_id):
    choice = get_param('Digits')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if choice == '1':
        balance = customer.balance if customer else 0
        set_step(call_id, 'handle_wallet')
        return r(f'יתרתך היא {balance:.2f} שקל', 'handle_wallet')
    elif choice == '2':
        set_step(call_id, 'process_topup')
        return r('הקש את הסכום לטעינה ולאחר מכן הקש סולמית', 'process_topup', input_len=6)
    elif choice == '0':
        return options_menu(call_id)
    else:
        set_step(call_id, 'handle_wallet')
        return r('בחירה לא חוקית', 'handle_wallet')

def process_topup(call_id):
    amount_str = get_param('Digits', '0')
    try:
        amount = float(amount_str)
        if amount < 5:
            set_step(call_id, 'process_topup')
            return r('הסכום המינימלי לטעינה הוא 5 שקל נסה שוב', 'process_topup')
        set_step(call_id, 'confirm_topup')
        return r(f'לטעינת {amount:.0f} שקל הקש 1 לאישור', 'confirm_topup')
    except:
        set_step(call_id, 'process_topup')
        return r('סכום לא תקין נסה שוב', 'process_topup')

def confirm_topup(call_id):
    return r('הטעינה תבוצע בקרוב תקבל אישור למייל שיחה טובה')

def update_details(call_id):
    set_step(call_id, 'handle_update')
    return r(
        'לעדכון מייל הקש 1 '
        'לעדכון פקס הקש 2 '
        'לחזרה הקש 0',
        'handle_update'
    )

def handle_update(call_id):
    choice = get_param('Digits')
    if choice == '1':
        set_step(call_id, 'save_email')
        return r('אמור בקול ברור את כתובת המייל שלך לאחר הצליל', 'save_email', record=15)
    elif choice == '2':
        set_step(call_id, 'save_fax')
        return r('הקש את מספר הפקס ולאחר מכן הקש סולמית', 'save_fax', input_len=15)
    elif choice == '0':
        return options_menu(call_id)
    else:
        set_step(call_id, 'handle_update')
        return r('בחירה לא חוקית', 'handle_update')

def save_email(call_id):
    phone = get_param('ApiPhone')
    rec_url = get_param('RecordingUrl')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer and rec_url:
        from services.transcribe import _download, _whisper
        import re
        path = _download(rec_url, f'email_{phone}')
        if path:
            text = _whisper(path)
            os.remove(path)
            if text:
                email = text.lower().strip()
                email = email.replace(' shtrudel ', '@').replace(' at ', '@')
                email = email.replace(' nekuda ', '.').replace(' dot ', '.')
                email = re.sub(r'\s+', '', email)
                if '@' in email:
                    customer.email = email
                    db.session.commit()
                    return r('המייל שלך עודכן בהצלחה שיחה טובה')
    set_step(call_id, 'handle_update')
    return r('לא הצלחתי לזהות את המייל נסה שוב', 'handle_update')

def save_fax(call_id):
    phone = get_param('ApiPhone')
    fax = get_param('Digits')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer and fax:
        customer.fax = fax
        db.session.commit()
        return r('מספר הפקס עודכן בהצלחה שיחה טובה')
    set_step(call_id, 'handle_update')
    return r('שגיאה נסה שוב', 'handle_update')
