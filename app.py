import os
import logging
from flask import Flask
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
    
    app.register_blueprint(ivr_bp, url_prefix='/ivr')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(payment_bp, url_prefix='/payment')
    app.register_blueprint(api_bp, url_prefix='/api')
    
    with app.app_context():
        db.create_all()
        _create_default_admin()
    
    logging.basicConfig(level=logging.INFO)
    return app

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
