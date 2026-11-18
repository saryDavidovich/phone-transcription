"""
טעינה אוטומטית ללקוח פרטי - בלי לשמור אצלנו פרטי כרטיס.

הקמה (פעם אחת, באזור האישי): הלקוח מקליד את הכרטיס באייפרם של נדרים פלוס, ונפתחת אצלם הוראת קבע
(HK) עם תאריך חיוב ראשון רחוק. כשה-CallBack של ההקמה מגיע (רק מכתובות נדרים), שומרים את מזהה ההוראה
ו-4 ספרות, ומקפיאים את ההוראה מיד (DisableKeva) - כך היא לעולם לא מחייבת לבד, רק משמשת "כרטיס שמור".
הכרטיס עצמו נשמר אצל נדרים בלבד.

חיוב: כל כמה דקות (services/tasks) - לקוח שהיתרה שלו מתחת לסף מקבל חיוב בודד מההוראה
(TashlumBodedNew, POST מהשרת עם מפתח ה-API). התשובה מזכה מיד (credit_charge, 'auto'), וה-Webhook
שמגיע אחר כך מזוהה לפי TP-<מזהה> בהערות ולא מזכה שוב.

הגנות: חיוב אחד לשעה לכל היותר, תקרה חודשית (ברירת מחדל 300 ₪), שני כישלונות ברצף = כיבוי ומייל ללקוח.
מופעל במתג auto_topup_on (הטלפון של המנהל תמיד).
"""
import logging
import secrets
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
MANAGE_URL = 'https://matara.pro/nedarimplus/Reports/Manage3.aspx'
ADMIN_PHONE = '0527134491'
_LOCK_KEY = 77001234


def enabled_for(c):
    from models import Settings
    from services.customer_phones import all_phones
    row = Settings.query.filter_by(key='auto_topup_on').first()
    if row and row.value == '1':
        return True
    try:
        return ADMIN_PHONE in all_phones(c)
    except Exception:
        return False


def configured():
    import os
    from services.nedarim_messages import _credentials
    mosad, key = _credentials()
    return bool(mosad and key and os.environ.get('NEDARIM_API_VALID'))


def get(c, create=False):
    from app import db
    from models import AutoTopup
    a = AutoTopup.query.filter_by(customer_id=c.id).first()
    if not a and create:
        a = AutoTopup(customer_id=c.id, enabled=False, status='off')
        db.session.add(a)
        db.session.flush()
    return a


def start_setup(c, amount, threshold):
    """מכין הקמה: מחזיר את מה שהדף שולח לאייפרם (FinishTransaction2 עם HK)."""
    import os
    from app import db
    from routes.institution_billing import NEDARIM_CATEGORY
    a = get(c, create=True)
    a.amount, a.threshold = amount, threshold
    a.setup_token = secrets.token_urlsafe(24)
    a.status = 'setup' if a.status != 'active' else a.status
    db.session.commit()
    app_url = (os.environ.get('APP_URL', '').rstrip('/') or '')
    if not app_url:
        from flask import request
        app_url = request.url_root.rstrip('/')
    app_url = app_url.replace('http://', 'https://', 1)
    cb = f"{app_url}/api/nedarim/autotopup-callback/{a.setup_token}"
    if os.environ.get('NEDARIM_WEBHOOK_KEY'):
        import urllib.parse as up
        cb += '?key=' + up.quote(os.environ['NEDARIM_WEBHOOK_KEY'], safe='')
    start = (datetime.utcnow() + timedelta(days=730)).strftime('%d/%m/%Y')
    clean = lambda v, n: ''.join(ch for ch in (v or '') if ch not in '<>')[:n]
    return {
        'Mosad': os.environ.get('NEDARIM_MOSAD', ''), 'ApiValid': os.environ.get('NEDARIM_API_VALID', ''),
        'PaymentType': 'HK', 'Currency': '1', 'Zeout': '', 'FirstName': clean(c.name, 50) or 'לקוח', 'LastName': '',
        'Street': '', 'City': '', 'Phone': c.phone if (c.phone or '').isdigit() else '', 'Mail': clean(c.email, 50),
        'Amount': f'{amount:g}', 'Tashlumim': '', 'Day': '', 'StartFrom': start,
        'Groupe': NEDARIM_CATEGORY, 'Comment': f'כרטיס שמור לטעינה אוטומטית - לקוח {c.id} (מוקפא)',
        'Param1': '', 'Param2': f'tpa-{a.id}', 'ForceUpdateMatching': '', 'ThirdPartyReceipt': '',
        'CallBack': cb, 'CallBackMailError': '', 'Tokef': '',
    }


def setup_callback(token, data):
    """CallBack של הקמת ההוראה (רק מכתובות נדרים). שומר מזהה + 4 ספרות ומקפיא."""
    from app import db
    from models import AutoTopup
    a = AutoTopup.query.filter_by(setup_token=token).with_for_update().first() if token else None
    if not a:
        return 'not_found'
    if str(data.get('Status') or '').upper() != 'OK':
        db.session.rollback()
        return 'not_ok'
    keva = str(data.get('ID') or data.get('KevaId') or '').strip()
    if not keva.isdigit():
        db.session.rollback()
        return 'no_id'
    if str(data.get('Param2') or f'tpa-{a.id}') != f'tpa-{a.id}':
        db.session.rollback()
        return 'mismatch'
    old = a.keva_id
    a.keva_id = keva
    a.last4 = str(data.get('LastNum') or '')[-4:] or None
    a.enabled = True
    a.status = 'active'
    a.fails = 0
    a.last_error = None
    a.setup_token = None
    db.session.commit()
    _freeze(keva)
    if old and old != keva:
        _freeze(old)
    log.info(f'auto topup: customer {a.customer_id} saved card (keva {keva})')
    return 'ok'


def _freeze(keva):
    import requests
    from services.nedarim_messages import _credentials
    mosad, key = _credentials()
    if not mosad or not key:
        return False
    try:
        r = requests.get(MANAGE_URL, params={'Action': 'DisableKeva', 'MosadId': mosad, 'ApiPassword': key,
                                             'KevaId': keva}, timeout=20)
        ok = (r.text or '').strip().startswith('OK')
        log.info(f'auto topup: freeze keva {keva}: {(r.text or "")[:80]}')
        return ok
    except Exception as e:
        log.warning(f'auto topup: freeze keva {keva} failed: {e}')
        return False


def disable(c):
    from app import db
    a = get(c)
    if not a:
        return
    keva = a.keva_id
    a.enabled = False
    a.status = 'off'
    a.keva_id = None
    a.last4 = None
    db.session.commit()
    if keva:
        _delete(keva)


def _delete(keva):
    import requests
    from services.nedarim_messages import _credentials
    mosad, key = _credentials()
    if not mosad or not key:
        return
    try:
        r = requests.get(MANAGE_URL, params={'Action': 'DeleteKeva', 'MosadId': mosad, 'ApiPassword': key,
                                             'KevaId': keva}, timeout=20)
        log.info(f'auto topup: delete keva {keva}: {(r.text or "")[:80]}')
    except Exception as e:
        log.warning(f'auto topup: delete keva {keva} failed: {e}')


def month_total(customer_id):
    from app import db
    from models import CustomerChargeLog
    since = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(db.session.query(db.func.coalesce(db.func.sum(CustomerChargeLog.amount), 0))
                 .filter(CustomerChargeLog.customer_id == customer_id, CustomerChargeLog.kind == 'auto',
                         CustomerChargeLog.status == 'success', CustomerChargeLog.created_at >= since).scalar() or 0)


def charge_now(a, c):
    """חיוב בודד מההוראה. מחזיר תוצאה טקסטואלית."""
    import requests
    from app import db
    from models import CustomerChargeLog
    from routes.institution_billing import NEDARIM_CATEGORY
    from services.nedarim_messages import _credentials
    from services.customer_billing import credit_charge, marker
    mosad, key = _credentials()
    if not mosad or not key or not a.keva_id:
        return 'not_configured'
    ch = CustomerChargeLog(customer_id=c.id, amount=float(a.amount), status='pending', kind='auto',
                           provider_ref=f'keva:{a.keva_id}')
    db.session.add(ch)
    a.last_charge_at = datetime.utcnow()
    db.session.commit()
    try:
        r = requests.post(MANAGE_URL, data={
            'Action': 'TashlumBodedNew', 'MosadId': mosad, 'ApiPassword': key, 'KevaId': a.keva_id,
            'Amount': f'{float(a.amount):g}', 'Currency': '1', 'Tashlumim': '1', 'Groupe': NEDARIM_CATEGORY,
            'Comments': f'טעינה אוטומטית - לקוח {c.id} {marker(ch.id)}', 'JoinToKevaId': 'NoJoin',
            'AjaxId': f'a{ch.id}-{int(time.time())}'}, timeout=40)
        data = r.json()
    except Exception as e:
        log.warning(f'auto topup: charge for customer {c.id} failed: {e}')
        # לא יודעים אם עבר - משאירים pending; ה-Webhook (TP-<id> בהערות) יזכה אם החיוב בוצע
        return 'unknown'
    if str(data.get('Status')) == 'OK':
        res = credit_charge(ch.id, data, 'auto')
        a.fails = 0
        a.last_error = None
        db.session.commit()
        return res
    ch.status = 'failed'
    a.fails = (a.fails or 0) + 1
    a.last_error = str(data.get('Message') or 'החיוב נדחה')[:300]
    if a.fails >= 2:
        a.enabled = False
        a.status = 'failed'
        _mail_failed(c, a.last_error)
    db.session.commit()
    log.info(f'auto topup: customer {c.id} charge refused: {a.last_error}')
    return 'refused'


def _mail_failed(c, why):
    try:
        from services import customer_auth as CA
        body = (f'<p style="line-height:1.8">שלום,</p><p style="line-height:1.8">הטעינה האוטומטית של היתרה שלך בתמלול פון '
                f'לא הצליחה ({why}), ולכן כיבינו אותה.</p><p style="line-height:1.8">אפשר לטעון ידנית או להפעיל מחדש עם '
                'כרטיס אחר באזור האישי ← יתרה וחיובים.</p>')
        if c.email:
            CA.send_mail(c.email, 'הטעינה האוטומטית כובתה - תמלול פון', 'טעינה אוטומטית', body)
    except Exception:
        log.exception('auto topup mail failed')


def run_due():
    """נקרא מלולאת המשימות. חיוב לכל לקוח שהיתרה שלו מתחת לסף (פעם בשעה לכל היותר)."""
    from app import db
    from models import AutoTopup, Customer
    if db.engine.dialect.name == 'postgresql':
        if not db.session.execute(db.text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': _LOCK_KEY}).scalar():
            db.session.rollback()
            return 0
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    rows = (db.session.query(AutoTopup, Customer).join(Customer, Customer.id == AutoTopup.customer_id)
            .filter(AutoTopup.enabled.is_(True), AutoTopup.status == 'active', AutoTopup.keva_id.isnot(None),
                    Customer.balance < AutoTopup.threshold, Customer.is_blocked.isnot(True),
                    db.or_(AutoTopup.last_charge_at.is_(None), AutoTopup.last_charge_at < hour_ago))
            .limit(20).all())
    pairs = [(a.id, c.id) for a, c in rows]
    db.session.commit()            # משחרר את נעילת ה-advisory; כל חיוב בטרנזקציה משלו
    n = 0
    for aid, cid in pairs:
        a = AutoTopup.query.filter_by(id=aid).with_for_update().first()
        c = Customer.query.get(cid)
        if not a or not c or not a.enabled or not enabled_for(c) or (c.balance or 0) >= (a.threshold or 0) \
                or (a.last_charge_at and a.last_charge_at > hour_ago):
            db.session.rollback()
            continue
        if month_total(c.id) + float(a.amount) > float(a.monthly_cap or 300):
            db.session.rollback()
            continue
        charge_now(a, c)
        n += 1
        time.sleep(1)              # נדרים: עסקה אחר עסקה, לא במקביל
    return n
