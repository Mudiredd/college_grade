import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, Payment, Subscription, SearchHistory, SemesterPattern
from extensions import bcrypt, mail
from flask_mail import Message
from datetime import datetime, timedelta

admin_bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


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

    all_payments = Payment.query.order_by(
        Payment.submitted_at.desc()
    ).all()

    users = User.query.order_by(
        User.created_at.desc()
    ).all()

    patterns = SemesterPattern.query.order_by(
        SemesterPattern.joining_year,
        SemesterPattern.semester,
        SemesterPattern.is_supply
    ).all()

    years = [row[0] for row in db.session.query(SemesterPattern.joining_year).distinct().order_by(SemesterPattern.joining_year.desc()).all()]

    # ── Chart data ──
    from sqlalchemy import func, extract
    now = datetime.now()

    # Revenue by month (last 6 months)
    revenue_data = []
    rev_labels = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m < 1:
            m += 12
            y -= 1
        total = db.session.query(func.coalesce(func.sum(Payment.amount), 0))\
            .filter(Payment.status == 'approved',
                    extract('year', Payment.submitted_at) == y,
                    extract('month', Payment.submitted_at) == m).scalar()
        rev_labels.append(f"{y}-{m:02d}")
        revenue_data.append(float(total))

    # User signups by month (last 6 months)
    signup_data = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m < 1:
            m += 12
            y -= 1
        count = User.query.filter(
            extract('year', User.created_at) == y,
            extract('month', User.created_at) == m).count()
        signup_data.append(count)

    # Plan distribution (active subscriptions)
    plan_counts = {}
    for p in ['free', 'basic', 'monthly', 'yearly']:
        plan_counts[p] = Subscription.query.filter_by(plan=p, is_active=True).count()
    # Free users are all users without an active subscription
    free_count = User.query.count() - Subscription.query.filter_by(is_active=True).count()
    plan_counts['free'] = free_count

    return render_template(
        'admin.html',
        pending=pending,
        approved=approved,
        all_payments=all_payments,
        users=users,
        patterns=patterns,
        years=years,
        rev_labels=rev_labels,
        revenue_data=revenue_data,
        signup_data=signup_data,
        plan_counts=plan_counts
    )





def _activate_payment(payment):
    """Activate subscription for an approved payment."""
    payment.status = 'approved'
    existing = Subscription.query.filter_by(user_id=payment.user_id, is_active=True).all()
    for s in existing:
        s.is_active = False
    plan_days = {'basic': 1, 'monthly': 30, 'yearly': 365}
    plan_priority = {'basic': 2, 'monthly': 1, 'yearly': 0}
    days = plan_days.get(payment.plan)
    end_date = datetime.now() + timedelta(days=days) if days else None
    sub = Subscription(
        user_id=payment.user_id, plan=payment.plan,
        priority=plan_priority.get(payment.plan, 3), end_date=end_date
    )
    user = User.query.get(payment.user_id)
    if user:
        user.plan = payment.plan
    db.session.add(sub)


def _send_approval_email(payment):
    """Send approval email to the user."""
    try:
        user = User.query.get(payment.user_id)
        if not user:
            return
        plan_labels = {'basic': 'Basic — 1 Day / 3 Searches', 'monthly': 'Monthly — 30 Days', 'yearly': 'Yearly — 365 Days'}
        plan_text = plan_labels.get(payment.plan, payment.plan)
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
        with current_app.app_context():
            mail.send(msg)
    except BaseException as e:
        logger.exception(f"Approval mail error: {e}")


def _send_rejection_email(payment):
    """Send rejection email to the user."""
    try:
        user = User.query.get(payment.user_id)
        if not user:
            return
        msg = Message(
            subject='❌ Payment Rejected — SR & BGNR College Tracker',
            sender=current_app.config['MAIL_USERNAME'],
            recipients=[user.email]
        )
        msg.body = f"""Hi {user.name},

Your payment of ₹{payment.amount} for the {payment.plan} plan has been rejected.

This could be due to an incorrect UTR number or payment details. Please try again with the correct information.

If you think this is a mistake, contact support.

— Admin"""
        with current_app.app_context():
            mail.send(msg)
    except BaseException as e:
        logger.exception(f"Rejection mail error: {e}")


def _send_telegram(message):
    """Send Telegram notification."""
    token = current_app.config.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = current_app.config.get('ADMIN_CHAT_ID', '')
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return False
    try:
        import requests
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        if not resp.ok:
            logger.warning(f"Telegram API error: {resp.status_code} {resp.text}")
            return False
        return True
    except BaseException as e:
        logger.exception(f"Telegram send failed: {e}")
        return False


@admin_bp.route('/admin/payments/<int:payment_id>/status', methods=['POST'])
@login_required
def set_payment_status(payment_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403

    payment = Payment.query.get_or_404(payment_id)
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')

    if new_status not in ('approved', 'rejected', 'pending'):
        return jsonify({'error': 'Invalid status'}), 400

    old_status = payment.status

    if new_status == old_status:
        return jsonify({'error': 'Status unchanged'}), 400

    # ── Handle transition ──
    try:
        user = User.query.get(payment.user_id)
        user_info = f"{user.name} / {user.email}" if user else f"User #{payment.user_id}"
        if new_status == 'approved':
            _activate_payment(payment)
            _send_approval_email(payment)
            _send_telegram(
                f"✅ <b>Payment Approved</b>\n\n"
                f"<b>User:</b> {user_info}\n"
                f"<b>Plan:</b> {payment.plan}\n"
                f"<b>Amount:</b> ₹{payment.amount}\n"
                f"<b>UTR:</b> {payment.utr}"
            )
        elif new_status == 'rejected':
            payment.status = 'rejected'
            _send_rejection_email(payment)
            _send_telegram(
                f"❌ <b>Payment Rejected</b>\n\n"
                f"<b>User:</b> {user_info}\n"
                f"<b>Plan:</b> {payment.plan}\n"
                f"<b>Amount:</b> ₹{payment.amount}\n"
                f"<b>UTR:</b> {payment.utr}"
            )
        else:  # revert to pending
            payment.status = 'pending'

        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Payment status change error: {e}")
        return jsonify({'error': 'Failed to update payment status'}), 500


@admin_bp.route('/admin/payments/bulk', methods=['POST'])
@login_required
def bulk_payment_action():

    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    action = data.get('action')
    ids = data.get('ids', [])

    if action not in ('approve', 'reject', 'pending'):
        return jsonify({'error': 'Invalid action'}), 400

    count = 0
    try:
        for pid in ids:
            payment = Payment.query.get(pid)
            if not payment:
                continue
            if action == 'approve':
                if payment.status == 'approved':
                    continue
                _activate_payment(payment)
                _send_approval_email(payment)
            elif action == 'reject':
                if payment.status == 'rejected':
                    continue
                payment.status = 'rejected'
                _send_rejection_email(payment)
            else:  # revert to pending
                if payment.status == 'pending':
                    continue
                payment.status = 'pending'
            count += 1

        db.session.commit()

        if count:
            action_label = {'approve': 'Approved', 'reject': 'Rejected', 'pending': 'Reverted to Pending'}
            _send_telegram(
                f"📋 <b>Bulk Payment {action_label.get(action, action)}</b>\n\n"
                f"<b>Count:</b> {count} payment(s)\n"
                f"<b>Action:</b> {action_label.get(action, action)}"
            )

        return jsonify({'ok': True, 'count': count})
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Bulk payment action error: {e}")
        return jsonify({'error': 'Failed to process bulk action'}), 500


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

    user = User.query.get(user_id)
    if user:
        user.plan = 'free'

    db.session.commit()

    flash(
        f'Subscription cancelled for {user.name}!',
        'error'
    )

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/reset-limits/<int:user_id>')
@login_required
def reset_user_limits(user_id):
    if not current_user.is_admin:
        return redirect(url_for('index'))

    today = datetime.now().date()
    SearchHistory.query.filter(
        SearchHistory.user_id == user_id,
        db.func.date(SearchHistory.searched_at) == today
    ).delete()
    db.session.commit()

    user = User.query.get(user_id)
    flash(f"Today's search limit reset for {user.name}!", 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/delete-user/<int:user_id>')
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash('Cannot delete admin users!', 'error')
        return redirect(url_for('admin.admin_panel'))

    Subscription.query.filter_by(user_id=user_id).delete()
    Payment.query.filter_by(user_id=user_id).delete()
    SearchHistory.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()

    flash(f'User {user.name} deleted!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/patterns/add', methods=['POST'])
@login_required
def add_pattern():
    if not current_user.is_admin:
        return redirect(url_for('index'))

    joining_year = request.form.get('joining_year', type=int)
    exam_name    = request.form.get('exam_name', '').strip().upper()
    semester     = request.form.get('semester', type=int)
    is_supply    = request.form.get('is_supply') == 'on'

    if not all([joining_year, exam_name, semester]):
        flash('Please fill all required fields!', 'error')
        return redirect(url_for('admin.admin_panel'))

    existing = SemesterPattern.query.filter_by(
        joining_year=joining_year, exam_name=exam_name,
        semester=semester, is_supply=is_supply
    ).first()
    if existing:
        flash('This pattern already exists!', 'error')
        return redirect(url_for('admin.admin_panel'))

    pattern = SemesterPattern(
        joining_year=joining_year,
        exam_name=exam_name,
        semester=semester,
        is_supply=is_supply
    )
    db.session.add(pattern)
    db.session.commit()

    flash(f'Pattern {exam_name} → Sem {semester} added!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/patterns/delete/<int:pattern_id>')
@login_required
def delete_pattern(pattern_id):
    if not current_user.is_admin:
        return redirect(url_for('index'))

    pattern = SemesterPattern.query.get_or_404(pattern_id)
    db.session.delete(pattern)
    db.session.commit()

    flash('Pattern deleted!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/patterns/edit/<int:pattern_id>', methods=['POST'])
@login_required
def edit_pattern(pattern_id):
    if not current_user.is_admin:
        return redirect(url_for('index'))

    pattern = SemesterPattern.query.get_or_404(pattern_id)
    pattern.exam_name = request.form.get('exam_name', '').strip().upper()
    pattern.semester = request.form.get('semester', type=int)
    pattern.is_supply = request.form.get('is_supply') == 'on'
    db.session.commit()

    flash('Pattern updated!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/patterns/delete-year/<int:joining_year>')
@login_required
def delete_year(joining_year):
    if not current_user.is_admin:
        return redirect(url_for('index'))

    SemesterPattern.query.filter_by(joining_year=joining_year).delete()
    db.session.commit()

    flash(f'All patterns for year {joining_year} deleted!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/patterns/seed', methods=['POST'])
@login_required
def seed_patterns():
    if not current_user.is_admin:
        return redirect(url_for('index'))

    joining_year = request.form.get('joining_year', type=int)
    if not joining_year:
        flash('Please enter a joining year!', 'error')
        return redirect(url_for('admin.admin_panel'))

    # Year offset per semester (common 3-year degree pattern: sems 1-3 in year 0, sems 4-5 in year 1, sem 6 in year 2)
    yr_offsets = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 2}
    # Regular: (semester, month)
    regular_pattern = [
        (1, 'JAN'), (2, 'MAY'), (3, 'OCT'),
        (4, 'APR'), (5, 'NOV'), (6, 'APR'),
    ]
    # Supply: (semester, month)
    supply_pattern = [
        (1, 'OCT'), (2, 'APR'), (3, 'NOV'),
    ]

    added = 0
    for sem, month in regular_pattern:
        exam = f"{month}-{joining_year + yr_offsets[sem]:02d}"
        if not SemesterPattern.query.filter_by(
            joining_year=joining_year, exam_name=exam,
            semester=sem, is_supply=False
        ).first():
            db.session.add(SemesterPattern(
                joining_year=joining_year, exam_name=exam,
                semester=sem, is_supply=False
            ))
            added += 1

    for sem, month in supply_pattern:
        exam = f"{month}-{joining_year + yr_offsets[sem]:02d}"
        if not SemesterPattern.query.filter_by(
            joining_year=joining_year, exam_name=exam,
            semester=sem, is_supply=True
        ).first():
            db.session.add(SemesterPattern(
                joining_year=joining_year, exam_name=exam,
                semester=sem, is_supply=True
            ))
            added += 1

    db.session.commit()
    flash(f'{added} patterns seeded for joining year {joining_year}!', 'success')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/api/years')
@login_required
def api_years():
    if not current_user.is_admin:
        return jsonify([])
    years = [row[0] for row in db.session.query(SemesterPattern.joining_year).distinct().order_by(SemesterPattern.joining_year.desc()).all()]
    return jsonify(years)