from flask import Blueprint, request, current_app
import uuid
import logging
from services.database import save_call, update_call_status, set_delivery_preference
from services.transcribe_service import transcribe_async
from services.t9 import t9_to_text

ivr_bp = Blueprint('ivr', __name__)
log = logging.getLogger(__name__)

# T9 map: digit -> letters
T9_MAP = {
    '2': 'abc', '3': 'def', '4': 'ghi', '5': 'jkl',
    '6': 'mno', '7': 'pqrs', '8': 'tuv', '9': 'wxyz',
    '0': '.@-_'
}


@ivr_bp.route('/incoming', methods=['GET', 'POST'])
def incoming():
    call_id = request.args.get('callId', str(uuid.uuid4()))
    caller = request.args.get('ApiPhone', 'unknown')
    log.info(f"incoming call: {call_id} from {caller}")
    save_call(call_id, caller)

    response = (
        "1:\n"
        "read=shalom umevorachim lemaarechet hatamul. "
        "lehaklata vedivur lemail hakish 1. "
        "lehakladat shimush bamail hapenimi hakish 2.\n"
        "input=1,5,1,choose_method,1\n"
    )
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ivr_bp.route('/choose_method', methods=['GET', 'POST'])
def choose_method():
    call_id = request.args.get('callId', '')
    choice = request.args.get('Digits', '1')

    if choice == '1':
        # voice recording for email
        response = (
            "1:\n"
            "read=acharei hatzliל tamer et ktovet hamail shelcha. "
            "lemaashal: moshe shtrudel gmail nekuda com. az hakish kokhavit veshamor.\n"
            "record=1,15,1,got_email_voice\n"
        )
    else:
        # T9 input
        response = (
            "1:\n"
            "read=hakish et ktovet hamail shelcha beshitat T9. "
            "kol otiot baveit 2: alef beit gimel. "
            "efshar lehakish kokhavit laavor laot habaa. "
            "0 lineshar. besiyum hakish tashtit.\n"
            "input=1,60,*,got_email_t9,#\n"
        )
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ivr_bp.route('/got_email_voice', methods=['GET', 'POST'])
def got_email_voice():
    """User recorded their email address by voice - transcribe it"""
    call_id = request.args.get('callId', '')
    rec_url = request.args.get('RecordingUrl', '')

    if rec_url:
        from services.transcribe_service import transcribe_email_voice
        email = transcribe_email_voice(rec_url)
        if email and '@' in email:
            set_delivery_preference(call_id, 'email', email)
            response = (
                f"1:\n"
                f"read=hamail shelcha hu {email.replace('@', ' shtrudel ').replace('.', ' nekuda ')}. "
                f"im nachon hakish 1, im lo hakish 2.\n"
                f"input=1,5,1,confirm_email,1\n"
            )
        else:
            response = (
                "1:\n"
                "read=lo hatzlachti lizaot et ktovet hamail. nase shenit.\n"
                "goto=choose_method\n"
            )
    else:
        response = "1:\ngoto=choose_method\n"

    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ivr_bp.route('/got_email_t9', methods=['GET', 'POST'])
def got_email_t9():
    """User entered email via T9 keypresses"""
    call_id = request.args.get('callId', '')
    digits = request.args.get('Digits', '')

    email = t9_to_text(digits)
    log.info(f"T9 digits: {digits} -> email: {email}")

    if email and '@' in email:
        set_delivery_preference(call_id, 'email', email)
        readable = email.replace('@', ' shtrudel ').replace('.', ' nekuda ')
        response = (
            f"1:\n"
            f"read=hamail shelcha hu {readable}. "
            f"im nachon hakish 1, im lo hakish 2.\n"
            f"input=1,5,1,confirm_email,1\n"
        )
    else:
        response = (
            "1:\n"
            "read=lo hatzlachti lehavin et hamail. nase shenit.\n"
            "goto=choose_method\n"
        )

    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ivr_bp.route('/confirm_email', methods=['GET', 'POST'])
def confirm_email():
    call_id = request.args.get('callId', '')
    choice = request.args.get('Digits', '1')

    if choice == '1':
        # Email confirmed - now record the message
        max_sec = current_app.config.get('MAX_RECORDING_SECONDS', 300)
        response = (
            f"1:\n"
            f"read=tov meod. achshav haklet et hahaoda shelcha acharei hatzliל. "
            f"lisiyum lakhatzu kokhavit.\n"
            f"record=1,{max_sec},1,recording_done\n"
        )
    else:
        response = (
            "1:\n"
            "read=beseder, nase shenit.\n"
            "goto=choose_method\n"
        )

    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ivr_bp.route('/recording_done', methods=['GET', 'POST'])
def recording_done():
    call_id = request.args.get('callId', '')
    rec_url = request.args.get('RecordingUrl', '')
    caller = request.args.get('ApiPhone', '')
    log.info(f"recording done: {call_id}")

    if rec_url:
        transcribe_async(call_id, rec_url, caller)

    response = (
        "1:\n"
        "read=toda raba. hahaklata התקבלה vehatamul yishalach eleicha bekarov. shalom.\n"
        "hangup=\n"
    )
    return response, 200, {'Content-Type': 'text/plain; charset=utf-8'}
