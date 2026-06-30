import os
from dotenv import load_dotenv

load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'srbgnr_secret_key_2026')

    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(basedir, 'instance', 'college_tracker.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MAIL_SERVER = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.getenv('MAIL_PORT', 587))
    MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USERNAME = os.getenv('MAIL_USERNAME', 'vishnu12shiva@gmail.com')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD', 'YOUR_APP_PASSWORD')

    ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'vishnu12shiva@gmail.com')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'sravan123')

    UPI_ID = os.getenv('UPI_ID', 'mudireddyreddy346@upi')

    SESSION_COOKIE_SECURE = bool(os.getenv('SESSION_COOKIE_SECURE', 'false').lower() in ('true', '1'))
    SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')

    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
    BASE_URL = os.getenv('BASE_URL', '').rstrip('/')

    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')
