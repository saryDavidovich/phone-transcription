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
    delivery_method = db.Column(db.String(10), default='email')  # email/fax
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20))  # charge/debit/credit/refund
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
