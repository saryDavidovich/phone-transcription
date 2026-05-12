import os
import logging
from flask import Flask
from routes.ivr import ivr_bp
from routes.transcribe import transcribe_bp
from routes.deliver import deliver_bp
from services.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

app.config['MAX_RECORDING_SECONDS'] = 300
app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY', '')
app.config['SENDGRID_API_KEY'] = os.environ.get('SENDGRID_API_KEY', '')
app.config['SENDGRID_FROM'] = os.environ.get('SENDGRID_FROM', '')
app.config['ANTHROPIC_API_KEY'] = os.environ.get('ANTHROPIC_API_KEY', '')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')

app.register_blueprint(ivr_bp, url_prefix='/ivr')
app.register_blueprint(transcribe_bp, url_prefix='/transcribe')
app.register_blueprint(deliver_bp, url_prefix='/deliver')

init_db()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
