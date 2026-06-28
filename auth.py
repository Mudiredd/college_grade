import re
import logging
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from email_validator import validate_email, EmailNotValidError

from models import db, User
from extensions import bcrypt, mail

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


def _validate_regd_no(regd_no):
    if not re.match(r'^\d{7,20}$', regd_no):
        return 'Invalid registration number format'
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


# ── Signup ───────────────────────────────────────────────
@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:100]
        email_raw = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        regd_no = request.form.get('regd_no', '').strip()

        # validate email
        try:
            valid = validate_email(email_raw)
            email = valid.email
        except EmailNotValidError:
            flash('Invalid email address!', 'error')
            return redirect(url_for('auth.signup'))

        # validate password
        pw_err = _validate_password(password)
        if pw_err:
            flash(pw_err, 'error')
            return redirect(url_for('auth.signup'))

        # validate regd_no
        rn_err = _validate_regd_no(regd_no)
        if rn_err:
            flash(rn_err, 'error')
            return redirect(url_for('auth.signup'))

        if not name:
            flash('Name is required!', 'error')
            return redirect(url_for('auth.signup'))

        # check existing email
        if User.query.filter_by(email=email).first():
            flash('Email already registered!', 'error')
            return redirect(url_for('auth.signup'))

        # create account
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            regd_no=regd_no,
            plan='free',
            password_changed_at=datetime.utcnow()
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash('Account created! Please subscribe to start searching.', 'success')
        return redirect(url_for('subscribe'))

    return render_template('signup.html')


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

            login_user(user)
            return redirect(url_for('index'))

        flash('Invalid email or password!', 'error')

    return render_template('login.html')


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

        # try to validate email format
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

    # if password was changed after token was issued, invalidate
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
