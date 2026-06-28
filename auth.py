from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message

from models import db, User
from extensions import bcrypt, mail

auth_bp = Blueprint('auth', __name__)


# ── Send Reset Email ─────────────────────────────────────
def send_reset_email(email, token):

    reset_url = url_for(
        'auth.reset_password',
        token=token,
        _external=True
    )

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

        # ✅ FIXED: mail send inside app context
        with current_app.app_context():
            mail.send(msg)

    except Exception as e:
        print("Mail Error:", e)


# ── Signup ───────────────────────────────────────────────
@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():

    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':

        name = request.form.get('name').strip()

        email = request.form.get('email').strip().lower()

        password = request.form.get('password')

        regd_no = request.form.get('regd_no').strip()

        # ── Check existing email ──
        if User.query.filter_by(email=email).first():

            flash(
                'Email already registered!',
                'error'
            )

            return redirect(url_for('auth.signup'))

        # ── Create account ──
        hashed_pw = bcrypt.generate_password_hash(
            password
        ).decode('utf-8')

        user = User(
            name=name,
            email=email,
            password=hashed_pw,
            regd_no=regd_no
        )

        db.session.add(user)
        db.session.commit()

        login_user(user)

        flash(
            'Account created! Please subscribe to start searching.',
            'success'
        )

        return redirect(url_for('subscribe'))

    return render_template('signup.html')


# ── Login ────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():

    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':

        email = request.form.get('email').strip().lower()

        password = request.form.get('password')

        user = User.query.filter_by(
            email=email
        ).first()

        if user and bcrypt.check_password_hash(
            user.password,
            password
        ):

            if user.is_admin:

                flash(
                    'Please use the Admin login page!',
                    'error'
                )

                return redirect(url_for('auth.login'))

            login_user(user)

            return redirect(url_for('index'))

        flash(
            'Invalid email or password!',
            'error'
        )

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

        email = request.form.get('email').strip().lower()

        user = User.query.filter_by(
            email=email
        ).first()

        if user:

            token = serializer.dumps(
                email,
                salt='password-reset'
            )

            # ✅ Send reset email
            send_reset_email(email, token)

        flash(
            'If that email exists, a reset link has been sent!',
            'info'
        )

    return render_template('forgot_password.html')


# ── Reset Password ───────────────────────────────────────
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):

    from extensions import serializer

    try:

        email = serializer.loads(
            token,
            salt='password-reset',
            max_age=1800
        )

    except Exception:

        flash(
            'Reset link is invalid or expired!',
            'error'
        )

        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':

        password = request.form.get('password')

        user = User.query.filter_by(
            email=email
        ).first()

        if user:

            user.password = bcrypt.generate_password_hash(
                password
            ).decode('utf-8')

            db.session.commit()

            flash(
                'Password reset successfully! Please login.',
                'success'
            )

            return redirect(url_for('auth.login'))

    return render_template('reset_password.html')