from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password      = db.Column(db.String(200), nullable=False)
    regd_no       = db.Column(db.String(20),  nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    subscriptions = db.relationship('Subscription', backref='user', lazy=True)

class Subscription(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan       = db.Column(db.String(20), nullable=False)
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date   = db.Column(db.DateTime, nullable=True)
    is_active  = db.Column(db.Boolean, default=True)

class Payment(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    utr          = db.Column(db.String(50), nullable=False)
    amount       = db.Column(db.Float, nullable=False)
    plan         = db.Column(db.String(20), nullable=False)
    status       = db.Column(db.String(20), default='pending')
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

class SearchHistory(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    regd_no     = db.Column(db.String(20), nullable=False)
    searched_at = db.Column(db.DateTime, default=datetime.utcnow)