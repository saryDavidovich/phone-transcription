"""דפי אישור/דחייה של בקשת שיוך לקוח קיים כתלמיד (ציבורי - בקישור מהמייל) +
דף אישור ידני למנהל כשללקוח אין מייל. ראה services/student_links.py."""
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from app import db
from models import StudentLinkRequest
from services import student_links as sl

student_link_bp = Blueprint('student_link', __name__)


@student_link_bp.context_processor
def _inject_shared_admin_context():
    # templates/admin/base.html צריך new_messages_count (ראה אותו דבר ב-routes/dictate.py)
    from routes.admin import inject_new_messages_count
    return inject_new_messages_count()


def _req_or_404(token):
    from flask import abort
    req = StudentLinkRequest.query.filter_by(token=token).first()
    if not req:
        abort(404)
    return req


def _state(req):
    if req.status == 'pending' and req.expires_at and req.expires_at < datetime.utcnow():
        return 'expired'
    return req.status


@student_link_bp.route('/student-link/<token>', methods=['GET'])
def view(token):
    req = _req_or_404(token)
    # בכוונה GET רק מציג - האישור עצמו ב-POST, כדי שסורקי מייל/תצוגה מקדימה לא יאשרו בטעות
    return render_template('student_link.html', req=req, state=_state(req), result=None)


@student_link_bp.route('/student-link/<token>', methods=['POST'])
def respond(token):
    req = _req_or_404(token)
    action = request.form.get('action')
    if action == 'approve':
        ok, msg = sl.approve_link(req)
    elif action == 'decline':
        ok, msg = sl.decline_link(req)
    else:
        ok, msg = False, 'פעולה לא ידועה'
    return render_template('student_link.html', req=req, state=_state(req), result=(ok, msg))


# --- אישור ידני ע"י מנהל המערכת (ללקוח בלי מייל) ---
@student_link_bp.route('/admin/student-links')
@login_required
def admin_list():
    rows = (StudentLinkRequest.query.filter_by(status='pending', needs_admin=True)
            .order_by(StudentLinkRequest.created_at.asc()).all())
    return render_template('admin/student_links.html', rows=rows)


@student_link_bp.route('/admin/student-links/<int:req_id>/<action>', methods=['POST'])
@login_required
def admin_respond(req_id, action):
    req = StudentLinkRequest.query.get_or_404(req_id)
    if action == 'approve':
        ok, msg = sl.approve_link(req, by_admin=True)
    elif action == 'decline':
        ok, msg = sl.decline_link(req)
    else:
        ok, msg = False, 'פעולה לא ידועה'
    flash(msg)
    return redirect(url_for('student_link.admin_list'))
