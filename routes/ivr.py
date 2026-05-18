from flask import Blueprint, request
from app import db
from models import Customer, Recording, Settings
from services.transcribe import transcribe_async
import uuid, logging, os

ivr_bp = Blueprint('ivr', __name__)
log = logging.getLogger(__name__)

call_sessions = {}

def get_param(key, default=''):
    return request.args.get(key) or request.form.get(key) or default

def get_setting(key, default=''):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def set_step(call_id, step):
    call_sessions[call_id] = step

def get_step(call_id):
    return call_sessions.get(call_id, '')

def r(text):
    if not text.endswith(('.', ',', '!')):
        text = text + '.'
    return text, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@ivr_bp.route('/incoming', methods=['GET', 'POST'])
def incoming():
    log.info(f"GET params: {dict(request.args)}")
    log.info(f"POST params: {dict(request.form)}")
    phone = get_param('ApiPhone')
    call_id = get_param('ApiCallId', str(uuid.uuid4()))
    log.info(f"Call from {phone}, id={call_id}")

    step = get_step(call_id)
    log.info(f"Current step: {step}")
    digits = get_param('Digits')

    if step == 'main_choice':
        if digits == '1':
            return check_balance(call_id, phone)
        elif digits == '2':
            return options_menu(call_id)
        else:
            return r('בחירה לא חוקית. להתחלת הקלטה הקש 1. לתפריט אפשרויות הקש 2.')

    elif step == 'handle_options':
        if digits == '1':
            return wallet_menu(call_id, phone)
        elif digits == '2':
            return update_details(call_id)
        elif digits == '3':
            explanation = get_setting('system_explanation', 'מערכת זו מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס.')
            set_step(call_id, 'handle_options')
            return r(explanation)
        elif digits == '0':
            set_step(call_id, 'main_choice')
            return r('להתחלת הקלטה הקש 1. לתפריט אפשרויות הקש 2.')
        else:
            set_step(call_id, 'handle_options')
            return r('בחירה לא חוקית. לטעינת ארנק הקש 1. לעדכון פרטים הקש 2. להסבר הקש 3. לחזרה הקש 0.')

    elif step == 'wallet_or_continue':
        if digits == '1':
            set_step(call_id, 'process_topup')
            return r('הקש את הסכום בשקלים ולאחר מכן הקש סולמית.')
        elif digits == '2':
            return start_recording(call_id)
        else:
            set_step(call_id, 'wallet_or_continue')
            return r('בחירה לא חוקית. למעבר לטעינת ארנק הקש 1. להמשך ללא תשלום הקש 2.')

    elif step == 'process_topup':
        try:
            amount = float(digits or '0')
            if amount < 5:
                set_step(call_id, 'process_topup')
                return r('הסכום המינימלי לטעינה הוא 5 שקל. נסה שוב.')
            set_step(call_id, 'confirm_topup')
            return r(f'לטעינת {amount:.0f} שקל הקש 1 לאישור.')
        except:
            set_step(call_id, 'process_topup')
            return r('סכום לא תקין. נסה שוב.')

    elif step == 'confirm_topup':
        return r('הטעינה תבוצע בקרוב. תקבל אישור למייל. שיחה טובה.')

    elif step == 'handle_wallet':
        if digits == '1':
            customer = Customer.query.filter_by(phone=phone).first()
            balance = customer.balance if customer else 0
            set_step(call_id, 'handle_wallet')
            return r(f'יתרתך היא {balance:.2f} שקל. לשמיעת יתרה הקש 1. לטעינה הקש 2. לחזרה הקש 0.')
        elif digits == '2':
            set_step(call_id, 'process_topup')
            return r('הקש את הסכום לטעינה ולאחר מכן הקש סולמית.')
        elif digits == '0':
            return options_menu(call_id)
        else:
            set_step(call_id, 'handle_wallet')
            return r('בחירה לא חוקית.')

    elif step == 'handle_update':
        if digits == '1':
            set_step(call_id, 'save_email')
            return r('אמור בקול ברור את כתובת המייל שלך לאחר הצליל.')
        elif digits == '2':
            set_step(call_id, 'save_fax')
            return r('הקש את מספר הפקס ולאחר מכן הקש סולמית.')
        elif digits == '0':
            return options_menu(call_id)
        else:
            set_step(call_id, 'handle_update')
            return r('בחירה לא חוקית.')

    elif step == 'save_fax':
        fax = digits
        customer = Customer.query.filter_by(phone=phone).first()
        if customer and fax:
            customer.fax = fax
            db.session.commit()
            return r('מספר הפקס עודכן בהצלחה. שיחה טובה.')
        set_step(call_id, 'handle_update')
        return r('שגיאה. נסה שוב.')

    elif step == 'recording_done':
        return recording_done(call_id, phone)

    elif step == 'choose_delivery':
        customer = Customer.query.filter_by(phone=phone).first()
        if customer:
            customer.delivery_method = 'email' if digits == '1' else 'fax'
            db.session.commit()
        dest = 'מייל' if digits == '1' else 'פקס'
        return r(f'תודה. התמלול ישלח ל{dest}. שיחה טובה.')

    # כניסה ראשונה
    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        customer = Customer(phone=phone, balance=0.0)
        db.session.add(customer)
        db.session.commit()

    if customer.is_blocked:
        return r('מצטערים. חשבונך חסום. לפרטים פנה לשירות לקוחות.')

    balance_msg = f'יתרתך היא {customer.balance:.2f} שקל. ' if customer.balance > 0 else ''
    set_step(call_id, 'main_choice')
    return r(f'שלום וברוכים הבאים למערכת התמלול. {balance_msg}להתחלת הקלטה הקש 1. לתפריט אפשרויות הקש 2.')

def options_menu(call_id):
    set_step(call_id, 'handle_options')
    return r('לטעינת ארנק הקש 1. לעדכון פרטים הקש 2. להסבר על המערכת הקש 3. לחזרה הקש 0.')

def check_balance(call_id, phone):
    customer = Customer.query.filter_by(phone=phone).first()
    min_balance = float(get_setting('min_balance', '0'))
    if not customer or customer.balance <= min_balance:
        set_step(call_id, 'wallet_or_continue')
        return r('יתרתך נמוכה. למעבר לטעינת ארנק הקש 1. להמשך ללא תשלום הקש 2.')
    return start_recording(call_id)

def start_recording(call_id):
    max_sec = int(get_setting('max_recording_seconds', '1800'))
    set_step(call_id, 'recording_done')
    return r('השאר את הודעתך לאחר הצליל. לסיום הקש סולמית או נתק.')

def wallet_menu(call_id, phone):
    customer = Customer.query.filter_by(phone=phone).first()
    balance = customer.balance if customer else 0
    set_step(call_id, 'handle_wallet')
    return r(f'יתרתך היא {balance:.2f} שקל. לשמיעת יתרה הקש 1. לטעינה הקש 2. לחזרה הקש 0.')

def update_details(call_id):
    set_step(call_id, 'handle_update')
    return r('לעדכון מייל הקש 1. לעדכון פקס הקש 2. לחזרה הקש 0.')

def recording_done(call_id, phone):
    rec_url = get_param('RecordingUrl')
    duration = int(get_param('Duration', '0'))

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return r('שגיאה. נסה שוב.')

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
            return r('ההקלטה התקבלה. התמלול ישלח אליך בקרוב. שיחה טובה.')
        else:
            set_step(call_id, 'choose_delivery')
            return r('ההקלטה התקבלה. לשליחה למייל הקש 1. לפקס הקש 2.')
    return r('ההקלטה התקבלה. תודה.')
