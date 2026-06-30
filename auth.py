import re
import logging
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from email_validator import validate_email, EmailNotValidError

from models import db, User
from extensions import bcrypt, mail, oauth

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)


def _validate_password(password):
    if len(password) < 6:
        return 'Password must be at least 6 characters'
    if not re.search(r'[A-Za-z]', password):
        return 'Password must contain at least one letter'
    if not re.search(r'[0-9]', password):
        return 'Password must contain at least one number'
    return None


# ── Send Reset Email ─────────────────────────────────────
def send_reset_email(email, token):
    reset_url = url_for('auth.reset_password', token=token, _external=True)

    try:
        msg = Message(
            subject='SR & BGNR College Tracker — Password Reset',
            sender=current_app.config['MAIL_USERNAME'],
            recipients=[email]
        )
        msg.body = f"""Hi,

You requested a password reset for your College Tracker account.

Click the link below to reset your password (valid for 30 minutes):

{reset_url}

If you didn't request this, ignore this email.

— SR & BGNR College Tracker"""

        with current_app.app_context():
            mail.send(msg)

    except Exception as e:
        logger.warning(f"Mail send failed for {email}: {e}")


# ── Send Verification Email ──────────────────────────────
def send_verify_email(email, token):
    verify_url = url_for('auth.verify_email', token=token, _external=True)

    try:
        msg = Message(
            subject='Verify your email — SR & BGNR College Tracker',
            sender=current_app.config['MAIL_USERNAME'],
            recipients=[email]
        )
        msg.body = f"""Hi,

Welcome to SR & BGNR College Tracker!

Click the link below to verify your email address (valid for 24 hours):

{verify_url}

If you didn't create an account, ignore this email.

— SR & BGNR College Tracker"""

        with current_app.app_context():
            mail.send(msg)
        return True
    except Exception as e:
        logger.warning(f"Verify email send failed for {email}: {e}")
        return False


# ── Signup ───────────────────────────────────────────────
@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:100]
        email_raw = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        try:
            valid = validate_email(email_raw)
            email = valid.email
        except EmailNotValidError:
            flash('Invalid email address!', 'error')
            return redirect(url_for('auth.signup'))

        pw_err = _validate_password(password)
        if pw_err:
            flash(pw_err, 'error')
            return redirect(url_for('auth.signup'))

        if not name:
            flash('Name is required!', 'error')
            return redirect(url_for('auth.signup'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered!', 'error')
            return redirect(url_for('auth.signup'))

        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            plan='free',
            password_changed_at=datetime.utcnow()
        )
        db.session.add(user)
        db.session.commit()

        # send verification email
        from extensions import serializer
        token = serializer.dumps(email, salt='email-verify')
        sent = send_verify_email(email, token)

        flash('Account created! Check your email to verify your account.', 'success')
        return render_template('verify_email.html', email=email, sent=sent)

    return render_template('signup.html')


# ── Verify Email ─────────────────────────────────────────
@auth_bp.route('/verify-email/<token>')
def verify_email(token):
    from extensions import serializer
    try:
        email = serializer.loads(token, salt='email-verify', max_age=86400)
    except Exception:
        flash('Verification link is invalid or expired!', 'error')
        return redirect(url_for('auth.login'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('User not found!', 'error')
        return redirect(url_for('auth.signup'))

    if user.email_verified:
        flash('Email already verified! Please login.', 'info')
    else:
        user.email_verified = True
        db.session.commit()
        flash('Email verified successfully! You can now login.', 'success')

    return redirect(url_for('auth.login'))


# ── Resend Verification ──────────────────────────────────
@auth_bp.route('/resend-verification', methods=['POST'])
def resend_verification():
    from extensions import serializer
    email = request.form.get('email', '').strip().lower()

    try:
        valid = validate_email(email)
        email = valid.email
    except EmailNotValidError:
        flash('Invalid email address!', 'error')
        return redirect(url_for('auth.signup'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('If that email exists, a verification link has been sent!', 'info')
        return redirect(url_for('auth.login'))

    if user.email_verified:
        flash('Email already verified! Please login.', 'info')
        return redirect(url_for('auth.login'))

    token = serializer.dumps(email, salt='email-verify')
    sent = send_verify_email(email, token)

    if sent:
        flash('Verification email resent! Check your inbox.', 'success')
    else:
        flash('Failed to send email. Try again later.', 'error')

    return render_template('verify_email.html', email=email, sent=sent)


# ── Login ────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email).first()

        if user and bcrypt.check_password_hash(user.password, password):
            if user.is_admin:
                flash('Please use the Admin login page!', 'error')
                return redirect(url_for('auth.login'))

            if not user.email_verified:
                flash('Please verify your email before logging in.', 'error')
                return render_template('login.html', needs_verify=True, email=email)

            login_user(user)
            return redirect(url_for('index'))

        flash('Invalid email or password!', 'error')

    return render_template('login.html')


# ── Google Login ──────────────────────────────────────────
@auth_bp.route('/login/google')
def google_login():
    client = oauth.create_client('google')
    if not client:
        flash('Google login is not configured.', 'error')
        return redirect(url_for('auth.login'))
    base = current_app.config['BASE_URL']
    redirect_uri = base + url_for('auth.google_authorize')
    return client.authorize_redirect(redirect_uri=redirect_uri)


@auth_bp.route('/login/google/authorize')
def google_authorize():
    client = oauth.create_client('google')
    if not client:
        flash('Google login is not configured.', 'error')
        return redirect(url_for('auth.login'))

    try:
        token = client.authorize_access_token()
        userinfo = token.get('userinfo')
        if not userinfo:
            userinfo = client.parse_id_token(token)
    except Exception as e:
        logger.exception(f"Google OAuth callback failed: {e}")
        flash('Google sign-in failed. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    email = userinfo.get('email', '')
    name = userinfo.get('name', email.split('@')[0])
    google_id = userinfo.get('sub', '')

    if not email:
        flash('Could not retrieve your email from Google.', 'error')
        return redirect(url_for('auth.login'))

    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            db.session.commit()
        else:
            import secrets
            user = User(
                name=name,
                email=email,
                password=bcrypt.generate_password_hash(secrets.token_urlsafe(32)).decode('utf-8'),
                google_id=google_id,
                plan='free',
                email_verified=True,
            )
            db.session.add(user)
            db.session.commit()

    login_user(user)
    flash('Signed in with Google!', 'success')
    return redirect(url_for('index'))


# ── Logout ───────────────────────────────────────────────
@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


# ── Forgot Password ──────────────────────────────────────
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        from extensions import serializer

        email = request.form.get('email', '').strip().lower()

        try:
            valid = validate_email(email)
            email = valid.email
        except EmailNotValidError:
            flash('If that email exists, a reset link has been sent!', 'info')
            return render_template('forgot_password.html')

        user = User.query.filter_by(email=email).first()

        if user:
            token = serializer.dumps([email, str(user.password_changed_at or '')], salt='password-reset')
            send_reset_email(email, token)

        flash('If that email exists, a reset link has been sent!', 'info')

    return render_template('forgot_password.html')


# ── Reset Password ───────────────────────────────────────
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    from extensions import serializer

    try:
        payload = serializer.loads(token, salt='password-reset', max_age=1800)
        if isinstance(payload, list):
            email, changed_at_str = payload
        else:
            email = payload
            changed_at_str = ''
    except Exception:
        flash('Reset link is invalid or expired!', 'error')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Reset link is invalid or expired!', 'error')
        return redirect(url_for('auth.forgot_password'))

    if changed_at_str and user.password_changed_at:
        if str(user.password_changed_at) != changed_at_str:
            flash('Reset link is invalid or expired!', 'error')
            return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')

        pw_err = _validate_password(password)
        if pw_err:
            flash(pw_err, 'error')
            return render_template('reset_password.html', token=token)

        user.password = bcrypt.generate_password_hash(password).decode('utf-8')
        user.password_changed_at = datetime.utcnow()
        db.session.commit()

        flash('Password reset successfully! Please login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('reset_password.html')
