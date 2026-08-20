"""
אינטגרציית תשלומים מול נדרים פלוס (ראה תיעוד שסופק).
זרימת "הקמת עסקה בצד שרת" - הסכום נקבע ונשלט אצלנו בשרת, לא בצד הלקוח:

1. מנהל המוסד ממלא סכום → /institution/billing/topup (POST) →
   השרת קורא ל-CreateTransaction אצל נדרים פלוס עם הסכום, ומקבל בחזרה
   מזהה עסקה (ID). אנחנו שומרים InstitutionChargeLog(status='pending').
2. הדפדפן מקבל את ה-ID, ומעביר אותו לאייפרם של נדרים דרך
   postMessage({Name:'FinishTransaction', Value: ID}) - האייפרם מבצע את
   הסליקה בפועל מול פרטי הכרטיס שהלקוח מזין שם (לא אצלנו).
3. האישור **היחיד המהימן** מגיע מ-Nedarim לכתובת ה-CallBack שהגדרנו
   (webhook), לא מתגובת הצד-לקוח - בדיוק כפי שהתיעוד שלהם מדגיש. רק שם
   אנחנו בפועל מזכים את יתרת המוסד.

חשוב: חיוב אוטומטי דרך טוקן שמור (ללא נוכחות הלקוח) דורש אישור נפרד
מנדרים פלוס (office@nedar.im) ואינו פתוח כברירת מחדל - לכן לא מומש כאן.
כל "תשלום נוסף" עובר תמיד דרך האייפרם (הלקוח/מנהל המוסד מזין כרטיס),
לא חיוב שקט ברקע.
"""
import os
import uuid
import logging
import requests
from flask import Blueprint, render_template, request, jsonify
from app import db
from models import Institution, InstitutionChargeLog
from routes.institution import institution_login_required

log = logging.getLogger(__name__)

institution_billing_bp = Blueprint('institution_billing', __name__)

NEDARIM_CREATE_URL = 'https://matara.pro/nedarimplus/V6/Files/WebServices/DebitIframe.aspx?Action=CreateTransaction'
# כתובות המקור הרשמיות של עדכוני ה-CallBack (מהתיעוד הרשמי של נדרים פלוס,
# בסעיף "אייפרם: אימות תשלום ואבטחה") - הן עצמן ממליצות לוודא זאת כדי
# למנוע התחזות. אנחנו כן חוסמים לפי זה (ראה nedarim_callback) - בלי זה,
# כל מי שמנחש/מנסה charge_id יכול היה "לזכות" יתרה למוסד בלי לשלם בפועל,
# כי אין שום סוד/חתימה משותפת בין הצדדים מלבד כתובת ה-IP.
NEDARIM_CALLBACK_IPS = {'18.196.146.117', '18.194.219.73'}
# קטגוריה קבועה שמתייגת כל תשלום ששייך למערכת "תמלול פון" בנדרים פלוס - הן
# תשלומי מוסדות (כאן) והן תשלומי לקוחות בודדים (ראו routes/payment.py). כך
# ה-Webhook ברמת המוסד (routes/payment.py) יודע לזהות בוודאות שמדובר בתשלום
# שלנו ולא בתשלום אחר שמתבצע תחת אותו מספר מוסד בנדרים פלוס.
NEDARIM_CATEGORY = 'תמלול פון'


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr


@institution_billing_bp.route('/institution/billing')
@institution_login_required
def billing_tab():
    from flask_login import current_user
    inst = current_user
    charges = InstitutionChargeLog.query.filter_by(institution_id=inst.id).order_by(
        InstitutionChargeLog.created_at.desc()
    ).limit(50).all()
    success_count = InstitutionChargeLog.query.filter_by(institution_id=inst.id, status='success').count()
    mosad = os.environ.get('NEDARIM_MOSAD', '')
    return render_template(
        'institution/billing.html',
        inst=inst, charges=charges, success_count=success_count,
        nedarim_configured=bool(mosad),
    )


@institution_billing_bp.route('/institution/billing/topup', methods=['POST'])
@institution_login_required
def topup():
    from flask_login import current_user
    inst = current_user

    mosad = os.environ.get('NEDARIM_MOSAD', '')
    api_valid = os.environ.get('NEDARIM_API_VALID', '')
    if not mosad or not api_valid:
        return jsonify({'error': 'חיבור נדרים פלוס לא הוגדר עדיין בצד השרת (משתני סביבה חסרים)'}), 500

    try:
        amount = float(request.json.get('amount'))
    except (TypeError, ValueError):
        return jsonify({'error': 'סכום לא תקין'}), 400
    if amount <= 0:
        return jsonify({'error': 'סכום לא תקין'}), 400

    charge = InstitutionChargeLog(institution_id=inst.id, amount=amount, status='pending')
    db.session.add(charge)
    db.session.commit()

    app_url = os.environ.get('APP_URL', '').rstrip('/') or request.url_root.rstrip('/')
    if app_url.startswith('http://'):
        # נדרים פלוס דורשים https לכתובת CallBack - אם הגענו לכאן עם http (למשל
        # כי אין ProxyFix/APP_URL ואנחנו מאחורי פרוקסי), נכריח https במקום
        # לשלוח כתובת שהם עלולים לדחות/להתעלם ממנה בשקט.
        log.warning(f'topup: callback base url came out as http:// ({app_url}) - forcing https, but check APP_URL env var')
        app_url = 'https://' + app_url[len('http://'):]
    callback_url = f"{app_url}/api/nedarim/callback/{charge.id}"

    try:
        resp = requests.post(NEDARIM_CREATE_URL, data={
            'Mosad': mosad,
            'ApiValid': api_valid,
            'PaymentType': 'Ragil',
            'Amount': str(amount),
            'Currency': '1',
            'Tashlumim': '1',
            'FirstName': inst.name,
            'Mail': inst.email or '',
            'Phone': inst.phone or '',
            'Groupe': NEDARIM_CATEGORY,  # תיוג קטגוריה קבוע - כך שגם הווידג'וק
            # ברמת המוסד (עדכוני עסקאות, ראו routes/payment.py) יודע לזהות
            # שמדובר בתשלום ששייך למערכת תמלול פון ולא לתשלום אחר כלשהו
            # שמתבצע תחת אותו מספר מוסד בנדרים פלוס.
            'Comment': f'טעינת יתרה - מוסד #{inst.id}',
            'Param1': str(charge.id),
            'CallBack': callback_url,
        }, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error(f'Nedarim CreateTransaction failed: {e}')
        charge.status = 'failed'
        db.session.commit()
        return jsonify({'error': 'שגיאה בפנייה לשירות הסליקה, נסה שוב'}), 502

    if data.get('Status') != 'OK':
        charge.status = 'failed'
        db.session.commit()
        return jsonify({'error': data.get('Message', 'שגיאה ביצירת העסקה')}), 400

    charge.provider_ref = data.get('ID')
    db.session.commit()
    return jsonify({'chargeLogId': charge.id, 'nedarimId': data.get('ID')})


@institution_billing_bp.route('/api/nedarim/callback/<int:charge_id>', methods=['POST'])
def nedarim_callback(charge_id):
    """Webhook - המקור המהימן היחיד לאישור תשלום, לפי הנחיית נדרים פלוס
    המפורשת. לא סומכים על שום תגובה שמגיעה מהדפדפן של הלקוח.

    שני תיקונים חשובים כאן:
    1. force=True ב-get_json - בלעדיו, אם הבקשה הנכנסת לא מסומנת בדיוק
       כ-Content-Type: application/json (למשל charset שונה, או כל סטייה
       קלה), Flask היה מחזיר None בשקט (silent=True) ומדלג על העדכון -
       וזה בדיוק התסמין שתואר: תשלום שהצליח בפועל אצל נדרים (קבלה נשלחה,
       מופיע אצלם כמוצלח) אבל אצלנו נשאר "ממתין" לנצח כי הקאלבק הגיע ולא
       פורש. שאר ה-webhooks הקיימים בקוד (routes/api.py) כבר משתמשים ב-
       force=True מהסיבה הזו בדיוק - כאן זה פשוט נשכח.
    2. חסימה אמיתית (403) אם הבקשה לא מגיעה מאחת מכתובות ה-IP הרשמיות של
       נדרים פלוס, לא רק לוג אזהרה - לפי התיעוד הרשמי שלהם, וגם כי charge_id
       הוא מספר סידורי פשוט לניחוש; בלי החסימה הזו, כל אחד שינחש/יריץ
       מספרים ברצף על הכתובת הזו יכול היה "לזכות" יתרה למוסד בלי לשלם.
    """
    ip = _client_ip()
    if ip not in NEDARIM_CALLBACK_IPS:
        log.warning(f'Nedarim callback REJECTED - unexpected IP: {ip} (charge_id={charge_id})')
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    charge = InstitutionChargeLog.query.get(charge_id)
    if not charge:
        return jsonify({'ok': False}), 404
    if charge.status == 'success':
        return jsonify({'ok': True})  # כבר טופל - עדכון כפול לא יזכה פעמיים

    data = request.get_json(force=True, silent=True) or {}
    if not data:
        log.warning(f'Nedarim callback: empty/unparsable body for charge {charge_id}, raw={request.get_data(as_text=True)[:500]}')
    if data.get('Status') == 'OK':
        charge.status = 'success'
        institution = Institution.query.get(charge.institution_id)
        institution.balance = (institution.balance or 0) + charge.amount
        last4 = data.get('LastNum')
        if last4:
            institution.card_last4 = last4
        db.session.commit()
        log.info(f'Institution {institution.id} topped up {charge.amount} via Nedarim (charge {charge_id})')
    else:
        charge.status = 'failed'
        db.session.commit()

    return jsonify({'ok': True})
