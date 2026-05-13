from flask import Blueprint, request, current_app
from app import db
from models import Customer, Recording, Transaction, Settings
from services.transcribe import transcribe_async
import uuid, logging

ivr_bp = Blueprint('ivr', __name__)
log = logging.getLogger(__name__)

BASE_URL = 'https://web-production-90272.up.railway.app/ivr'

def get_param(key, default=''):
    return request.args.get(key) or request.form.get(key) or default

def get_setting(key, default=''):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def r(text, next_route=None, input_len=1, timeout=10, terminator='#', record=None):
    lines = [f'read={text}']
    if record:
        next_url = f'{BASE_URL}/{next_route}' if next_route else ''
        lines.append(f'record=1,{record},1,{next_url}')
    elif next_route:
        next_url = f'{BASE_URL}/{next_route}'
        lines.append(f'input={input_len},{timeout},1,{next_url},{terminator}')
        return '\n'.join(lines) + '\n', 200, {'Content-Type': 'text/plain; charset=utf-8'}
@ivr_bp.route('/incoming', methods=['GET', 'POST'])
def incoming():
    log.info(f"GET params: {dict(request.args)}")
    log.info(f"POST params: {dict(request.form)}")
    phone = get_param('ApiPhone')
    call_id = get_param('callId', str(uuid.uuid4()))
    log.info(f"Call from {phone}, id={call_id}")

    customer = Customer.query.filter_by(phone=phone).first()

    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()
        welcome = get_setting('welcome_new', 'שלום וברוכים הבאים למערכת התמלול.')
        return r(f'{welcome} לתפריט הקש 1', 'main_menu')

    if customer.is_blocked:
        return r('מצטערים, חשבונך חסום. לפרטים פנה לשירות לקוחות.')

    welcome = get_setting('welcome_returning', 'שלום וברוכים השבים.')
    balance_msg = f'יתרתך היא {customer.balance:.2f} שקל.'
    return r(f'{welcome} {balance_msg} לתפריט הקש 1', 'main_menu')

@ivr_bp.route('/main_menu', methods=['GET', 'POST'])
def main_menu():
    menu = (
        'להתחלת הקלטה הקש 1. '
        'לארנק וטעינה הקש 2. '
        'לעדכון פרטים הקש 3. '
        'להסבר על המערכת הקש 9.'
    )
    return r(menu, 'handle_menu')

@ivr_bp.route('/handle_menu', methods=['GET', 'POST'])
def handle_menu():
    choice = get_param('Digits')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()

    if choice == '1':
        min_balance = float(get_setting('min_balance', '5'))
        if not customer or customer.balance < min_balance:
            return r('אין לך מספיק כסף בארנק. לטעינה הקש 1, לחזרה הקש 0.', 'wallet_or_back')
        max_sec = int(get_setting('max_recording_seconds', '1800'))
        return r('השאר את הודעתך לאחר הצליל. לסיום הקש # או נתק.', 'recording_done', record=max_sec)
    elif choice == '2':
        return r('לשמיעת יתרה הקש 1. לטעינה הקש 2. לחזרה הקש 0.', 'wallet_menu')
    elif choice == '3':
        return r('לעדכון מייל הקש 1. לעדכון פקס הקש 2. לחזרה הקש 0.', 'update_details')
    elif choice == '9':
        explanation = get_setting('system_explanation', 'מערכת התמלול מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס.')
        return r(explanation, 'main_menu')
    else:
        return r('בחירה לא חוקית.', 'main_menu')

@ivr_bp.route('/wallet_or_back', methods=['GET', 'POST'])
def wallet_or_back():
    choice = get_param('Digits')
    if choice == '1':
        return r('הקש את הסכום בשקלים ולאחר מכן הקש #', 'process_topup', input_len=6)
    return r('חוזר לתפריט הראשי', 'main_menu')

@ivr_bp.route('/wallet_menu', methods=['GET', 'POST'])
def wallet_menu():
    choice = get_param('Digits')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if choice == '1':
        balance = customer.balance if customer else 0
        return r(f'יתרתך היא {balance:.2f} שקל. לחזרה הקש כל מקש.', 'main_menu')
    elif choice == '2':
        return r('הקש את הסכום לטעינה ולאחר מכן הקש #', 'process_topup', input_len=6)
    return r('חוזר לתפריט הראשי.', 'main_menu')

@ivr_bp.route('/process_topup', methods=['GET', 'POST'])
def process_topup():
    amount_str = get_param('Digits', '0')
    try:
        amount = float(amount_str)
        if amount < 5:
            return r('הסכום המינימלי לטעינה הוא 5 שקל. נסה שוב.', 'wallet_menu')
        return r(f'לטעינת {amount:.0f} שקל תועבר לסליקה. הקש 1 לאישור.', 'confirm_topup')
    except:
        return r('סכום לא תקין. נסה שוב.', 'wallet_menu')

@ivr_bp.route('/confirm_topup', methods=['GET', 'POST'])
def confirm_topup():
    return r('הטעינה תבוצע בקרוב. תקבל אישור למייל. חוזר לתפריט.', 'main_menu')

@ivr_bp.route('/update_details', methods=['GET', 'POST'])
def update_details():
    choice = get_param('Digits')
    if choice == '1':
        return r('אמור בקול ברור את כתובת המייל שלך. לאחר הצליל.', 'save_email', record=15)
    elif choice == '2':
        return r('הקש את מספר הפקס ולאחר מכן הקש #', 'save_fax', input_len=15)
    return r('חוזר לתפריט.', 'main_menu')

@ivr_bp.route('/save_email', methods=['GET', 'POST'])
def save_email():
    phone = get_param('ApiPhone')
    rec_url = get_param('RecordingUrl')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer and rec_url:
        from services.transcribe import _download, _whisper
        import os, re
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
                    return r(f'המייל שלך עודכן ל {email}. לחזרה הקש כל מקש.', 'main_menu')
    return r('לא הצלחתי לזהות את המייל. נסה שוב.', 'update_details')

@ivr_bp.route('/save_fax', methods=['GET', 'POST'])
def save_fax():
    phone = get_param('ApiPhone')
    fax = get_param('Digits')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer and fax:
        customer.fax = fax
        db.session.commit()
        return r('מספר הפקס עודכן. לחזרה הקש כל מקש.', 'main_menu')
    return r('שגיאה. נסה שוב.', 'update_details')

@ivr_bp.route('/recording_done', methods=['GET', 'POST'])
def recording_done():
    phone = get_param('ApiPhone')
    call_id = get_param('callId', str(uuid.uuid4()))
    rec_url = get_param('RecordingUrl')
    duration = int(get_param('Duration', '0'))

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return r('שגיאה. נסה שוב.')

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

    if rec_url and (customer.email or customer.fax):
        transcribe_async(call_id, rec_url, customer.id, customer.delivery_method or 'email', customer.email or customer.fax or '', duration)
        return r('ההקלטה התקבלה. התמלול ישלח אליך בקרוב. לשליחה למייל הקש 1, לפקס הקש 2.', 'choose_delivery')
    else:
        return r('ההקלטה התקבלה. לשליחה למייל הקש 1, לפקס הקש 2.', 'choose_delivery')

@ivr_bp.route('/choose_delivery', methods=['GET', 'POST'])
def choose_delivery():
    choice = get_param('Digits', '1')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer:
        customer.delivery_method = 'email' if choice == '1' else 'fax'
        db.session.commit()
    dest = 'מייל' if choice == '1' else 'פקס'
    return r(f'תודה. התמלול ישלח ל{dest}. שיחה טובה.')
