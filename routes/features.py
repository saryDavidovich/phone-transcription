"""
API לשרת ה-IVR עבור מיילים לאחרים (שלוחה 1 ← 6) ומשימות (שלוחה 1 ← 5),
"""
from flask import Blueprint, request, jsonify

from app import db
from services.customer_phones import find_customer_by_phone
from services import customer_mail as CM
from services import tasks as TK

features_api = Blueprint('features_api', __name__)


def _customer():
    data = request.get_json(silent=True) or {}
    phone = (data.get('phone') or request.args.get('phone') or '').strip()
    return find_customer_by_phone(phone), data, phone


# ---------------------------------------------------------------------------
# מיילים לאחרים
# ---------------------------------------------------------------------------
@features_api.route('/email-out/menu', methods=['GET'])
def email_out_menu():
    customer, _d, _p = _customer()
    if not customer:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'enabled': CM.enabled(), 'unit_price': CM.unit_price(),
                    'contacts': [{'id': c.id, 'name': c.name or c.email, 'email': c.email} for c in CM.contacts(customer)],
                    'replies': len(CM.new_replies(customer)), 'replies_total': len(CM.recent_replies(customer)),
                    'has_email': bool(customer.email)})


@features_api.route('/email-out/contact', methods=['POST'])
def email_out_add_contact():
    customer, data, _p = _customer()
    if not customer:
        return jsonify({'ok': False, 'error': 'customer'})
    c, err = CM.add_contact(customer, data.get('email'), data.get('name', ''))
    return jsonify({'ok': bool(c), 'error': err, 'id': c.id if c else None})


@features_api.route('/email-out/contact/update', methods=['POST'])
def email_out_update_contact():
    customer, data, _p = _customer()
    if not customer:
        return jsonify({'ok': False, 'error': 'customer'})
    c, err = CM.update_contact(customer, data.get('id'), name=data.get('name') or None,
                               email=data.get('email') if data.get('email') else None)
    return jsonify({'ok': bool(c), 'error': err})


@features_api.route('/email-out/contact/delete', methods=['POST'])
def email_out_delete_contact():
    customer, data, _p = _customer()
    return jsonify({'ok': bool(customer) and CM.delete_contact(customer, int(data.get('id') or 0))})


@features_api.route('/email-out/contact-name', methods=['POST'])
def email_out_contact_name():
    """שם איש קשר מהקלטה (Gemini) - בטלפון אומרים את השם."""
    import os, requests as req
    data = request.get_json(silent=True) or {}
    if not CM.is_yemot_url(data.get('rec_url', '')):
        return jsonify({'name': '', 'error': 'recording'})
    try:
        from google import genai
        from google.genai import types as gtypes
        r = req.get(data.get('rec_url', ''), timeout=30)
        r.raise_for_status()
        client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))
        res = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=['בהקלטה אדם אומר שם של איש קשר (שם של אדם או של מקום/ארגון). החזר רק את השם כפי שנאמר, '
                      'בעברית (או באנגלית אם נאמר באנגלית), בלי שום מילה נוספת. אם לא נשמע שם - החזר טקסט ריק.',
                      gtypes.Part.from_bytes(data=r.content, mime_type='audio/wav')],
            config=gtypes.GenerateContentConfig(thinking_config=gtypes.ThinkingConfig(thinking_budget=0)))
        name = (res.text or '').strip().strip('."\'')[:120]
    except Exception as e:
        return jsonify({'name': '', 'error': str(e)[:200]})
    return jsonify({'name': name})


@features_api.route('/email-out/send', methods=['POST'])
def email_out_send():
    _c, data, phone = _customer()
    return jsonify(CM.send_from_phone(phone, data.get('rec_url', ''), data.get('call_id', ''),
                                      contact_id=data.get('contact_id'), email=data.get('email'),
                                      reply_id=data.get('reply_id')))


@features_api.route('/email-out/replies', methods=['GET'])
def email_out_replies():
    customer, _d, _p = _customer()
    return jsonify({'replies': CM.recent_replies(customer) if customer else []})


@features_api.route('/email-out/replies/heard', methods=['POST'])
def email_out_replies_heard():
    customer, data, _p = _customer()
    if customer:
        CM.mark_replies_heard(customer, [int(x) for x in (data.get('ids') or []) if str(x).isdigit()])
    return jsonify({'ok': True})


@features_api.route('/email-out/contacts-link', methods=['POST'])
def email_out_contacts_link():
    """שולח ללקוח במייל את רשימת אנשי הקשר + הסבר איך מוסיפים ומוחקים במייל."""
    customer, _d, _p = _customer()
    if not customer or not customer.email:
        return jsonify({'ok': False, 'error': 'no_email'})
    CM.send_contacts_summary(customer, customer.email)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# משימות
# ---------------------------------------------------------------------------
@features_api.route('/tasks/menu', methods=['GET'])
def tasks_menu():
    customer, _d, _p = _customer()
    if not customer:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'enabled': TK.enabled(), 'unit_price': TK.unit_price(), 'fax_price': TK.fax_price(),
                    'tasks': [{'id': t.id, 'text': (t.text or '')[:200], 'when': TK.when_text(t.due_at, t.recurrence),
                               'channel': t.channel} for t in TK.active_tasks(customer)]})


@features_api.route('/tasks/create', methods=['POST'])
def tasks_create():
    _c, data, phone = _customer()
    return jsonify(TK.create_from_phone(phone, data.get('rec_url', ''), data.get('call_id', '')))


@features_api.route('/tasks/confirm', methods=['POST'])
def tasks_confirm():
    _c, data, phone = _customer()
    return jsonify(TK.confirm(phone, data.get('task_id'), data.get('channel')))


@features_api.route('/tasks/discard', methods=['POST'])
def tasks_discard():
    _c, data, phone = _customer()
    return jsonify(TK.discard(phone, data.get('task_id')))


@features_api.route('/tasks/cancel', methods=['POST'])
def tasks_cancel():
    customer, data, _p = _customer()
    return jsonify({'ok': bool(customer) and TK.cancel(customer, data.get('task_id'))})


# ---------------------------------------------------------------------------
# העלאת קוד לגיטהאב -> מצב תחזוקה עד שהגרסה החדשה עולה (services/maintenance.py)
# ---------------------------------------------------------------------------
@features_api.route('/github-push', methods=['POST'])
def github_push():
    import hashlib
    import hmac
    import os
    secret = os.environ.get('GITHUB_WEBHOOK_SECRET', '')
    if not secret:
        return jsonify({'ok': False, 'error': 'GITHUB_WEBHOOK_SECRET לא מוגדר'}), 503
    sig = request.headers.get('X-Hub-Signature-256', '')
    want = 'sha256=' + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return jsonify({'ok': False, 'error': 'bad signature'}), 401
    event = request.headers.get('X-GitHub-Event', '')
    if event == 'ping':
        return jsonify({'ok': True, 'pong': True})
    data = request.get_json(silent=True) or {}
    branch = os.environ.get('GITHUB_DEPLOY_BRANCH', 'main')
    if event != 'push' or data.get('ref') != f'refs/heads/{branch}':
        return jsonify({'ok': True, 'ignored': True})
    from services import maintenance as MT
    return jsonify({'ok': True, 'maintenance_on': MT.on_github_push()})
