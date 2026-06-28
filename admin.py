from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, Payment, Subscription
from extensions import bcrypt, mail
from flask_mail import Message
from datetime import datetime, timedelta

admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():

    if current_user.is_authenticated and current_user.is_admin:
        return redirect(url_for('admin.admin_panel'))

    if request.method == 'POST':

        email = request.form.get('email')
        password = request.form.get('password')

        if (
            email == current_app.config['ADMIN_EMAIL']
            and password == current_app.config['ADMIN_PASSWORD']
        ):

            user = User.query.filter_by(
                email=email,
                is_admin=True
            ).first()

            if user:
                login_user(user)
                return redirect(url_for('admin.admin_panel'))

        flash('Invalid admin credentials!', 'error')

    return render_template('admin_login.html')


@admin_bp.route('/admin')
@login_required
def admin_panel():

    if not current_user.is_admin:
        return redirect(url_for('index'))

    pending = Payment.query.filter_by(
        status='pending'
    ).order_by(
        Payment.submitted_at.desc()
    ).all()

    approved = Payment.query.filter_by(
        status='approved'
    ).order_by(
        Payment.submitted_at.desc()
    ).limit(10).all()

    users = User.query.order_by(
        User.created_at.desc()
    ).all()

    return render_template(
        'admin.html',
        pending=pending,
        approved=approved,
        users=users
    )


@admin_bp.route('/admin/approve/<int:payment_id>')
@login_required
def approve_payment(payment_id):

    if not current_user.is_admin:
        return redirect(url_for('index'))

    payment = Payment.query.get_or_404(payment_id)

    payment.status = 'approved'

    # ── Disable existing subscriptions ──
    existing = Subscription.query.filter_by(
        user_id=payment.user_id,
        is_active=True
    ).all()

    for s in existing:
        s.is_active = False

    # ── Create new subscription ──
    end_date = (
        datetime.now() + timedelta(days=30)
        if payment.plan == 'monthly'
        else None
    )

    sub = Subscription(
        user_id=payment.user_id,
        plan=payment.plan,
        end_date=end_date
    )

    db.session.add(sub)
    db.session.commit()

    user = User.query.get(payment.user_id)

    # ── Send approval email ──
    try:

        plan_text = (
            '1 Month'
            if payment.plan == 'monthly'
            else 'Lifetime'
        )

        msg = Message(
            subject='✅ Payment Approved — SR & BGNR College Tracker',
            sender=current_app.config['MAIL_USERNAME'],
            recipients=[user.email]
        )

        msg.body = f"""Hi {user.name},

Your payment of ₹{payment.amount} has been approved!

Plan: {plan_text}

You can now use SR & BGNR College Tracker.

— Admin"""

        # ✅ FIXED: mail send inside app context
        with current_app.app_context():
            mail.send(msg)

    except Exception as e:
        print("Mail Error:", e)

    flash(
        f'Payment approved for {user.name}!',
        'success'
    )

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/reject/<int:payment_id>')
@login_required
def reject_payment(payment_id):

    if not current_user.is_admin:
        return redirect(url_for('index'))

    payment = Payment.query.get_or_404(payment_id)

    payment.status = 'rejected'

    db.session.commit()

    flash(
        'Payment rejected!',
        'error'
    )

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/cancel-subscription/<int:user_id>')
@login_required
def cancel_subscription(user_id):

    if not current_user.is_admin:
        return redirect(url_for('index'))

    subs = Subscription.query.filter_by(
        user_id=user_id,
        is_active=True
    ).all()

    for s in subs:
        s.is_active = False

    db.session.commit()

    user = User.query.get(user_id)

    flash(
        f'Subscription cancelled for {user.name}!',
        'error'
    )

    return redirect(url_for('admin.admin_panel'))