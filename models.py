from app import db
from flask_login import UserMixin
from datetime import datetime

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(200))
    fax = db.Column(db.String(20))
    balance = db.Column(db.Float, default=0.0)
    is_blocked = db.Column(db.Boolean, default=False)
    delivery_method = db.Column(db.String(10), default='email')
    default_settings = db.Column(db.JSON, nullable=True)  # ברירות מחדל: tier, language, output_language
    transcription_tier = db.Column(db.String(10), default='basic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    recordings = db.relationship('Recording', backref='customer', lazy=True)
    transactions = db.relationship('Transaction', backref='customer', lazy=True)

class Recording(db.Model):
    __tablename__ = 'recordings'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    call_id = db.Column(db.String(100), unique=True)
    duration_seconds = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    transcript = db.Column(db.Text)
    summary = db.Column(db.Text)
    status = db.Column(db.String(30), default='recording')
    delivery_method = db.Column(db.String(10))
    delivered_to = db.Column(db.String(200))
    alefbot_job_id = db.Column(db.String(100))
    rec_url = db.Column(db.String(500))
    source_filename = db.Column(db.String(255), nullable=True)
    fax_campaign_id = db.Column(db.String(64), nullable=True, index=True)
    fax_status = db.Column(db.String(32), nullable=True)
    fax_status_note = db.Column(db.Text, nullable=True)
    # הקלטות ממתינות לתשלום
    transcription_tier = db.Column(db.String(10), nullable=True)
    language = db.Column(db.String(10), nullable=True)
    output_language = db.Column(db.String(10), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)  # תפוגה אחרי 72 שעות
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20))
    description = db.Column(db.String(200))
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AdminUser(db.Model, UserMixin):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class CallSession(db.Model):
    __tablename__ = 'call_sessions'
    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    step = db.Column(db.String(50), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ManagerMessage(db.Model):
    __tablename__ = 'manager_messages'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(200))
    fax = db.Column(db.String(20))
    delivery_method = db.Column(db.String(10))
    call_id = db.Column(db.String(100))
    rec_url = db.Column(db.String(500))
    status = db.Column(db.String(30), default='new')
    admin_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OcrResult(db.Model):
    __tablename__ = 'ocr_results'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(255))
    original_file_path = db.Column(db.String(512))  # נתיב לקובץ המקורי
    ocr_text = db.Column(db.Text)
    char_count = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    engine = db.Column(db.String(20), default='gemini')
    status = db.Column(db.String(20), default='completed')  # completed / error
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref=db.backref('ocr_results', lazy=True))
