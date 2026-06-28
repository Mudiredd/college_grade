import uuid
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id                = db.Column(db.Integer, primary_key=True)
    uuid              = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()), nullable=False)
    name              = db.Column(db.String(100), nullable=False)
    email             = db.Column(db.String(120), unique=True, nullable=False)
    password          = db.Column(db.String(200), nullable=False)
    regd_no           = db.Column(db.String(20), nullable=True, default='')
    is_admin          = db.Column(db.Boolean, default=False)
    plan              = db.Column(db.String(20), default='free')
    email_verified    = db.Column(db.Boolean, default=False)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    subscriptions     = db.relationship('Subscription', backref='user', lazy=True)

    __table_args__ = (
        db.Index('ix_user_email', 'email'),
        db.Index('ix_user_uuid', 'uuid'),
    )


class Subscription(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id', name='fk_subscription_user'), nullable=False, index=True)
    plan       = db.Column(db.String(20), nullable=False)
    priority   = db.Column(db.Integer, default=3)
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date   = db.Column(db.DateTime, nullable=True)
    is_active  = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.Index('ix_subscription_user_active', 'user_id', 'is_active'),
    )


class Payment(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id', name='fk_payment_user'), nullable=False, index=True)
    utr          = db.Column(db.String(50), nullable=False)
    amount       = db.Column(db.Float, nullable=False)
    plan         = db.Column(db.String(20), nullable=False)
    status       = db.Column(db.String(20), default='pending')
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_payment_user_date', 'user_id', 'submitted_at'),
    )


class SearchHistory(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id', name='fk_search_user'), nullable=False, index=True)
    regd_no     = db.Column(db.String(20), nullable=False)
    searched_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_search_user_date', 'user_id', 'searched_at'),
    )