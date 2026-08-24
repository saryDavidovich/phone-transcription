import os
import logging
from flask import Flask, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix
db = SQLAlchemy()
login_manager = LoginManager()
def create_app():
    app = Flask(__name__)

    # האפליקציה רצה מאחורי Reverse Proxy (Railway/וכו') שמסיים את ה-HTTPS
    # ומדבר עם התהליך שלנו פנימית ב-HTTP רגיל. בלי ProxyFix, כל דבר שמסתמך
    # על הכתובת הנוכחית (request.url_root, url_for(..., _external=True)) -
    # למשל כתובת ה-CallBack שנשלחת לנדרים פלוס, או ה-redirect_uri של כניסת
    # Google - "יחשוב" בטעות שהאתר רץ ב-http:// ולא ב-https://. נדרים פלוס
    # דורשים https לכתובת ה-Webhook/CallBack שלהם - כתובת http שגויה בשקט
    # היא הסבר סביר מאוד לכך שהם "לא יודעים לאן לשלוח" עדכון תשלום.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this')
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///transcription.db')
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 1800,   # 30 דקות — מספיק לתמלולים ארוכים
    'pool_timeout': 30,
    'connect_args': {'connect_timeout': 10},
}
    
    app.config['PRICE_PER_30MIN'] = float(os.environ.get('PRICE_PER_30MIN', '5.0'))
    app.config['MIN_BALANCE'] = float(os.environ.get('MIN_BALANCE', '5.0'))
    
    app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY', '')
    app.config['GMAIL_USER'] = os.environ.get('GMAIL_USER', '')
    app.config['GMAIL_APP_PASSWORD'] = os.environ.get('GMAIL_APP_PASSWORD', '')
    app.config['CARDCOM_TERMINAL'] = os.environ.get('CARDCOM_TERMINAL', '')
    app.config['CARDCOM_USERNAME'] = os.environ.get('CARDCOM_USERNAME', '')
    
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'admin.login'
    
    from routes.ivr import ivr_bp
    from routes.admin import admin_bp
    from routes.payment import payment_bp
    from routes.api import api_bp
    from routes.email_inbound import email_bp
    from routes.institution import institution_bp
    from routes.institution_billing import institution_billing_bp
    from routes.institution_transcribe import institution_transcribe_bp
    from routes.institution_students import institution_students_bp
    from routes.recording_proxy import download_bp

    app.register_blueprint(ivr_bp, url_prefix='/ivr')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(payment_bp, url_prefix='/payment')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(email_bp, url_prefix='/api')
    app.register_blueprint(institution_bp, url_prefix='/institution')
    app.register_blueprint(institution_billing_bp)
    app.register_blueprint(institution_transcribe_bp)
    app.register_blueprint(institution_students_bp)
    app.register_blueprint(download_bp)

    # Route ציבורי לקבצי פקס זמניים
    @app.route('/static/fax_tmp/<filename>')
    def fax_file(filename):
        fax_dir = os.path.join(app.root_path, 'static', 'fax_tmp')
        return send_from_directory(fax_dir, filename)
    
    with app.app_context():
        db.create_all()
        _migrate_db()
        _create_default_admin()

    # פילטר להצגת זמן לפי אזור זמן ישראל - מטפל אוטומטית בשעון קיץ/חורף
    # (לא כמו hours=3 קבוע שהיה שגוי בחורף, כשישראל היא UTC+2 ולא UTC+3)
    from zoneinfo import ZoneInfo
    from datetime import timezone as _timezone

    def il_time(dt, fmt='%d/%m/%Y %H:%M'):
        if dt is None:
            return ''
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_timezone.utc)
        return dt.astimezone(ZoneInfo('Asia/Jerusalem')).strftime(fmt)

    app.jinja_env.filters['il_time'] = il_time

    # פילטר לחילוץ מספר ההקלטה האמיתי בימות מתוך rec_url
    # (rec_url לדוגמה: https://www.call2all.co.il/ym/api/DownloadFile?...&path=ivr2:/recordings/160.wav
    #  צריך להציג רק "160" - זה מה שמופיע בפועל בממשק ניהול הקבצים של ימות)
    import re as _re

    def yemot_rec_number(rec_url):
        if not rec_url:
            return None
        match = _re.search(r'recordings/(\d+)\.wav', rec_url)
        return match.group(1) if match else None

    app.jinja_env.filters['yemot_rec_number'] = yemot_rec_number

    logging.basicConfig(level=logging.INFO)
    return app

def _migrate_db():
    statements = [
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS transcription_tier VARCHAR(10) DEFAULT 'basic'",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS alefbot_job_id VARCHAR(100)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS rec_url VARCHAR(500)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS source_filename VARCHAR(255)",
        # שדות סטטוס פקס (ימות המשיח) - שייכים ל-recordings, לא ל-customers
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_campaign_id VARCHAR(64)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_status VARCHAR(32)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_status_note TEXT",
        "CREATE INDEX IF NOT EXISTS ix_recordings_fax_campaign_id ON recordings (fax_campaign_id)",
        "CREATE INDEX IF NOT EXISTS ix_recordings_customer_id ON recordings (customer_id)",
        "CREATE INDEX IF NOT EXISTS ix_transactions_customer_id ON transactions (customer_id)",
        # עמודות pending payment
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS transcription_tier VARCHAR(10)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS language VARCHAR(10)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS output_language VARCHAR(10)",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
        "ALTER TABLE recordings ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS default_settings JSONB",
        # טבלת OCR
        """CREATE TABLE IF NOT EXISTS ocr_results (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                original_filename VARCHAR(255),
                original_file_path VARCHAR(512),
                ocr_text TEXT,
                char_count INTEGER DEFAULT 0,
                cost FLOAT DEFAULT 0.0,
                engine VARCHAR(20) DEFAULT 'gemini',
                status VARCHAR(20) DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS ix_ocr_results_customer_id ON ocr_results (customer_id)",
        # עמודות pending payment עבור OCR
        "ALTER TABLE ocr_results ADD COLUMN IF NOT EXISTS delivered_to VARCHAR(255)",
        "ALTER TABLE ocr_results ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
        # ניקוי - עמודות פקס שנוספו בטעות ל-customers בעבר
        "ALTER TABLE customers DROP COLUMN IF EXISTS fax_campaign_id",
        "ALTER TABLE customers DROP COLUMN IF EXISTS fax_status",
        "ALTER TABLE customers DROP COLUMN IF EXISTS fax_status_note",
        # עמודת תמלול הודעות למנהל (שלוחה 9) - נוספה למודל ManagerMessage
        "ALTER TABLE manager_messages ADD COLUMN IF NOT EXISTS transcript TEXT",
        # עמודת שרשור (In-Reply-To) בהתכתבות לקוחות - נוספה למודל CustomerMessage
        "ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS message_id VARCHAR(255)",
        # שיוך הודעה לשיחה (ConversationThread) - נוסף כשעברנו מ"שרשור שטוח" ל"שיחות נפרדות"
        "ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS thread_id INTEGER REFERENCES conversation_threads(id)",
        # תיבה כללית - מיילים נכנסים שלא ניתן לשייך לאף לקוח (ראה models.GeneralInboxMessage)
        """CREATE TABLE IF NOT EXISTS general_inbox_messages (
                id SERIAL PRIMARY KEY,
                from_email VARCHAR(255) NOT NULL,
                subject VARCHAR(500),
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS ix_general_inbox_messages_created_at ON general_inbox_messages (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_general_inbox_messages_from_email ON general_inbox_messages (from_email)",
        "ALTER TABLE general_inbox_messages ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()",
        # שיחה דו-כיוונית בתיבה הכללית - הודעות בתוך שרשור (ראה models.InboxMessage)
        """CREATE TABLE IF NOT EXISTS inbox_messages (
                id SERIAL PRIMARY KEY,
                thread_id INTEGER NOT NULL REFERENCES general_inbox_messages(id),
                direction VARCHAR(10) NOT NULL,
                body TEXT NOT NULL,
                message_id VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS ix_inbox_messages_thread_id ON inbox_messages (thread_id)",
        # מיגרציית נתונים חד-פעמית: אם קיימת עדיין עמודת body ישנה (גרסה קודמת
        # של הפיצ'ר, לפני שהתיבה הכללית הפכה לשיחה דו-כיוונית) - מעבירים כל
        # הודעה קיימת להיות ההודעה הראשונה (direction='in') בשרשור שלה.
        """INSERT INTO inbox_messages (thread_id, direction, body, created_at)
           SELECT id, 'in', body, created_at FROM general_inbox_messages
           WHERE body IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM inbox_messages WHERE thread_id = general_inbox_messages.id)""",
        "ALTER TABLE general_inbox_messages DROP COLUMN IF EXISTS body",

        # ===== ניהול מוסד (תלמידים) =====
        """CREATE TABLE IF NOT EXISTS institutions (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                email VARCHAR(200),
                phone VARCHAR(20),
                login_code VARCHAR(20),
                password_hash VARCHAR(256),
                google_id VARCHAR(100) UNIQUE,
                balance FLOAT DEFAULT 0.0,
                is_blocked BOOLEAN DEFAULT FALSE,
                max_usage_per_student FLOAT,
                allowed_hours_start VARCHAR(5),
                allowed_hours_end VARCHAR(5),
                notify_email VARCHAR(200),
                notify_fax VARCHAR(20),
                authorized_logins JSONB,
                card_last4 VARCHAR(4),
                nedarim_token VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        """CREATE TABLE IF NOT EXISTS institution_charge_logs (
                id SERIAL PRIMARY KEY,
                institution_id INTEGER NOT NULL REFERENCES institutions(id),
                amount FLOAT NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                provider_ref VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS ix_institution_charge_logs_institution_id ON institution_charge_logs (institution_id)",
        # customers.phone הפך לאופציונלי - "תלמיד" עשוי להיות מזוהה רק לפי
        # מספר תלמיד (student_number), בלי טלפון בכלל
        "ALTER TABLE customers ALTER COLUMN phone DROP NOT NULL",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS institution_id INTEGER REFERENCES institutions(id)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS student_number VARCHAR(10) UNIQUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS student_display_name VARCHAR(100)",
        "CREATE INDEX IF NOT EXISTS ix_customers_institution_id ON customers (institution_id)",
        "CREATE INDEX IF NOT EXISTS ix_customers_student_number ON customers (student_number)",
        """CREATE TABLE IF NOT EXISTS institution_uploads (
                id SERIAL PRIMARY KEY,
                institution_id INTEGER NOT NULL REFERENCES institutions(id),
                original_filename VARCHAR(255),
                tier VARCHAR(20) DEFAULT 'gemini',
                status VARCHAR(20) DEFAULT 'processing',
                transcript TEXT,
                docx_filename VARCHAR(255),
                error_message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS ix_institution_uploads_institution_id ON institution_uploads (institution_id)",
        # הודעה למנהל: מקור ('ivr' ברירת מחדל, או 'institution_contact' לטופס
        # "צור קשר" בממשק ניהול מוסד - שאין לו rec_url בכלל)
        "ALTER TABLE manager_messages ADD COLUMN IF NOT EXISTS source VARCHAR(30) DEFAULT 'ivr'",
        # משך/עלות תמלול אד-הוק של המוסד עצמו (לשונית "יצירת תמלול")
        "ALTER TABLE institution_uploads ADD COLUMN IF NOT EXISTS duration_seconds INTEGER",
        "ALTER TABLE institution_uploads ADD COLUMN IF NOT EXISTS cost FLOAT",
        # מגבלת שימוש לתלמיד: יומית/שבועית בדקות, במקום מגבלת ₪ ישנה
        # (max_usage_per_student נשארת בטבלה בלי שימוש, לתאימות לאחור)
        "ALTER TABLE institutions ADD COLUMN IF NOT EXISTS max_minutes_per_period FLOAT",
        "ALTER TABLE institutions ADD COLUMN IF NOT EXISTS limit_period VARCHAR(10)",
    ]
    logger = logging.getLogger(__name__)
    ok, failed = 0, 0
    # כל שורה בעסקה (transaction) ונפרדת משלה - כדי שכישלון בשורה אחת (למשל
    # עמודה שכבר קיימת בפורמט אחר, או טבלה שעדיין לא נוצרה) לא יעצור את כל
    # שאר המיגרציות שאחריה. זו הייתה תקלה אמיתית שקרתה בעבר: כל המיגרציה
    # הייתה בלוק try אחד עם commit יחיד, ושורה ראשונה שנכשלה חסמה את כל השאר.
    for stmt in statements:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text(stmt))
                conn.commit()
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Migration statement skipped ({stmt[:60]}...): {e}")
    logger.info(f"Migration: {ok} statements applied, {failed} skipped")


def _create_default_admin():
    try:
        from models import AdminUser
        if not AdminUser.query.first():
            from werkzeug.security import generate_password_hash
            admin = AdminUser(
                username='admin',
                password_hash=generate_password_hash('admin123')
            )
            db.session.add(admin)
            db.session.commit()
    except Exception:
        db.session.rollback()

app = create_app()
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
