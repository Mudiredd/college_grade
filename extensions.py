from flask_bcrypt import Bcrypt
from flask_mail import Mail
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer
from authlib.integrations.flask_client import OAuth

bcrypt = Bcrypt()
mail = Mail()
migrate = Migrate()
limiter = Limiter(key_func=get_remote_address)
oauth = OAuth()
serializer = None


def init_serializer(secret_key):
    global serializer
    serializer = URLSafeTimedSerializer(secret_key)
