"""
API לשרת ה-IVR עבור מיילים לאחרים (שלוחה 1 ← 6) ומשימות (שלוחה 1 ← 5),
ודף אנשי הקשר האישי של הלקוח (/c/<קישור חתום>).
"""
from flask import Blueprint, request, jsonify, render_template_string, redirect

from app import db
from services.customer_phones import find_customer_by_phone
from services import customer_mail as CM
from services import tasks as TK

features_api = Blueprint('features_api', __name__)
contacts_page_bp = Blueprint('contacts_page', __name__)


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
                    'replies': len(CM.new_replies(customer)), 'has_email': bool(customer.email)})


@features_api.route('/email-out/contact', methods=['POST'])
def email_out_add_contact():
    customer, data, _p = _customer()
    if not customer:
        return jsonify({'ok': False, 'error': 'customer'})
    c, err = CM.add_contact(customer, data.get('email'), data.get('name', ''))
    return jsonify({'ok': bool(c), 'error': err, 'id': c.id if c else None})


@features_api.route('/email-out/send', methods=['POST'])
def email_out_send():
    _c, data, phone = _customer()
    return jsonify(CM.send_from_phone(phone, data.get('rec_url', ''), data.get('call_id', ''),
                                      contact_id=data.get('contact_id'), email=data.get('email')))


@features_api.route('/email-out/replies', methods=['GET'])
def email_out_replies():
    customer, _d, _p = _customer()
    return jsonify({'replies': CM.new_replies(customer) if customer else []})


@features_api.route('/email-out/replies/heard', methods=['POST'])
def email_out_replies_heard():
    customer, data, _p = _customer()
    if customer:
        CM.mark_replies_heard(customer, [int(x) for x in (data.get('ids') or []) if str(x).isdigit()])
    return jsonify({'ok': True})


@features_api.route('/email-out/contacts-link', methods=['POST'])
def email_out_contacts_link():
    """שולח ללקוח במייל את רשימת אנשי הקשר + קישור לדף הניהול."""
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
# דף אנשי הקשר האישי של הלקוח
# ---------------------------------------------------------------------------
_PAGE = '''<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>אנשי הקשר שלי</title>
<style>
body{margin:0;background:#f3f4f6;font-family:Arial,sans-serif;color:#111827}
.w{max-width:640px;margin:0 auto;padding:20px 16px}
h1{font-size:22px;margin:0 0 4px;color:#1d4ed8}.sub{color:#6b7280;font-size:14px;margin:0 0 18px;line-height:1.6}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}td{padding:9px 6px;border-top:1px solid #f1f5f9;font-size:15px;vertical-align:middle}
td.n{color:#6b7280;width:34px}td.e{direction:ltr;text-align:left;color:#374151;word-break:break-all}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #d1d5db;border-radius:8px;font-size:15px;margin-bottom:8px}
input[type=email]{direction:ltr;text-align:left}
button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;font-weight:700;cursor:pointer}
button.del{background:#fee2e2;color:#b91c1c;padding:6px 12px;font-size:13px}
.msg{padding:10px 12px;border-radius:8px;margin-bottom:14px;font-size:14px}.ok{background:#dcfce7;color:#166534}.err{background:#fee2e2;color:#991b1b}
</style></head><body><div class="w">
<h1>אנשי הקשר שלי</h1>
<p class="sub">אנשי הקשר לשליחת מיילים מהטלפון (שלוחה 1 ואז 6). בטלפון הם מוקראים לפי המספרים שכאן.</p>
{% if msg %}<div class="msg {{ 'err' if err else 'ok' }}">{{ msg }}</div>{% endif %}
<div class="card"><form method="post"><input type="hidden" name="act" value="add">
<input name="name" placeholder="שם (כך הוא יוקרא בטלפון)" maxlength="120">
<input name="email" type="email" placeholder="כתובת מייל" required autocapitalize="off" spellcheck="false">
<button type="submit">➕ הוספה</button></form></div>
<div class="card">{% if contacts %}<table>{% for c in contacts %}<tr><td class="n">{{ loop.index }}</td><td>{{ c.name }}</td>
<td class="e">{{ c.email }}</td><td style="width:70px"><form method="post" onsubmit="return confirm('למחוק את {{ c.name }}?')">
<input type="hidden" name="act" value="del"><input type="hidden" name="id" value="{{ c.id }}"><button class="del">מחיקה</button></form></td></tr>{% endfor %}</table>
{% else %}<p style="color:#6b7280;margin:0">עדיין אין אנשי קשר.</p>{% endif %}</div>
<p class="sub">מערכת תמלול פון 03-3131795</p></div></body></html>'''


@contacts_page_bp.route('/c/<token>', methods=['GET', 'POST'])
def contacts_page(token):
    customer = CM.customer_from_token(token)
    if not customer:
        return 'הקישור לא תקין', 404
    msg, err = request.args.get('m', ''), request.args.get('e') == '1'
    if request.method == 'POST':
        if request.form.get('act') == 'add':
            c, e = CM.add_contact(customer, request.form.get('email'), request.form.get('name', ''))
            return redirect(f'/c/{token}?m=' + ('נשמר' if c else e) + ('' if c else '&e=1'), 303)
        if request.form.get('act') == 'del' and str(request.form.get('id', '')).isdigit():
            CM.delete_contact(customer, int(request.form['id']))
            return redirect(f'/c/{token}?m=נמחק', 303)
    return render_template_string(_PAGE, contacts=CM.contacts(customer), msg=msg, err=err)
