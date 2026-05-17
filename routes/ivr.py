from flask import Blueprint, request
from app import db
from models import Customer, Recording, Settings
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

def r(text, next_step=None, input_len=1, timeout=10, record=None):
    text = text.replace('.', ' ').replace('-', ' ')
    if record:
        next_url = f'{BASE_URL}/{next_step}' if next_step else ''
        response = f'read=t-{text}=rec,,record,,,{record},no,yes,{next_url}'
    elif next_step:
        next_url = f'{BASE_URL}/{next_step}'
        response = f'read=t-{text}=Digits,,1,{input_len},{timeout},Number,yes,{next_url}'
    else:
        response = f'id_list_message=t-{text}'
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@ivr_bp.route('/incoming', methods=['GET', 'POST'])
def incoming():
    log.info(f"GET params: {dict(request.args)}")
    log.info(f"POST params: {dict(request.form)}")
    phone = get_param('ApiPhone')
    call_id = get_param('ApiCallId', str(uuid.uuid4()))
    log.info(f"Call from {phone}, id={call_id}")

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()

    if customer.is_blocked:
        return r('מצטערים חשבונך חסום לפרטים פנה לשירות לקוחות')

    if customer.balance > 0:
        balance_msg = f'יתרתך היא {customer.balance:.2f} שקל '
    else:
        balance_msg = ''

    return r(
        f'שלום וברוכים הבאים למערכת התמלול '
        f'{balance_msg}'
        f'להתחלת הקלטה הקש 1 '
        f'לתפריט אפשרויות הקש 2',
        'main_choice'
    )

@ivr_bp.route('/main_choice', methods=['GET', 'POST'])
def main_choice():
    choice = get_param('Digits')
    if choice == '1':
        return check_balance()
    elif choice == '2':
        return options_menu()
    else:
        return r('בחירה לא חוקית להתחלת הקלטה הקש 1 לתפריט אפשרויות הקש 2', 'main_choice')

@ivr_bp.route('/options_menu', methods=['GET', 'POST'])
def options_menu():
    return r(
        'לטעינת ארנק הקש 1 '
        'לעדכון פרטים הקש 2 '
        'להסבר על המערכת הקש 3 '
        'לחזרה הקש 0',
        'handle_options'
    )

@ivr_bp.route('/handle_options', methods=['GET', 'POST'])
def handle_options():
    choice = get_param('Digits')
    if choice == '1':
        return wallet_menu()
    elif choice == '2':
        return update_details()
    elif choice == '3':
        explanation = get_setting(
            'system_explanation',
            'מערכת זו מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס '
            'העלות היא לפי אורך ההקלטה'
        )
        return r(explanation, 'options_menu')
    elif choice == '0':
        return r('להתחלת הקלטה הקש 1 לתפריט אפשרויות הקש 2', 'main_choice')
    else:
        return r('בחירה לא חוקית', 'options_menu')

def check_balance():
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    min_balance = float(get_setting('min_balance', '0'))
    if not customer or customer.balance <= min_balance:
        return r(
            'יתרתך נמוכה '
            'למעבר לטעינת ארנק הקש 1 '
            'להמשך ללא תשלום הקש 2',
            'wallet_or_continue'
        )
    return start_recording()

@ivr_bp.route('/wallet_or_continue', methods=['GET', 'POST'])
def wallet_or_continue():
    choice = get_param('Digits')
    if choice == '1':
        return r('הקש את הסכום בשקלים ולאחר מכן הקש סולמית', 'process_topup', input_len=6)
    elif choice == '2':
        return start_recording()
    else:
        return r('בחירה לא חוקית', 'wallet_or_continue')

def start_recording():
    max_sec = int(get_setting('max_recording_seconds', '1800'))
    return r(
        'השאר את הודעתך לאחר הצליל לסיום הקש סולמית או נתק',
        'recording_done',
        record=max_sec
    )

@ivr_bp.route('/recording_done', methods=['GET', 'POST'])
def recording_done():
    phone = get_param('ApiPhone')
    call_id = get_param('ApiCallId', str(uuid.uuid4()))
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
            return r('ההקלטה התקבלה לשליחה למייל הקש 1 לפקס הקש 2', 'choose_delivery')
    return r('ההקלטה התקבלה תודה')

@ivr_bp.route('/choose_delivery', methods=['GET', 'POST'])
def choose_delivery():
    choice = get_param('Digits', '1')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer:
        customer.delivery_method = 'email' if choice == '1' else 'fax'
        db.session.commit()
    dest = 'מייל' if choice == '1' else 'פקס'
    return r(f'תודה התמלול ישלח ל{dest} שיחה טובה')

@ivr_bp.route('/wallet_menu', methods=['GET', 'POST'])
def wallet_menu():
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    balance = customer.balance if customer else 0
    return r(
        f'יתרתך היא {balance:.2f} שקל '
        f'לשמיעת יתרה הקש 1 '
        f'לטעינה הקש 2 '
        f'לחזרה הקש 0',
        'handle_wallet'
    )

@ivr_bp.route('/handle_wallet', methods=['GET', 'POST'])
def handle_wallet():
    choice = get_param('Digits')
    phone = get_param('ApiPhone')
    customer = Customer.query.filter_by(phone=phone).first()
    if choice == '1':
        balance = customer.balance if customer else 0
        return r(f'יתרתך היא {balance:.2f} שקל', 'wallet_menu')
    elif choice == '2':
        return r('הקש את הסכום לטעינה ולאחר מכן הקש סולמית', 'process_topup', input_len=6)
    elif choice == '0':
        return options_menu()
    else:
        return r('בחירה לא חוקית', 'wallet_menu')

@ivr_bp.route('/process_topup', methods=['GET', 'POST'])
def process_topup():
    amount_str = get_param('Digits', '0')
    try:
        amount = float(amount_str)
        if amount < 5:
            return r('הסכום המינימלי לטעינה הוא 5 שקל נסה שוב', 'wallet_menu')
        return r(f'לטעינת {amount:.0f} שקל הקש 1 לאישור', 'confirm_topup')
    except:
        return r('סכום לא תקין נסה שוב', 'wallet_menu')

@ivr_bp.route('/confirm_topup', methods=['GET', 'POST'])
def confirm_topup():
    return r('הטעינה תבוצע בקרוב תקבל אישור למייל שיחה טובה')

@ivr_bp.route('/update_details', methods=['GET', 'POST'])
def update_details():
    return r(
        'לעדכון מייל הקש 1 '
        'לעדכון פקס הקש 2 '
        'לחזרה הקש 0',
        'handle_update'
    )

@ivr_bp.route('/handle_update', methods=['GET', 'POST'])
def handle_update():
    choice = get_param('Digits')
    if choice == '1':
        return r('אמור בקול ברור את כתובת המייל שלך לאחר הצליל', 'save_email', record=15)
    elif choice == '2':
        return r('הקש את מספר הפקס ולאחר מכן הקש סולמית', 'save_fax', input_len=15)
    elif choice == '0':
        return options_menu()
    else:
        return r('בחירה לא חוקית', 'update_details')

@ivr_bp.route('/save_email', methods=['GET', 'POST'])
def save_email():
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
    return r('לא הצלחתי לזהות את המייל נסה שוב', 'update_details')

@ivr_bp.route('/save_fax', methods=['GET', 'POST'])
def save_fax():
    phone = get_param('ApiPhone')
    fax = get_param('Digits')
    customer = Customer.query.filter_by(phone=phone).first()
    if customer and fax:
        customer.fax = fax
        db.session.commit()
        return r('מספר הפקס עודכן בהצלחה שיחה טובה')
    return r('שגיאה נסה שוב', 'update_details')
