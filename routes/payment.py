import logging
from flask import Blueprint, request, jsonify

payment_bp = Blueprint('payment', __name__)
log = logging.getLogger(__name__)


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr


def _phone_match_candidates(raw_phone):
    """מייצר את כל צורות הכתיב הסבירות של מספר טלפון ישראלי, כדי להשוות
    מול DB בלי תלות בפורמט המדויק שבו נדרים פלוס מחזירים את השדה Phone
    (עם/בלי מקפים ורווחים, עם/בלי קידומת 0, עם/בלי קידומת מדינה 972) -
    בפרט כשמדובר במספר שכבר קיים אצלנו בפורמט שונה במקצת מזה שהמשלם הקליד
    בנדרים פלוס."""
    import re
    digits = re.sub(r'\D', '', raw_phone or '')
    if not digits:
        return []
    candidates = {digits}
    if digits.startswith('972'):
        candidates.add('0' + digits[3:])
    elif digits.startswith('0'):
        candidates.add('972' + digits[1:])
    else:
        candidates.add('0' + digits)
        candidates.add('972' + digits)
    return list(candidates)


@payment_bp.route('/nedarim/topup-link/<phone>')
def nedarim_topup_link(phone):
    """מפנה טלפון נתון לדף התשלום המתארח של נדרים פלוס (שיטת ה-Redirect
    הפשוטה מהתיעוד הרשמי שלהם - בלי שום פיתוח בצד שלנו: לא CreateTransaction,
    לא אייפרם) עם הטלפון וקטגוריית "תמלול פון" ממולאים מראש. זו הדרך לתת
    לכל לקוח בקצה - לא רק מוסדות - לינק אישי לטעינת יתרה בנדרים פלוס; הזיכוי
    בפועל קורה כשמגיע עדכון ל-Webhook ברמת המוסד (ראו nedarim_webhook למטה),
    לא מהדפדפן. אפשר לשלוח את הקישור הזה ב-SMS/מייל ללקוח, או לקשר אליו
    מתוך שלוחת IVR.
    """
    import os
    import urllib.parse
    from flask import redirect

    mosad = os.environ.get('NEDARIM_MOSAD', '')
    if not mosad:
        return 'חיבור נדרים פלוס לא הוגדר עדיין בצד השרת', 500

    from routes.institution_billing import NEDARIM_CATEGORY
    params = {
        'mosad': mosad,
        'Phone': phone,
        'Groupe': NEDARIM_CATEGORY,
        'GroupeLock': '1',
    }
    return redirect('https://www.matara.pro/nedarimplus/online/?' + urllib.parse.urlencode(params))


NEDARIM_WEBHOOK_IPS = {'18.196.146.117', '18.194.219.73'}


@payment_bp.route('/nedarim/webhook', methods=['POST'])
def nedarim_webhook():
    """Webhook קבוע ברמת המוסד בנדרים פלוס (מוגדר עצמאית אצלם, בתפריט
    'עוד' > 'Webhook', בשדה 'עדכוני עסקאות') - נשלח על **כל** עסקת אשראי
    מוצלחת תחת מספר המוסד שלנו, ולא רק על עסקאות שהוקמו דרך ה-API שלנו עם
    CallBack מפורש (למשל תשלום דרך קישור ה-Redirect הפשוט של
    nedarim_topup_link, או תשלום שמנהל המוסד ביצע ידנית דרך הדשבורד של
    נדרים פלוס עצמם). זה בדיוק מה שהיה חסר כדי שתשלומים "יידעו לאן להגיע".

    לצורך אבטחה/דיוק:
    1. מאמתים שהבקשה מגיעה מאחת מכתובות ה-IP הרשמיות של נדרים פלוס (התיעוד
       מציין רק את שתי הכתובות האלה עבור כל מנגנוני ה-CallBack/Webhook שלהם).
    2. מזכים יתרה **רק** לעסקאות שמתויגות בקטגוריה (Groupe) "תמלול פון" -
       כדי לא לגעת בתשלומים אחרים שאולי מתבצעים תחת אותו מספר מוסד למטרה
       שונה (אם יש כאלה).
    3. זיהוי היעד לזיכוי: קודם לפי מספר טלפון (Institution.phone /
       Customer.phone), ואם לא נמצא - לפי שם בדיוק כפי שמופיע אצל נדרים
       פלוס (ClientName מול Institution.name).
    4. מניעת זיכוי כפול: בודקים אם TransactionId הזה כבר טופל לפני שמזכים.
    """
    from app import db
    from models import Institution, InstitutionChargeLog, Customer, Transaction, ManagerMessage

    ip = _client_ip()
    if ip not in NEDARIM_WEBHOOK_IPS:
        log.warning(f'Nedarim webhook REJECTED - unexpected IP: {ip}')
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    data = request.get_json(force=True, silent=True) or {}
    if not data:
        log.warning(f'Nedarim webhook: empty/unparsable body, raw={request.get_data(as_text=True)[:500]}')
        return jsonify({'ok': True})  # אין מה לעשות עם זה, אבל לא נחזיר שגיאה

    tx_id = str(data.get('TransactionId') or data.get('ID') or '').strip()
    groupe = (data.get('Groupe') or '').strip()
    phone = (data.get('Phone') or '').strip()
    client_name = (data.get('ClientName') or '').strip()
    try:
        amount = float(data.get('Amount') or 0)
    except (TypeError, ValueError):
        amount = 0

    # לוג מלא של כל בקשה תקינה שמגיעה - חיוני לאבחון: אם לא מגיע כלום ללוג
    # הזה אחרי חיוב אמיתי, הבעיה היא לפני שהבקשה בכלל מגיעה לשרת (למשל
    # הכתובת שנשמרה אצל נדרים פלוס שגויה, או שהקוד הזה עדיין לא פרוס בפועל
    # ב-Railway) - לא בלוגיקת ההתאמה/הקטגוריה שבהמשך.
    log.info(f'Nedarim webhook received: tx_id={tx_id}, groupe={groupe!r}, phone={phone!r}, client_name={client_name!r}, amount={amount}')

    from routes.institution_billing import NEDARIM_CATEGORY
    # דרישה נוקשה: מזכים רק עסקה שמתויגת בדיוק בקטגוריה "תמלול פון" - זו
    # הקטגוריה היחידה שהלקוח יכול לבחור אצלכם לפני תשלום, אז זה תקין שהיא
    # תמיד תגיע ממולאת. כל דבר אחר (או ריק) נחסם, כדי לא לגעת בטעות בתשלום
    # שלא שייך למערכת הזו.
    if groupe != NEDARIM_CATEGORY:
        log.info(f'Nedarim webhook: ignoring transaction {tx_id} - Groupe={groupe!r} != {NEDARIM_CATEGORY!r}')
        return jsonify({'ok': True})

    if amount <= 0 or not tx_id:
        log.warning(f'Nedarim webhook: missing amount/tx_id, data={data}')
        return jsonify({'ok': True})

    # מניעת זיכוי כפול - בודקים אם ה-TransactionId הזה כבר נקלט אצלנו
    if InstitutionChargeLog.query.filter_by(provider_ref=tx_id).first():
        log.info(f'Nedarim webhook: transaction {tx_id} already processed (institution), skipping')
        return jsonify({'ok': True})
    if Transaction.query.filter(Transaction.description.contains(f'[נדרים:{tx_id}]')).first():
        log.info(f'Nedarim webhook: transaction {tx_id} already processed (customer), skipping')
        return jsonify({'ok': True})

    # מגוון צורות כתיב אפשריות לאותו מספר (עם/בלי 0 מוביל, עם/בלי קידומת
    # מדינה) - כדי לא לפספס התאמה למספר שכבר קיים אצלנו בפורמט מעט שונה
    # מזה שנדרים פלוס מחזירים בשדה Phone.
    phone_candidates = _phone_match_candidates(phone)
    local_phone = next((c for c in phone_candidates if c.startswith('0')), phone)

    # 1. ניסיון התאמה למוסד - לפי טלפון או שם מדויק כפי שמופיע בנדרים פלוס
    institution = None
    if phone_candidates:
        institution = Institution.query.filter(Institution.phone.in_(phone_candidates)).first()
    if not institution and client_name:
        institution = Institution.query.filter_by(name=client_name).first()

    if institution:
        institution.balance = (institution.balance or 0) + amount
        charge = InstitutionChargeLog(
            institution_id=institution.id, amount=amount, status='success', provider_ref=tx_id,
        )
        db.session.add(charge)
        db.session.commit()
        log.info(f'Nedarim webhook: credited institution {institution.id} +{amount} (tx {tx_id})')
        return jsonify({'ok': True})

    # 2. ניסיון התאמה ללקוח בודד - לפי טלפון. אם אין עדיין לקוח עם הטלפון
    # הזה - יוצרים לו כרטיס לקוח חדש ומזכים אותו (זה בדיוק הרעיון: "כל
    # משתמש במערכת" יכול לטעון יתרה, גם מי שמעולם לא התקשר/נרשם קודם -
    # הטעינה עצמה היא מה שיוצרת לו חשבון).
    customer = None
    if phone_candidates:
        customer = Customer.query.filter(Customer.phone.in_(phone_candidates)).first()
        if not customer:
            customer = Customer(phone=local_phone, name=client_name or None, balance=0.0)
            db.session.add(customer)
            db.session.flush()
            log.info(f'Nedarim webhook: creating new customer for phone={local_phone} (tx {tx_id})')
    if customer:
        bonus = _calculate_bonus_public(amount)
        total_credit = amount + bonus
        customer.balance = (customer.balance or 0) + total_credit
        desc = f'טעינת יתרה דרך נדרים פלוס ₪{amount:.2f}'
        if bonus > 0:
            desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total_credit:.2f}'
        desc += f' [נדרים:{tx_id}]'
        txn = Transaction(customer_id=customer.id, amount=total_credit, type='charge', description=desc)
        db.session.add(txn)
        db.session.commit()
        log.info(f'Nedarim webhook: credited customer {customer.id} +{total_credit} (tx {tx_id})')

        from services.transcribe import process_pending_recordings
        from routes.email_inbound import process_pending_ocr
        import threading, time

        def _delayed_process(customer_id):
            time.sleep(3)
            process_pending_recordings(customer_id)
            process_pending_ocr(customer_id)

        threading.Thread(target=_delayed_process, args=(customer.id,), daemon=True).start()

        if customer.email:
            from routes.admin import get_setting
            threading.Thread(
                target=_issue_receipt_and_send,
                args=(tx_id, customer.email, amount, bonus, total_credit, get_setting),
                daemon=True,
            ).start()
        return jsonify({'ok': True})

    # 3. אין בכלל מספר טלפון בעדכון (ולא נמצא גם מוסד לפי שם) - אין למי
    # לזכות אוטומטית. לא מזכים אף אחד באופן שגוי - במקום זאת פותחים הודעה
    # בתיבת "הודעות למנהל" לטיפול ידני, עם תיוג אדום.
    log.warning(f'Nedarim webhook: unmatched payment, tx={tx_id}, phone={phone}, name={client_name}, amount={amount}')
    try:
        msg = ManagerMessage(
            phone=phone or '—',
            name=client_name or None,
            transcript=f'תשלום בסך ₪{amount:.2f} התקבל בנדרים פלוס (קטגוריה: {NEDARIM_CATEGORY}, מזהה עסקה: {tx_id}) אך לא נמצא מוסד/לקוח עם טלפון או שם תואם. יש לזכות ידנית ולבדוק את פרטי הזיהוי.',
            source='nedarim_unmatched',
        )
        db.session.add(msg)
        db.session.commit()
    except Exception:
        log.exception('Nedarim webhook: failed to create unmatched-payment notice')

    return jsonify({'ok': True})


def _calculate_bonus_public(amount):
    """עטיפה קטנה סביב _calculate_bonus כדי שגם nedarim_webhook יוכל
    להשתמש בה בלי תלות ב-request context שכבר קיים ב-get_setting."""
    from routes.admin import get_setting
    return _calculate_bonus(amount, get_setting)


@payment_bp.route('/nedarim/callback', methods=['GET', 'POST'])
def nedarim_callback():
    """
    נקרא משלוחה 200 אחרי סליקה מוצלחת.
    קורא את קובץ הלוג מימות ומעדכן יתרה.
    """
    from app import db
    from models import Customer, Transaction
    from routes.admin import get_setting
    import requests as req

    log.info(f"Nedarim callback: args={dict(request.args)}")

    # אימות טוקן סודי
    expected_secret = get_setting('payment_callback_secret', '')
    if expected_secret:
        incoming_secret = (
            request.args.get('secret') or
            request.form.get('secret') or ''
        ).strip()
        if incoming_secret != expected_secret:
            log.warning(f"Nedarim callback: invalid secret, rejecting")
            return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # מספר טלפון - שלוחת API שולחת ApiPhone
    phone = (
        request.args.get('ApiPhone') or
        request.form.get('ApiPhone') or
        request.args.get('Phone') or
        request.form.get('Phone') or ''
    ).strip()

    if not phone:
        log.warning("Nedarim callback: no phone found")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # קריאת קובץ הלוג מימות
    yemot_token = get_setting('yemot_token', '')
    yemot_log_path = get_setting('yemot_log_path', 'ivr2:/199/LogCreditCardOK.ini')

    if not yemot_token:
        log.error("Nedarim callback: yemot_token not configured in settings")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    try:
        resp = req.get(
            'https://www.call2all.co.il/ym/api/GetTextFile',
            params={'token': yemot_token, 'what': yemot_log_path},
            timeout=10
        )
        resp.raise_for_status()
        contents = resp.json().get('contents', '')
        log.info(f"Yemot log file read: {len(contents)} chars")
    except Exception as e:
        log.error(f"Failed to read yemot log: {e}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # פענוח הקובץ - פורמט: Field#Value%Field#Value\nField#Value%...
    amount = 0
    approval = ''
    lines = [l.strip() for l in contents.strip().split('\n') if l.strip()]

    # סריקה מהשורה האחרונה - הסליקה האחרונה של הלקוח
    for line in reversed(lines):
        fields = {}
        for part in line.split('%'):
            if '#' in part:
                k, _, v = part.partition('#')
                fields[k.strip()] = v.strip()

        line_phone = fields.get('Phone', '')
        line_status = fields.get('Status', '')
        line_amount = fields.get('BillingSum', '0')
        line_approval = fields.get('DealSuccessfully', '')

        if line_phone == phone and line_status == 'OK':
            try:
                amount = float(line_amount)
                approval = line_approval
                log.info(f"Found transaction: phone={phone}, amount={amount}, approval={approval}")
            except ValueError:
                pass
            break

    if amount <= 0:
        log.warning(f"No valid amount found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    customer = Customer.query.filter_by(phone=phone).first()
    if not customer:
        log.warning(f"Customer not found for phone={phone}")
        return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # בדיקת כפילות לפי מספר אישור
    if approval:
        existing = Transaction.query.filter(
            Transaction.customer_id == customer.id,
            Transaction.description.contains(f'אישור: {approval}')
        ).first()
        if existing:
            log.warning(f"Transaction {approval} already processed, skipping")
            return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}

    # חישוב בונוס
    bonus = _calculate_bonus(amount, get_setting)
    total_credit = amount + bonus

    customer.balance += total_credit

    desc = f'טעינת ארנק ₪{amount:.2f}'
    if bonus > 0:
        desc += f' + בונוס ₪{bonus:.2f} = סה"כ ₪{total_credit:.2f}'
    if approval:
        desc += f' | אישור: {approval}'

    txn = Transaction(
        customer_id=customer.id,
        amount=total_credit,
        type='charge',
        description=desc,
    )
    db.session.add(txn)
    db.session.commit()

    log.info(f"Payment OK: customer={customer.id}, +{total_credit}, balance={customer.balance}")

    # הפעל הקלטות ממתינות ב-thread נפרד — המתן 3 שניות כדי לוודא שהטעינה נרשמה ב-DB
    from services.transcribe import process_pending_recordings
    from routes.email_inbound import process_pending_ocr
    import threading, time

    def _delayed_process(customer_id):
        time.sleep(3)
        process_pending_recordings(customer_id)
        process_pending_ocr(customer_id)

    t = threading.Thread(target=_delayed_process, args=(customer.id,), daemon=True)
    t.start()

    # הפקת קבלה ושליחה למייל - ב-thread נפרד כדי לא לעכב את ימות
    if approval and customer.email:
        import threading
        t = threading.Thread(
            target=_issue_receipt_and_send,
            args=(approval, customer.email, amount, bonus, total_credit, get_setting),
            daemon=True
        )
        t.start()

    return 'go_to_folder=/', 200, {'Content-Type': 'text/plain; charset=utf-8'}


def _issue_receipt_and_send(approval, customer_email, amount, bonus, total_credit, get_setting):
    """מפיק קבלה מנדרים פלוס ושולח למייל הלקוח."""
    from app import app
    with app.app_context():
        _issue_receipt_and_send_inner(approval, customer_email, amount, bonus, total_credit, get_setting)


def _issue_receipt_and_send_inner(approval, customer_email, amount, bonus, total_credit, get_setting):
    """הלוגיקה הפנימית - רצה בתוך app_context."""
    import requests as req

    mosad = get_setting('nedarim_mosad_number', '')
    api_password = get_setting('nedarim_api_password', '')
    tamal_type = get_setting('nedarim_tamal_type', '400')

    if not mosad or not api_password:
        log.warning("Receipt: nedarim_mosad_number or nedarim_api_password not configured, sending email without receipt link")
        _send_receipt_email(
            to=customer_email,
            amount=amount,
            bonus=bonus,
            total_credit=total_credit,
            approval=approval,
            receipt_url=None,
        )
        return

    # שלב 1: הפקת קבלה
    try:
        resp = req.post(
            'https://matara.pro/nedarimplus/Reports/Tamal3.aspx',
            data={
                'Action': 'TamalCreate',
                'MosadNumber': mosad,
                'ApiPassword': api_password,
                'TransactionId': approval,
                'TamalType': tamal_type,
            },
            timeout=15
        )
        result = resp.text.strip()
        log.info(f"Receipt creation: TransactionId={approval}, result={result}")
        if result != 'OK':
            log.error(f"Receipt creation failed: {result}")
            return
    except Exception as e:
        log.error(f"Receipt creation error: {e}")
        return

    # שלב 2: קבלת קישור לקבלה
    receipt_url = None
    try:
        resp = req.post(
            'https://matara.pro/nedarimplus/Reports/Tamal3.aspx',
            data={
                'Action': 'ShowInvoice',
                'MosadNumber': mosad,
                'ApiPassword': api_password,
                'TransactionId': approval,
            },
            timeout=15
        )
        data = resp.json()
        if data.get('Result') == 'OK':
            receipt_url = data.get('Message', '')
            log.info(f"Receipt URL: {receipt_url}")
        else:
            log.error(f"ShowInvoice failed: {data}")
    except Exception as e:
        log.error(f"ShowInvoice error: {e}")

    # שלב 3: שליחת מייל ללקוח
    _send_receipt_email(
        to=customer_email,
        amount=amount,
        bonus=bonus,
        total_credit=total_credit,
        approval=approval,
        receipt_url=receipt_url,
    )


def _send_receipt_email(to, amount, bonus, total_credit, approval, receipt_url):
    """שולח מייל אישור טעינה עם קישור לקבלה."""
    try:
        import os
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email

        bonus_row = ''
        if bonus > 0:
            bonus_row = f'<tr><td style="padding:6px 0;color:#6b7280">בונוס מבצע</td><td style="padding:6px 0;text-align:left;color:#10b981"><b>+₪{bonus:.2f}</b></td></tr>'

        receipt_btn = ''
        if receipt_url:
            receipt_btn = f'''
<div style="text-align:center;margin:20px 0">
  <a href="{receipt_url}" style="background:#1d4ed8;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700">
    📄 הצגת קבלה רשמית
  </a>
</div>'''

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:auto;background:#f9fafb;padding:24px;border-radius:12px">
<div style="background:#fff;border-radius:10px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,0.08)">
  <h2 style="color:#1d4ed8;margin:0 0 16px">✅ הארנק נטען בהצלחה</h2>
  <table style="width:100%;border-top:1px solid #e5e7eb;margin-bottom:8px">
    <tr><td style="padding:6px 0;color:#6b7280">סכום שנסלק</td><td style="padding:6px 0;text-align:left"><b>₪{amount:.2f}</b></td></tr>
    {bonus_row}
    <tr style="border-top:1px solid #e5e7eb"><td style="padding:8px 0;font-weight:700">סה"כ זוכה לארנק</td><td style="padding:8px 0;text-align:left;font-weight:700;color:#1d4ed8">₪{total_credit:.2f}</td></tr>
  </table>
  <div style="font-size:12px;color:#9ca3af;margin-bottom:16px">מספר אישור: {approval}</div>
  {receipt_btn}
</div>
<div style="text-align:center;font-size:11px;color:#9ca3af;margin-top:12px">תמלול פון 03-3131795</div>
</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
            to_emails=to,
            subject=f'הארנק נטען - ₪{total_credit:.2f} | תמלול פון',
            html_content=html
        )
        sg.send(message)
        log.info(f"Receipt email sent to {to}")
    except Exception as e:
        log.error(f"Receipt email error: {e}")


def _calculate_bonus(amount, get_setting):
    bonus = 0.0
    for i in range(1, 6):
        threshold_str = get_setting(f'bonus_threshold_{i}', '')
        bonus_str = get_setting(f'bonus_amount_{i}', '')
        if not threshold_str or not bonus_str:
            break
        try:
            threshold = float(threshold_str)
            bonus_amount = float(bonus_str)
            if amount >= threshold:
                bonus = bonus_amount
        except ValueError:
            continue
    return bonus


@payment_bp.route('/pending', methods=['POST'])
def save_pending():
    from routes.admin import set_setting
    data = request.json or {}
    phone = data.get('phone', '').strip()
    amount = float(data.get('amount', 0))
    if not phone or amount <= 0:
        return jsonify({'status': 'error'}), 400
    set_setting(f'pending_payment_{phone}', str(amount))
    log.info(f"Pending payment saved: phone={phone}, amount={amount}")
    return jsonify({'status': 'ok'})
