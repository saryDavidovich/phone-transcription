from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from app import db, login_manager
from models import Customer, Recording, Transaction, Settings, AdminUser
from datetime import datetime, timedelta
from sqlalchemy import func

admin_bp = Blueprint('admin', __name__)

@login_manager.user_loader
def load_user(user_id):
    return AdminUser.query.get(int(user_id))

def get_setting(key, default=''):
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = Settings.query.filter_by(key=key).first()
    if s:
        s.value = value
        s.updated_at = datetime.utcnow()
    else:
        s = Settings(key=key, value=value)
        db.session.add(s)
    db.session.commit()

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = AdminUser.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('admin.dashboard'))
        flash('שם משתמש או סיסמה שגויים')
    return render_template('admin/login.html')

@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('admin.login'))

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def dashboard():
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)

    stats = {
        'total_customers': Customer.query.count(),
        'active_customers': Customer.query.filter_by(is_blocked=False).count(),
        'blocked_customers': Customer.query.filter_by(is_blocked=True).count(),
        'total_recordings': Recording.query.count(),
        'today_recordings': Recording.query.filter(
            func.date(Recording.created_at) == today
        ).count(),
        'month_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge',
            Transaction.created_at >= month_start
        ).scalar() or 0,
        'total_revenue': db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge'
        ).scalar() or 0,
        'total_balance': db.session.query(func.sum(Customer.balance)).scalar() or 0,
    }

    recent_recordings = Recording.query.order_by(Recording.created_at.desc()).limit(10).all()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()

    return render_template('admin/dashboard.html',
        stats=stats,
        recent_recordings=recent_recordings,
        recent_transactions=recent_transactions
    )

@admin_bp.route('/customers')
@login_required
def customers():
    search = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    query = Customer.query
    if search:
        query = query.filter(
            Customer.phone.contains(search) |
            Customer.name.contains(search) |
            Customer.email.contains(search)
        )
    customers = query.order_by(Customer.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/customers.html', customers=customers, search=search)

@admin_bp.route('/customers/<int:id>')
@login_required
def customer_detail(id):
    customer = Customer.query.get_or_404(id)
    recordings = Recording.query.filter_by(customer_id=id).order_by(Recording.created_at.desc()).all()
    transactions = Transaction.query.filter_by(customer_id=id).order_by(Transaction.created_at.desc()).all()
    return render_template('admin/customer_detail.html',
        customer=customer, recordings=recordings, transactions=transactions)

@admin_bp.route('/customers/<int:id>/block', methods=['POST'])
@login_required
def block_customer(id):
    customer = Customer.query.get_or_404(id)
    customer.is_blocked = not customer.is_blocked
    db.session.commit()
    status = 'נחסם' if customer.is_blocked else 'בוטל חסם'
    flash(f'לקוח {status} בהצלחה')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/credit', methods=['POST'])
@login_required
def credit_customer(id):
    customer = Customer.query.get_or_404(id)
    amount = float(request.form.get('amount', 0))
    reason = request.form.get('reason', 'זיכוי ידני')
    if amount > 0:
        customer.balance += amount
        txn = Transaction(
            customer_id=id,
            amount=amount,
            type='credit',
            description=reason
        )
        db.session.add(txn)
        db.session.commit()
        flash(f'לקוח זוכה ב-{amount:.2f} ₪')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/customers/<int:id>/update', methods=['POST'])
@login_required
def update_customer(id):
    customer = Customer.query.get_or_404(id)
    customer.name = request.form.get('name', customer.name)
    customer.email = request.form.get('email', customer.email)
    customer.fax = request.form.get('fax', customer.fax)
    db.session.commit()
    flash('פרטי לקוח עודכנו')
    return redirect(url_for('admin.customer_detail', id=id))

@admin_bp.route('/recordings')
@login_required
def recordings():
    page = request.args.get('page', 1, type=int)
    recordings = Recording.query.order_by(Recording.created_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/recordings.html', recordings=recordings)

@admin_bp.route('/recordings/<int:id>')
@login_required
def recording_detail(id):
    recording = Recording.query.get_or_404(id)
    return render_template('admin/recording_detail.html', recording=recording)

@admin_bp.route('/reports')
@login_required
def reports():
    # Monthly revenue
    monthly = db.session.query(
        func.strftime('%Y-%m', Transaction.created_at).label('month'),
        func.sum(Transaction.amount).label('revenue')
    ).filter(Transaction.type == 'charge').group_by('month').order_by('month').all()

    # Top customers by spend
    top_customers = db.session.query(
        Customer,
        func.sum(Transaction.amount).label('total_spend')
    ).join(Transaction).filter(
        Transaction.type == 'debit'
    ).group_by(Customer.id).order_by(func.sum(Transaction.amount).desc()).limit(10).all()

    return render_template('admin/reports.html',
        monthly=monthly, top_customers=top_customers)

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        set_setting('price_per_30min', request.form.get('price_per_30min', '5'))
        set_setting('min_balance', request.form.get('min_balance', '5'))
        set_setting('max_recording_seconds', request.form.get('max_recording_seconds', '1800'))
        set_setting('welcome_new', request.form.get('welcome_new', ''))
        set_setting('welcome_returning', request.form.get('welcome_returning', ''))
        set_setting('system_explanation', request.form.get('system_explanation', ''))
        flash('הגדרות נשמרו בהצלחה')
        return redirect(url_for('admin.settings'))

    current_settings = {
        'price_per_30min': get_setting('price_per_30min', '5'),
        'min_balance': get_setting('min_balance', '5'),
        'max_recording_seconds': get_setting('max_recording_seconds', '1800'),
        'welcome_new': get_setting('welcome_new', 'שלום וברוכים הבאים למערכת התמלול.'),
        'welcome_returning': get_setting('welcome_returning', 'שלום וברוכים השבים.'),
        'system_explanation': get_setting('system_explanation', 'מערכת התמלול מאפשרת לך להקליט הודעות שיתומללו ויישלחו אליך למייל או לפקס.'),
    }
    return render_template('admin/settings.html', settings=current_settings)

@admin_bp.route('/api/stats')
@login_required
def api_stats():
    """JSON stats for dashboard charts"""
    last_30 = []
    for i in range(29, -1, -1):
        day = datetime.utcnow().date() - timedelta(days=i)
        revenue = db.session.query(func.sum(Transaction.amount)).filter(
            Transaction.type == 'charge',
            func.date(Transaction.created_at) == day
        ).scalar() or 0
        recordings = Recording.query.filter(
            func.date(Recording.created_at) == day
        ).count()
        last_30.append({'date': str(day), 'revenue': float(revenue), 'recordings': recordings})
    return jsonify(last_30)
