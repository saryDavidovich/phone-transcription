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
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
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
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
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
    transcript = db.Column(db.Text)  # תמלול אוטומטי (בלי חשיבה) של ההודעה, מתמלא ברקע אחרי השמירה
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
    status = db.Column(db.String(20), default='completed')  # completed / error / pending_payment
    delivered_to = db.Column(db.String(255), nullable=True)  # כתובת מייל לשליחה כשתשלים תשלום
    expires_at = db.Column(db.DateTime, nullable=True)  # לתוצאות pending_payment - 72 שעות
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref=db.backref('ocr_results', lazy=True))


class ProcessedWebhook(db.Model):
    """מונע עיבוד כפול כאשר SendGrid שולח את אותו webhook יותר מפעם אחת (retry)"""
    __tablename__ = 'processed_webhooks'

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ConversationThread(db.Model):
    """שיחה בודדת (thread) בהתכתבות מייל עם לקוח. ללקוח יכולות להיות כמה שיחות
    נפרדות במקביל - כל אחת מוצגת בנפרד בממשק, לא מעורבבת יחד."""
    __tablename__ = 'conversation_threads'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship('Customer', backref='threads')


class CustomerMessage(db.Model):
    """הודעה בודדת בתוך שיחה (ConversationThread). direction='out' - המנהל
    שלח, direction='in' - תגובת הלקוח שהתקבלה במייל. is_read מסמן הודעות
    נכנסות שהמנהל עוד לא צפה בהן (לצורך התראה בעמוד הודעות למנהל)."""
    __tablename__ = 'customer_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('conversation_threads.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # 'out' | 'in'
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=True)
    message_id = db.Column(db.String(255))  # Message-ID שקבענו לעצמנו, לשרשור אמיתי (In-Reply-To/References)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship('Customer', backref='messages')
    thread = db.relationship('ConversationThread', backref=db.backref('messages', order_by='CustomerMessage.created_at'))


class CallLog(db.Model):
    """רישום כל שיחה נכנסת (התחלה/סיום) - לצורך דוחות פילוח שיחות לפי תאריך.
    נכתב ע"י שירות ה-IVR (yemot-router2) דרך /api/call/start ו-/api/call/end.
    started_at/ended_at נשמרים ב-UTC; ההמרה לשעון ישראל נעשית בזמן קריאה בלבד."""
    __tablename__ = 'call_logs'

    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False, index=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)


class GeneralInboxMessage(db.Model):
    """שרשור פנייה מגורם שלא זוהה כלקוח רשום (לא נמצא מספר טלפון בנושא שתואם
    ללקוח קיים) - מקביל ל-ConversationThread, אבל מזוהה לפי כתובת מייל בלבד
    (אין customer_id) כדי שגם מי שעדיין לא לקוח רשום יוכל לקבל תשובה במייל
    עם שרשור אמיתי (Message-ID/In-Reply-To), בדיוק כמו שיחה עם לקוח. is_read
    מסמן אם יש בשרשור הזה תוכן חדש שהמנהל עוד לא צפה בו."""
    __tablename__ = 'general_inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    from_email = db.Column(db.String(255), nullable=False, index=True)
    subject = db.Column(db.String(500))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class InboxMessage(db.Model):
    """הודעה בודדת בתוך שרשור GeneralInboxMessage. direction='in' - מהשולח
    החיצוני, direction='out' - תגובת המנהל. אותו עיקרון בדיוק כמו
    CustomerMessage, רק ששייכת לשרשור אנונימי (לפי מייל) ולא ללקוח רשום."""
    __tablename__ = 'inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('general_inbox_messages.id'), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # 'out' | 'in'
    body = db.Column(db.Text, nullable=False)
    message_id = db.Column(db.String(255))  # רק להודעות 'out' - לשרשור אמיתי מול תגובות עתידיות
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    thread = db.relationship('GeneralInboxMessage', backref=db.backref('messages', order_by='InboxMessage.created_at'))
