import os
import logging
from flask import Flask, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
db = SQLAlchemy()
login_manager = LoginManager()
def create_app():
    app = Flask(__name__)
    
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
    
    app.register_blueprint(ivr_bp, url_prefix='/ivr')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(payment_bp, url_prefix='/payment')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(email_bp, url_prefix='/api')

    # Route ציבורי לקבצי פקס זמניים
    @app.route('/static/fax_tmp/<filename>')
    def fax_file(filename):
        fax_dir = os.path.join(app.root_path, 'static', 'fax_tmp')
        return send_from_directory(fax_dir, filename)
    
    with app.app_context():
        db.create_all()
        _migrate_db()
        _create_default_admin()

    logging.basicConfig(level=logging.INFO)
    return app

def _migrate_db():
    try:
        with db.engine.connect() as conn:
            conn.execute(db.text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS transcription_tier VARCHAR(10) DEFAULT 'basic'"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS alefbot_job_id VARCHAR(100)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS rec_url VARCHAR(500)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS source_filename VARCHAR(255)"))
            # שדות סטטוס פקס (ימות המשיח) - שייכים ל-recordings, לא ל-customers
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_campaign_id VARCHAR(64)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_status VARCHAR(32)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS fax_status_note TEXT"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS ix_recordings_fax_campaign_id ON recordings (fax_campaign_id)"))
            # עמודות pending payment
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS transcription_tier VARCHAR(10)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS language VARCHAR(10)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS output_language VARCHAR(10)"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))
            conn.execute(db.text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))
            conn.execute(db.text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS default_settings JSONB"))
            # טבלת OCR
            conn.execute(db.text("""
                CREATE TABLE IF NOT EXISTS ocr_results (
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
                )
            """))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS ix_ocr_results_customer_id ON ocr_results (customer_id)"))
            # עמודות pending payment עבור OCR
            conn.execute(db.text("ALTER TABLE ocr_results ADD COLUMN IF NOT EXISTS delivered_to VARCHAR(255)"))
            conn.execute(db.text("ALTER TABLE ocr_results ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))
            # ניקוי - עמודות פקס שנוספו בטעות ל-customers בעבר
            conn.execute(db.text("ALTER TABLE customers DROP COLUMN IF EXISTS fax_campaign_id"))
            conn.execute(db.text("ALTER TABLE customers DROP COLUMN IF EXISTS fax_status"))
            conn.execute(db.text("ALTER TABLE customers DROP COLUMN IF EXISTS fax_status_note"))
            conn.commit()
        logging.getLogger(__name__).info("Migration: all columns ready")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration skipped: {e}")

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
