import os
import logging
import time
import json
import uuid
import threading
from datetime import datetime, date
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    get_marks_pdf_fresh,
    parse_pdf,
    get_exam_ids_for_student,
    scrape_exam_options,
    parse_exam_date,
)

_CACHE_TTL = 300
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


def _delete_search_history(user_id, regd_no):
    try:
        with app.app_context():
            SearchHistory.query.filter_by(user_id=user_id, regd_no=regd_no).delete()
            db.session.commit()
    except Exception:
        pass


def _run_scrape(task_id, regd_no, password, user_id, plan):
    logger.info(f"Task {task_id}: scraping {regd_no} (plan={plan})")
    try:
        if task_id in _task_cancel:
            _delete_search_history(user_id, regd_no)
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
        exam_date_cache = {eid: parse_exam_date(n) for eid, n in exam_names.items()}

        # ── Pattern-driven scanning ──
        from models import SemesterPattern
        import re
        joining_year = None
        if len(regd_no) > 4 and regd_no[2:4].isdigit():
            joining_year = int(regd_no[2:4])
        else:
            m = re.search(r'(\d{2})', regd_no)
            joining_year = int(m.group(1)) if m else None
        patterns = []
        if joining_year:
            with app.app_context():
                patterns = SemesterPattern.query.filter_by(joining_year=joining_year).all()

        logger.info(f"Task {task_id}: joining_year={joining_year}, patterns={len(patterns)}, regd_no={regd_no}, marks_page.url={marks_page.url}")

        from bs4 import BeautifulSoup
        marks_page_text = marks_page.text
        marks_soup = BeautifulSoup(marks_page_text, "html.parser")
        hf_stud_id_el = marks_soup.find("input", {"name": "ctl00$ContentPlaceHolder1$hfStudId"})
        hf_stud_id = hf_stud_id_el["value"] if hf_stud_id_el is not None else ""
        if not hf_stud_id:
            logger.warning(f"Task {task_id}: hfStudId not found in marks_page, trying fallback")
            from scraper import _val
            try:
                hf_stud_id = _val(marks_soup, "ctl00$ContentPlaceHolder1$hfStudId")
            except Exception:
                hf_stud_id = ""

        if patterns:
            logger.info(f"Task {task_id}: patterns={len(patterns)}, joining_year={joining_year}")
            _update_task_state(task_id, step='scanning', message=f'Scanning {len(patterns)} entries...', progress=5)
            all_data = {sem: {"regular": None, "supplies": []} for sem in range(1, 9)}

            if task_id in _task_cancel:
                _delete_search_history(user_id, regd_no)
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return

            # Pass 1: Regular patterns only — parallel with fresh VIEWSTATE per thread
            regulars = [p for p in patterns if not p.is_supply]
            regular_tasks = []
            for p in regulars:
                match = next((eid for eid, n in exam_names.items() if n.upper() == p.exam_name.upper()), None)
                if match is not None:
                    regular_tasks.append((p, match))

            done = 0
            total_reg = len(regular_tasks) or 1
            with ThreadPoolExecutor(max_workers=5) as pool:
                fut_to_p = {pool.submit(get_marks_pdf_fresh, session, marks_page.url, str(m), str(p.semester), hf_stud_id): p for p, m in regular_tasks}
                for fut in as_completed(fut_to_p):
                    if task_id in _task_cancel:
                        _delete_search_history(user_id, regd_no)
                        _update_task_state(task_id, status='cancelled', message='Cancelled')
                        _task_cancel.discard(task_id)
                        return
                    p = fut_to_p[fut]
                    done += 1
                    pct = min(5 + int(done / total_reg * 80), 85)
                    _update_task_state(task_id, progress=pct, message=f'{p.exam_name} sem {p.semester}...')
                    try:
                        res = fut.result()
                        if "pdf" in res.headers.get("Content-Type", ""):
                            parsed = parse_pdf(res.content, p.exam_name, p.semester)
                            if parsed:
                                all_data[p.semester]["regular"] = {"exam": p.exam_name, "subjects": parsed}
                                sem_merged = _merge_sem(all_data[p.semester]["regular"], all_data[p.semester]["supplies"])
                                _update_task_state(task_id, semester={'semester': p.semester, 'data': sem_merged})
                    except Exception as e:
                        logger.warning(f"Task {task_id}: regular fetch failed for {p.exam_name} sem {p.semester}: {e}")

            # Check which semesters have failures
            failed_sems = set()
            for sem, d in all_data.items():
                if d["regular"] and any(s["status"] == "fail" for s in d["regular"]["subjects"]):
                    failed_sems.add(sem)

            if task_id in _task_cancel:
                _delete_search_history(user_id, regd_no)
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return

            # Pass 2: Supply patterns only for semesters with failures — parallel
            supplies = [p for p in patterns if p.is_supply and p.semester in failed_sems]
            supply_tasks = []
            for p in supplies:
                match = next((eid for eid, n in exam_names.items() if n.upper() == p.exam_name.upper()), None)
                if match is not None:
                    supply_tasks.append((p, match))

            done = 0
            total_sup = len(supply_tasks) or 1
            with ThreadPoolExecutor(max_workers=5) as pool:
                fut_to_p = {pool.submit(get_marks_pdf_fresh, session, marks_page.url, str(m), str(p.semester), hf_stud_id): p for p, m in supply_tasks}
                for fut in as_completed(fut_to_p):
                    if task_id in _task_cancel:
                        _delete_search_history(user_id, regd_no)
                        _update_task_state(task_id, status='cancelled', message='Cancelled')
                        _task_cancel.discard(task_id)
                        return
                    p = fut_to_p[fut]
                    done += 1
                    pct = min(85 + int(done / total_sup * 10), 90)
                    _update_task_state(task_id, progress=pct, message=f'{p.exam_name} supply sem {p.semester}...')
                    try:
                        res = fut.result()
                        if "pdf" in res.headers.get("Content-Type", ""):
                            parsed = parse_pdf(res.content, p.exam_name, p.semester)
                            if parsed:
                                all_data[p.semester]["supplies"].append({"exam": p.exam_name, "subjects": parsed})
                                if all_data[p.semester]["regular"]:
                                    sem_merged = _merge_sem(all_data[p.semester]["regular"], all_data[p.semester]["supplies"])
                                    _update_task_state(task_id, semester={'semester': p.semester, 'data': sem_merged})
                    except Exception as e:
                        logger.warning(f"Task {task_id}: supply fetch failed for {p.exam_name} sem {p.semester}: {e}")

            merged = {str(k): _merge_sem(v["regular"], v["supplies"]) if v["regular"] else None for k, v in all_data.items()}
            _result_cache[regd_no] = (time.time(), merged)
            _update_task_state(task_id, status='done', data=merged, progress=100)
            logger.info(f"Task {task_id}: pattern-done for {regd_no}")
            return

        # ── Fallback: auto-discovery scanning ──
        _update_task_state(task_id, step='scanning', message=f'Scanning {len(exam_ids)} exam sessions...')

        all_data = {sem: {"regular": None, "supplies": []} for sem in range(1, 9)}
        sem1_found_idx = -1

        for idx, exam_id in enumerate(exam_ids):
            if task_id in _task_cancel:
                _delete_search_history(user_id, regd_no)
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return
            exam_name = exam_names.get(exam_id, str(exam_id))
            _update_task_state(task_id, step='scanning', message=f'Scanning sem 1 in {exam_name}...')
            res = get_marks_pdf(session, marks_page, str(exam_id), "1")
            content_type = res.headers.get("Content-Type", "")
            logger.info(f"Task {task_id}: sem1 scan exam_id={exam_id} exam={exam_name} content_type={content_type}")
            if "pdf" not in content_type:
                marks_page = res
                continue
            parsed = parse_pdf(res.content, exam_name, 1)
            if parsed:
                logger.info(f"Task {task_id}: sem 1 found in {exam_name}")
                all_data[1]["regular"] = {"exam": exam_name, "subjects": parsed}
                sem1_found_idx = idx
                sem_merged = _merge_sem(all_data[1]["regular"], all_data[1]["supplies"])
                _update_task_state(task_id, semester={'semester': 1, 'data': sem_merged})
                break

        if sem1_found_idx == -1:
            _delete_search_history(user_id, regd_no)
            logger.warning(f"Task {task_id}: semester 1 not found after checking {len(exam_ids)} exams")
            _update_task_state(task_id, status='error', error='Semester 1 not found')
            return

        # ---- free trial: show only sem 1, skip PDF ----
        if plan == 'free':
            merged = {'1': _merge_sem(all_data[1]["regular"], all_data[1]["supplies"])}
            _result_cache[regd_no] = (time.time(), merged)
            _update_task_state(task_id, status='done', data=merged, progress=100)
            return

        sem1_date = exam_date_cache.get(exam_ids[sem1_found_idx])
        if sem1_date:
            remaining = [eid for eid in exam_ids
                if (d := exam_date_cache.get(eid)) is not None and d >= sem1_date]
        else:
            remaining = exam_ids[sem1_found_idx:]
        if sem1_date:
            remaining.sort(key=lambda eid: exam_date_cache.get(eid) or date.max)
        x_date = sem1_date
        highest_found = 1
        total_remaining = len(remaining)

        for exam_idx, exam_id in enumerate(remaining):
            if task_id in _task_cancel:
                _delete_search_history(user_id, regd_no)
                _update_task_state(task_id, status='cancelled', message='Cancelled')
                _task_cancel.discard(task_id)
                return
            exam_name = exam_names.get(exam_id, str(exam_id))
            curr_date = exam_date_cache.get(exam_id)
            if x_date and curr_date and curr_date != x_date:
                gap = (curr_date.year - x_date.year) * 12 + (curr_date.month - x_date.month)
                if gap < 4:
                    continue

            start = 1 if highest_found % 2 == 1 else 2
            sems_to_check = list(range(start, highest_found + 1, 2))
            if highest_found + 1 <= 8:
                sems_to_check.append(highest_found + 1)
            sems_to_check = [s for s in sems_to_check if all_data[s]["regular"] is None]

            if not sems_to_check:
                if curr_date:
                    x_date = curr_date
                continue

            _update_task_state(task_id, message=f'Scanning {exam_name}...')

            found_new_regular = False
            with ThreadPoolExecutor(max_workers=5) as pool:
                fut_to_sem = {pool.submit(get_marks_pdf_fresh, session, marks_page.url, str(exam_id), str(sem), hf_stud_id): sem for sem in sems_to_check}
                for fut in as_completed(fut_to_sem):
                    sem = fut_to_sem[fut]
                    try:
                        res = fut.result()
                        if "pdf" not in res.headers.get("Content-Type", ""):
                            continue
                        parsed = parse_pdf(res.content, exam_name, sem)
                        if not parsed:
                            continue
                        is_supply = len(parsed) < 5
                        if not is_supply:
                            all_data[sem]["regular"] = {"exam": exam_name, "subjects": parsed}
                            highest_found = max(highest_found, sem)
                            found_new_regular = True
                            sem_merged = _merge_sem(all_data[sem]["regular"], all_data[sem]["supplies"])
                            _update_task_state(task_id, semester={'semester': sem, 'data': sem_merged})
                        else:
                            all_data[sem]["supplies"].append({"exam": exam_name, "subjects": parsed})
                            if all_data[sem]["regular"]:
                                sem_merged = _merge_sem(all_data[sem]["regular"], all_data[sem]["supplies"])
                                _update_task_state(task_id, semester={'semester': sem, 'data': sem_merged})
                    except Exception as e:
                        logger.warning(f"Task {task_id}: auto-detect failed for {exam_name} sem {sem}: {e}")

            if found_new_regular:
                supply_sems = []
                for ss in range(1, highest_found):
                    if ss in sems_to_check:
                        continue
                    if all_data[ss]["regular"] is None:
                        continue
                    supply_sems.append(ss)

                if supply_sems:
                    with ThreadPoolExecutor(max_workers=5) as pool:
                        fut_to_ss = {pool.submit(get_marks_pdf_fresh, session, marks_page.url, str(exam_id), str(ss), hf_stud_id): ss for ss in supply_sems}
                        for fut in as_completed(fut_to_ss):
                            ss = fut_to_ss[fut]
                            try:
                                res = fut.result()
                                if "pdf" in res.headers.get("Content-Type", ""):
                                    parsed = parse_pdf(res.content, exam_name, ss)
                                    if parsed and len(parsed) < 5:
                                        all_data[ss]["supplies"].append({"exam": exam_name, "subjects": parsed})
                                        sem_merged = _merge_sem(all_data[ss]["regular"], all_data[ss]["supplies"])
                                        _update_task_state(task_id, semester={'semester': ss, 'data': sem_merged})
                            except Exception as e:
                                logger.warning(f"Task {task_id}: auto-detect supply failed for {exam_name} sem {ss}: {e}")

            if curr_date:
                x_date = curr_date

            pct = min(85 + int((exam_idx + 1) / total_remaining * 10), 90)
            _update_task_state(task_id, progress=pct)

        merged = {str(k): _merge_sem(v["regular"], v["supplies"]) if v["regular"] else None for k, v in all_data.items()}
        _result_cache[regd_no] = (time.time(), merged)
        with _task_state_lock:
            if task_id in _task_state:
                _task_state[task_id]['semester'] = None
        _update_task_state(task_id, status='done', data=merged, progress=100)
        logger.info(f"Task {task_id}: done for {regd_no}")

    except Exception as e:
        _delete_search_history(user_id, regd_no)
        logger.exception(f"Task {task_id}: error")
        _update_task_state(task_id, status='error', error=f'Scraping failed: {e}')


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
@app.route('/generate-pdf', methods=['POST'])
@login_required
def generate_pdf():
    try:
        import json
        from io import BytesIO
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm, cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        raw = request.form.get("data", "{}")
        regd_no = request.form.get("regd_no", "").strip()
        data = json.loads(raw)

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("Title2", parent=styles["Heading1"],
                                     fontSize=16, spaceAfter=6, alignment=TA_CENTER,
                                     textColor=colors.HexColor("#d4405e"))
        sem_style = ParagraphStyle("Sem", parent=styles["Heading2"],
                                   fontSize=13, spaceAfter=4, spaceBefore=12,
                                   textColor=colors.HexColor("#333"))
        cell_style = ParagraphStyle("Cell", fontSize=9, leading=12, alignment=TA_LEFT)
        center_style = ParagraphStyle("Center", parent=cell_style, alignment=TA_CENTER)

        elements = []
        elements.append(Paragraph(f"SR & BGNR College Tracker", title_style))
        elements.append(Paragraph(f"Registration No: {regd_no}", ParagraphStyle(
            "Sub", parent=styles["Normal"], fontSize=10, alignment=TA_CENTER,
            textColor=colors.HexColor("#666"), spaceAfter=8)))
        elements.append(Spacer(1, 4*mm))

        sorted_sems = sorted((int(k), v) for k, v in data.items() if v)
        for sem_num, sem_data in sorted_sems:
            elements.append(Paragraph(f"Semester {sem_num} — {sem_data.get('exam', 'N/A')}", sem_style))
            subjects = sem_data.get("subjects", [])
            if not subjects:
                elements.append(Paragraph("No data available", styles["Normal"]))
                continue

            header = ["#", "Subject", "Marks", "Status"]
            rows = [header]
            for i, s in enumerate(subjects, 1):
                status = s.get("status", "")
                marks = s.get("marks", "")
                if status == "pass":
                    status_text = "PASS"
                elif s.get("supply_passed"):
                    status_text = f"PASS (Supply)"
                else:
                    status_text = "FAIL"
                rows.append([
                    Paragraph(str(i), center_style),
                    Paragraph(s.get("subject", ""), cell_style),
                    Paragraph(str(marks), center_style),
                    Paragraph(status_text, center_style),
                ])

            col_w = [12*mm, 80*mm, 25*mm, 35*mm]
            table = Table(rows, colWidths=col_w, repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d4405e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, 0), 10),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ddd")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
            ]))
            elements.append(table)
            elements.append(Spacer(1, 3*mm))

        doc.build(elements)
        buf.seek(0)

        filename = f"SR-BGNR-{regd_no}.pdf"
        return Response(
            buf.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.exception("generate-pdf error")
        return jsonify({"error": str(e)}), 500


# =========================================================
@app.route('/get_results', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def get_results():
    data = request.json
    regd_no = data.get("regd_no", "").strip()
    password = data.get("password", "0").strip()

    if not regd_no:
        return jsonify({'error': 'Registration number is required'}), 400

    ok, msg = check_search_limit(current_user)
    if not ok:
        return jsonify({'error': msg}), 429

    hist = SearchHistory(user_id=current_user.id, regd_no=regd_no)
    db.session.add(hist)
    db.session.commit()

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