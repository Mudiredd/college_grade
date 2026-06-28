import os
import logging
import time
import json
import uuid
import threading
from datetime import datetime
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import (
    Flask, render_template, request, jsonify,
    Response, redirect, url_for, flash, send_file
)
from flask_cors import CORS
from flask_login import LoginManager, login_required, current_user

from config import Config
# ===================== MODELS =====================
from models import db, User, Subscription, Payment, SearchHistory

# ===================== EXTENSIONS =====================
from extensions import bcrypt, mail, migrate, limiter, init_serializer

# ===================== BLUEPRINTS =====================
from auth import auth_bp
from admin import admin_bp

# ===================== SCRAPER =====================
from scraper import (
    login_step2,
    click_marks_memo,
    get_marks_pdf,
    parse_pdf,
    get_exam_ids_for_student,
    scrape_exam_options
)

_CACHE_TTL = 600
_result_cache = {}

# ===================== POLLING QUEUE =====================
_PLAN_PRIORITY = {'yearly': 0, 'monthly': 1, 'basic': 2, 'free': 3}
_task_id_counter = 0
_task_queue = []  # [(priority, counter, task_id, regd_no, password, user_id), ...]
_task_queue_lock = threading.Lock()
_task_state = {}  # task_id -> dict with status, progress, step, data, error
_task_state_lock = threading.Lock()
_task_cancel = set()  # task_ids marked for cancellation
_executor = ThreadPoolExecutor(max_workers=7)
_dispatcher_running = True


def _next_task_id():
    global _task_id_counter
    _task_id_counter += 1
    return str(_task_id_counter)


def get_plan_priority(user):
    from models import Subscription
    sub = Subscription.query.filter_by(
        user_id=user.id, is_active=True
    ).order_by(Subscription.id.desc()).first()
    if not sub:
        return _PLAN_PRIORITY['free']
    return _PLAN_PRIORITY.get(sub.plan, _PLAN_PRIORITY['free'])


def get_user_plan(user):
    from models import Subscription
    sub = Subscription.query.filter_by(
        user_id=user.id, is_active=True
    ).order_by(Subscription.id.desc()).first()
    if not sub:
        return 'free'
    return sub.plan


def check_search_limit(user):
    plan = get_user_plan(user)
    if plan == 'free':
        today = datetime.now().date()
        cnt = SearchHistory.query.filter(
            SearchHistory.user_id == user.id,
            db.func.date(SearchHistory.searched_at) == today
        ).count()
        if cnt >= 1:
            return False, 'Free trial daily limit reached (1/day)'
    if plan == 'basic':
        total = SearchHistory.query.filter_by(user_id=user.id).count()
        if total >= 1:
            return False, 'Basic plan limit reached (1 search total)'
    elif plan == 'monthly':
        today = datetime.now().date()
        cnt = SearchHistory.query.filter(
            SearchHistory.user_id == user.id,
            db.func.date(SearchHistory.searched_at) == today
        ).count()
        if cnt >= 4:
            return False, 'Monthly plan daily limit reached (4/day)'
    elif plan == 'yearly':
        today = datetime.now().date()
        cnt = SearchHistory.query.filter(
            SearchHistory.user_id == user.id,
            db.func.date(SearchHistory.searched_at) == today
        ).count()
        if cnt >= 8:
            return False, 'Yearly plan daily limit reached (8/day)'
    return True, None


def _dispatcher():
    while _dispatcher_running:
        task = None
        with _task_queue_lock:
            with _task_state_lock:
                running = sum(1 for t in _task_state if _task_state[t]['status'] == 'running')
            if _task_queue and running < 7:
                _task_queue.sort(key=lambda x: (x[0], x[1]))
                running_count = running
                if running_count < 7:
                    task = _task_queue.pop(0)
        if task:
            priority, counter, task_id, regd_no, password, user_id, plan = task
            with _task_state_lock:
                _task_state[task_id] = {
                    'status': 'running', 'progress': 0, 'step': 'queued',
                    'message': 'Starting...', 'semester': None, 'data': None, 'error': None,
                    'regd_no': regd_no, 'user_id': user_id
                }
                _task_state[task_id]['step'] = 'logging_in'
                _task_state[task_id]['message'] = 'Logging in...'
            _executor.submit(_run_scrape, task_id, regd_no, password, user_id, plan)
        else:
            time.sleep(0.5)


def _update_task_state(task_id, **kwargs):
    with _task_state_lock:
        if task_id in _task_state:
            _task_state[task_id].update(kwargs)


def _run_scrape(task_id, regd_no, password, user_id, plan):
    logger.info(f"Task {task_id}: scraping {regd_no} (plan={plan})")
    try:
        if task_id in _task_cancel:
            _update_task_state(task_id, status='cancelled', message='Cancelled')
            _task_cancel.discard(task_id)
            return

        cached = _result_cache.get(regd_no)
        if cached is not None and time.time() - cached[0] < _CACHE_TTL:
            _update_task_state(task_id, status='done', data=cached[1], progress=100)
            return

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://srbgnrexams.ac.in/",
        })
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=2, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        _update_task_state(task_id, step='logging_in', message='Logging in...')
        dashboard = login_step2(session, regd_no, password)

        _update_task_state(task_id, step='marks_memo', message='Opening marks memo...')
        marks_page = click_marks_memo(session, dashboard)

        _update_task_state(task_id, step='exam_list', message='Fetching exam list...')
        exam_names = scrape_exam_options(session, marks_page)

        exam_ids = get_exam_ids_for_student(regd_no, exam_names)
        _update_task_state(task_id, step='scanning', message=f'Scanning {len(exam_ids)} exam sessions...')

        all_data = {sem: {"regular": None, "supplies": []} for sem in range(1, 9)}
        sem1_found_idx = -1

        for idx, exam_id in enumerate(exam_ids):
            if task_id in _task_cancel:
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return
            exam_name = exam_names.get(exam_id, str(exam_id))
            _update_task_state(task_id, step='scanning', message=f'Scanning sem 1 in {exam_name}...')
            res = get_marks_pdf(session, marks_page, str(exam_id), "1")
            if "pdf" not in res.headers.get("Content-Type", ""):
                continue
            parsed = parse_pdf(res.content, exam_name, 1)
            if parsed:
                all_data[1]["regular"] = {"exam": exam_name, "subjects": parsed}
                sem1_found_idx = idx
                sem_merged = _merge_sem(all_data[1]["regular"], all_data[1]["supplies"])
                _update_task_state(task_id, semester={'semester': 1, 'data': sem_merged})
                break

        if sem1_found_idx == -1:
            _update_task_state(task_id, status='error', error='Semester 1 not found')
            return

        # ---- free trial: show only sem 1, skip PDF ----
        if plan == 'free':
            merged = {'1': _merge_sem(all_data[1]["regular"], all_data[1]["supplies"])}
            _result_cache[regd_no] = (time.time(), merged)
            _update_task_state(task_id, status='done', data=merged, progress=100)
            return

        remaining = exam_ids[sem1_found_idx:]
        highest_found = 1
        total = len(remaining) * 8
        done = 0

        for exam_id in remaining:
            if task_id in _task_cancel:
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return
            exam_name = exam_names.get(exam_id, str(exam_id))
            start = 1 if highest_found % 2 == 1 else 2
            sems_to_check = list(range(start, highest_found + 1, 2))
            if highest_found + 1 <= 8:
                sems_to_check.append(highest_found + 1)
            for sem in sems_to_check:
                done += 1
                progress = int((done / total) * 100)
                _update_task_state(task_id, progress=progress, message=f'Scanning {exam_name} sem {sem}...')
                time.sleep(3)
                res = get_marks_pdf(session, marks_page, str(exam_id), str(sem))
                if "pdf" not in res.headers.get("Content-Type", ""):
                    marks_page = res
                    continue
                parsed = parse_pdf(res.content, exam_name, sem)
                if not parsed:
                    continue
                is_supply = len(parsed) < 5
                if not is_supply and all_data[sem]["regular"] is None:
                    all_data[sem]["regular"] = {"exam": exam_name, "subjects": parsed}
                    highest_found = max(highest_found, sem)
                    sem_merged = _merge_sem(all_data[sem]["regular"], all_data[sem]["supplies"])
                    _update_task_state(task_id, semester={'semester': sem, 'data': sem_merged})
                elif is_supply:
                    all_data[sem]["supplies"].append({"exam": exam_name, "subjects": parsed})
                    if all_data[sem]["regular"]:
                        sem_merged = _merge_sem(all_data[sem]["regular"], all_data[sem]["supplies"])
                        _update_task_state(task_id, semester={'semester': sem, 'data': sem_merged})

        merged = {str(k): _merge_sem(v["regular"], v["supplies"]) if v["regular"] else None for k, v in all_data.items()}
        _result_cache[regd_no] = (time.time(), merged)
        with _task_state_lock:
            if task_id in _task_state:
                _task_state[task_id]['semester'] = None
        _update_task_state(task_id, status='done', data=merged, progress=100)
        logger.info(f"Task {task_id}: done for {regd_no}")

        # save search history only on success
        with app.app_context():
            from models import SearchHistory
            hist = SearchHistory(user_id=user_id, regd_no=regd_no)
            db.session.add(hist)
            db.session.commit()

    except Exception as e:
        logger.error(f"Task {task_id}: error - {e}")
        _update_task_state(task_id, status='error', error='Scraping failed — please try again')


# start dispatcher
_dispatcher_thread = threading.Thread(target=_dispatcher, daemon=True)
_dispatcher_thread.start()

# ===================== APP =====================
app = Flask(__name__, instance_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance'))
CORS(app, origins=[
    'http://localhost:5000',
    'http://127.0.0.1:5000',
    'https://srbgnrcollegetracker.pythonanywhere.com',
    'https://srbgnr-college-tracker.vercel.app',
], supports_credentials=True)

# ===================== CONFIG =====================
app.config.from_object(Config)

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===================== INIT =====================
db.init_app(app)
bcrypt.init_app(app)
mail.init_app(app)
migrate.init_app(app, db)
limiter.init_app(app)

with app.app_context():
    init_serializer(app.config['SECRET_KEY'])

# ===================== LOGIN =====================
login_manager = LoginManager(app)
login_manager.login_view = 'auth.login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ===================== BLUEPRINTS =====================
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)

# ===================== ERROR HANDLERS =====================
@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({'error': 'Internal server error'}), 500


@app.errorhandler(429)
def ratelimit_error(e):
    return jsonify({'error': 'Rate limit exceeded. Please slow down.'}), 429


# ===================== HELPERS =====================
def get_active_subscription(user):
    sub = Subscription.query.filter_by(
        user_id=user.id,
        is_active=True
    ).order_by(Subscription.id.desc()).first()

    if not sub:
        return None

    if (
        sub.plan == 'monthly'
        and sub.end_date
        and sub.end_date < datetime.now()
    ):
        sub.is_active = False
        db.session.commit()
        return None

    return sub


def get_searches_today(user):
    today = datetime.now().date()
    return SearchHistory.query.filter(
        SearchHistory.user_id == user.id,
        db.func.date(SearchHistory.searched_at) == today
    ).count()

# ===================== HOME =====================
@app.route('/')
@login_required
def index():
    if current_user.is_admin:
        return redirect(url_for('admin.admin_panel'))

    sub = get_active_subscription(current_user)
    searches_today = get_searches_today(current_user)

    plan = get_user_plan(current_user)
    limits = {'free': 1, 'basic': 1, 'monthly': 4, 'yearly': 8}
    daily_limit = limits.get(plan, 0)
    if plan == 'basic':
        total = SearchHistory.query.filter_by(user_id=current_user.id).count()
        searches_left = max(0, daily_limit - total)
    else:
        searches_left = max(0, daily_limit - searches_today)

    return render_template(
        'index.html',
        user=current_user,
        sub=sub,
        plan=plan,
        searches_today=searches_today,
        searches_left=searches_left
    )

# ===================== SEARCH LIMIT =====================
@app.route('/searches-remaining')
@login_required
def searches_remaining():
    count = get_searches_today(current_user)
    return jsonify({
        'searches_today': count,
        'searches_left': max(0, 5 - count)
    })

# ===================== SUBSCRIBE =====================
@app.route('/subscribe')
@login_required
def subscribe():
    sub = get_active_subscription(current_user)
    if sub:
        return redirect(url_for('index'))

    return render_template('subscribe.html', upi_id=app.config['UPI_ID'])

# ===================== PAYMENT =====================
@app.route('/submit-payment', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def submit_payment():
    utr = request.form.get('utr').strip()
    plan = request.form.get('plan')

    amounts = {'basic': 15.0, 'monthly': 49.0, 'yearly': 129.0}
    amount = amounts.get(plan, 0)

    payment = Payment(
        user_id=current_user.id,
        utr=utr,
        amount=amount,
        plan=plan
    )

    db.session.add(payment)
    db.session.commit()

    flash("Payment submitted!", "success")
    return redirect(url_for('account'))

# ===================== ACCOUNT =====================
@app.route('/account')
@login_required
def account():
    sub = get_active_subscription(current_user)
    searches_today = get_searches_today(current_user)

    searches = SearchHistory.query.filter_by(
        user_id=current_user.id
    ).order_by(SearchHistory.searched_at.desc()).limit(20).all()

    payments = Payment.query.filter_by(
        user_id=current_user.id
    ).order_by(Payment.submitted_at.desc()).all()

    return render_template(
        'account.html',
        sub=sub,
        searches_today=searches_today,
        searches=searches,
        payments=payments
    )

# =========================================================
# ===================== MERGE HELPER =====================
def _merge_sem(reg, supplies):
    if not reg:
        return None
    subjects = []
    remaining = 0
    for subj in reg["subjects"]:
        entry = dict(subj)
        if entry["status"] == "fail":
            passed_in = None
            for supply in supplies:
                for s in supply["subjects"]:
                    if s["subject"] == subj["subject"] and s["status"] == "pass":
                        passed_in = supply["exam"]
                        break
                if passed_in:
                    break
            if passed_in:
                entry["status"] = "pass"
                entry["supply_passed"] = passed_in
            else:
                remaining += 1
        subjects.append(entry)
    return {
        "exam": reg["exam"],
        "subjects": subjects,
        "remaining_supplies": remaining,
        "supply_history": [
            {"exam": s["exam"], "subject_count": len(s["subjects"])} for s in supplies
        ]
    }

# =========================================================
@app.route('/get_results', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def get_results():
    ok, msg = check_search_limit(current_user)
    if not ok:
        return jsonify({'error': msg}), 429

    data = request.json
    regd_no = data.get("regd_no", "").strip()
    password = data.get("password", "0").strip()

    if not regd_no:
        return jsonify({'error': 'Registration number is required'}), 400

    priority = get_plan_priority(current_user)
    task_id = _next_task_id()
    counter = _task_id_counter
    plan = get_user_plan(current_user)

    with _task_queue_lock:
        with _task_state_lock:
            queue_pos = len(_task_queue)
            _task_queue.append((priority, counter, task_id, regd_no, password, current_user.id, plan))
            if task_id not in _task_state:
                _task_state[task_id] = {
                    'status': 'queued', 'progress': 0, 'step': 'queued',
                    'message': f'Position in queue: {queue_pos + 1}', 'semester': None,
                    'data': None, 'error': None, 'regd_no': regd_no, 'user_id': current_user.id
                }
            _task_state[task_id]['queue_position'] = queue_pos + 1

    return jsonify({'task_id': task_id})


@app.route('/task_status/<task_id>')
@login_required
@limiter.limit("120 per minute")
def task_status(task_id):
    with _task_state_lock:
        state = _task_state.get(task_id)
        if not state:
            return jsonify({'error': 'Task not found'}), 404
        if state.get('user_id') != current_user.id and not current_user.is_admin:
            return jsonify({'error': 'Forbidden'}), 403
        resp = {k: state[k] for k in ('status', 'progress', 'step', 'message', 'semester', 'data', 'error')}
        resp['queue_position'] = state.get('queue_position', 0)
    return jsonify(resp)


@app.route('/cancel_task/<task_id>', methods=['POST'])
@login_required
@limiter.limit("20 per minute")
def cancel_task(task_id):
    _task_cancel.add(task_id)
    with _task_queue_lock:
        _task_queue[:] = [t for t in _task_queue if t[2] != task_id]
    with _task_state_lock:
        state = _task_state.get(task_id)
        if not state:
            return jsonify({'error': 'Task not found'}), 404
        if state.get('user_id') != current_user.id and not current_user.is_admin:
            return jsonify({'error': 'Forbidden'}), 403
        if state['status'] in ('queued',):
            state['status'] = 'cancelled'
    return jsonify({'ok': True})


# ===================== CREATE ADMIN =====================
def create_admin():
    with app.app_context():
        db.create_all()

        if not User.query.filter_by(email=app.config['ADMIN_EMAIL']).first():
            admin = User(
                name="Admin",
                email=app.config['ADMIN_EMAIL'],
                password=bcrypt.generate_password_hash(
                    app.config['ADMIN_PASSWORD']
                ).decode('utf-8'),
                regd_no="0000000",
                is_admin=True
            )

            db.session.add(admin)
            db.session.commit()
            print("Admin created!")

# ===================== RUN =====================
if __name__ == "__main__":
    create_admin()
    app.run(debug=False, threaded=True)