from flask_bcrypt import Bcrypt
from flask_mail import Mail
from itsdangerous import URLSafeTimedSerializer

bcrypt     = Bcrypt()
mail       = Mail()
serializer = None  # initialized in app.py after SECRET_KEY is set

def init_serializer(secret_key):
    global serializer
    serializer = URLSafeTimedSerializer(secret_key)