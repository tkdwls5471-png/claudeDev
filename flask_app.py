import io
import os
import json
import hashlib
import logging
from datetime import timedelta

import pandas as pd
from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from sqlalchemy import func, update

import timeutil
import email_service as es
import analysis_worker
from db import (
    init_db, SessionLocal,
    User, EmailAccount, Email, Setting,
    encrypt, decrypt, hash_password, check_password,
)
from eml_parser import parse_eml
from attachment_store import save_original, read_original, content_type_for
from imap_connector import test_connection, fetch_email_by_uid
import scheduler as sched

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'eml-secret-2026')
# 템플릿(index.html 등) 수정 시 서버 재시작 없이 반영되도록 자동 리로드
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

def _static_version():
    """정적 파일 캐시 무효화용 버전: static/ 아래 파일의 가장 최근 수정 시각 (배포하면 바뀜)."""
    latest = 0
    for dirpath, _, files in os.walk(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')):
        for f in files:
            latest = max(latest, os.path.getmtime(os.path.join(dirpath, f)))
    return str(int(latest))


STATIC_VERSION = os.environ.get('STATIC_VERSION') or _static_version()


@app.context_processor
def _inject_version():
    return {'static_v': STATIC_VERSION}


# ─── Flask-Login ──────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login_page'


class AuthUser(UserMixin):
    def __init__(self, user: User):
        self.id = user.id
        self.username = user.username


@login_manager.user_loader
def load_user(user_id):
    db = SessionLocal()
    try:
        user = db.get(User, int(user_id))
        return AuthUser(user) if user else None
    finally:
        db.close()


@login_manager.unauthorized_handler
def _unauthorized():
    """API는 302 대신 401 JSON → 프론트가 로그인 화면으로 보낸다."""
    if request.path.startswith('/api/') or request.path.startswith('/download'):
        return jsonify(success=False, message='로그인이 필요합니다', login_required=True), 401
    return redirect(url_for('login_page'))


# ─── App 초기화 ──────────────────────────────────────────────────────────────

with app.app_context():
    init_db()
    sched.start_background()


# ─── 공용 헬퍼 ───────────────────────────────────────────────────────────────

def _owned(db, email_id):
    return es.owned_email(db, current_user.id, email_id)


def _not_found():
    return jsonify(success=False, message='메일을 찾을 수 없습니다'), 404


def _row(db, e):
    return es.to_row(e, es.account_kinds(db, current_user.id))


def _int_arg(name, default, lo=None, hi=None):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _load_raw(db, e):
    """원본 EML 바이트: 업로드는 디스크, IMAP은 메일 서버에서 (없으면 None)."""
    if e.raw_stored:
        return read_original(e.raw_stored)
    uid = es.imap_uid(e)
    acc = db.get(EmailAccount, e.account_id)
    if not uid or acc is None or (acc.kind or 'imap') != 'imap':
        return None
    return fetch_email_by_uid(acc.imap_server, acc.imap_port, acc.imap_username,
                              decrypt(acc.imap_password_enc), uid, acc.use_ssl)


# ─── 페이지 라우트 ─────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('login.html')


# ─── 인증 API ─────────────────────────────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify(success=False, message='아이디와 비밀번호를 입력하세요'), 400

    db = SessionLocal()
    try:
        if db.query(User).filter_by(username=username).first():
            return jsonify(success=False, message='이미 사용 중인 아이디입니다'), 409
        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        login_user(AuthUser(user))
        return jsonify(success=True, username=user.username)
    finally:
        db.close()


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).first()
        if not user or not check_password(password, user.password_hash):
            return jsonify(success=False, message='아이디 또는 비밀번호가 틀렸습니다'), 401
        login_user(AuthUser(user), remember=True)
        return jsonify(success=True, username=user.username)
    finally:
        db.close()


@app.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify(success=True)


@app.route('/api/me')
@login_required
def me():
    return jsonify(id=current_user.id, username=current_user.username)


# ─── 이메일 계정 관리 ─────────────────────────────────────────────────────────

def _account_json(a):
    return {
        'id': a.id,
        'label': a.label,
        'email_address': a.email_address,
        'imap_server': a.imap_server,
        'imap_port': a.imap_port,
        'last_synced': timeutil.to_kst_str(a.last_synced),
        'last_sync_status': a.last_sync_status,
        'last_sync_error': a.last_sync_error,
        'last_sync_new': a.last_sync_new,
        'syncing': sched.account_syncing(a.id),
    }


def _imap_account(db, account_id):
    return db.query(EmailAccount).filter_by(
        id=account_id, user_id=current_user.id, kind='imap').first()


@app.route('/api/accounts', methods=['GET'])
@login_required
def list_accounts():
    db = SessionLocal()
    try:
        accounts = db.query(EmailAccount).filter_by(
            user_id=current_user.id, is_active=True, kind='imap').all()
        return jsonify([_account_json(a) for a in accounts])
    finally:
        db.close()


@app.route('/api/accounts', methods=['POST'])
@login_required
def add_account():
    data = request.get_json() or {}
    required = ('email_address', 'imap_server', 'imap_port', 'imap_username', 'imap_password')
    if not all(data.get(k) for k in required):
        return jsonify(success=False, message='필수 항목이 누락되었습니다'), 400

    server  = data['imap_server']
    try:
        port = int(data['imap_port'])
    except (TypeError, ValueError):
        return jsonify(success=False, message='포트 번호가 올바르지 않습니다'), 400
    user    = data['imap_username']
    pw      = data['imap_password']
    use_ssl = data.get('use_ssl', True)

    ok, err = test_connection(server, port, user, pw, use_ssl)
    if not ok:
        return jsonify(success=False, message=f'연결 실패: {err}'), 400

    # 연결 테스트 전용 요청: 계정을 생성하지 않고 연결 성공 여부만 반환.
    if data.get('_test_only'):
        return jsonify(success=True, message='연결 성공')

    db = SessionLocal()
    try:
        # 동일 계정 중복 방지 (더블 서브밋/재등록 방어)
        existing = db.query(EmailAccount).filter_by(
            user_id=current_user.id, email_address=data['email_address'], kind='imap',
        ).first()
        if existing and existing.is_active:
            return jsonify(success=False, message='이미 등록된 계정입니다'), 409
        if existing:  # 이전에 삭제(비활성)된 계정이면 재활성화 + 자격증명 갱신
            existing.label             = data.get('label') or data['email_address']
            existing.imap_server       = server
            existing.imap_port         = port
            existing.imap_username     = user
            existing.imap_password_enc = encrypt(pw)
            existing.use_ssl           = use_ssl
            existing.is_active         = True
            existing.last_uid          = 0      # 최근 메일부터 다시 받음 (이미 있는 메일은 중복 제외)
            existing.uidvalidity       = None
            acc_id = existing.id
        else:
            acc = EmailAccount(
                user_id=current_user.id,
                label=data.get('label') or data['email_address'],
                email_address=data['email_address'],
                imap_server=server,
                imap_port=port,
                imap_username=user,
                imap_password_enc=encrypt(pw),
                use_ssl=use_ssl,
                kind='imap',
            )
            db.add(acc)
            db.flush()
            acc_id = acc.id
        db.commit()
    finally:
        db.close()
    sched.clear_login_backoff(acc_id)
    sched.request_sync(current_user.id)
    return jsonify(success=True, id=acc_id)


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def delete_account(account_id):
    db = SessionLocal()
    try:
        acc = _imap_account(db, account_id)
        if not acc:
            return jsonify(success=False, message='계정을 찾을 수 없습니다'), 404
        acc.is_active = False
        db.commit()
        return jsonify(success=True)
    finally:
        db.close()


@app.route('/api/accounts/<int:account_id>/test', methods=['POST'])
@login_required
def test_account(account_id):
    db = SessionLocal()
    try:
        acc = _imap_account(db, account_id)
        if not acc:
            return jsonify(success=False, message='계정을 찾을 수 없습니다'), 404
        pw = decrypt(acc.imap_password_enc)
        ok, err = test_connection(acc.imap_server, acc.imap_port, acc.imap_username, pw, acc.use_ssl)
        if ok:
            sched.clear_login_backoff(acc.id)
        return jsonify(success=ok, message=err or '연결 성공')
    finally:
        db.close()


# ─── 동기화 / 처리 현황 ───────────────────────────────────────────────────────

@app.route('/api/sync', methods=['POST'])
@login_required
def manual_sync():
    """현재 사용자 계정을 백그라운드에서 바로 동기화 (결과는 /api/status)."""
    started = sched.request_sync(current_user.id)
    return jsonify(success=True, started=started, running=True)


@app.route('/api/status')
@login_required
def get_status():
    db = SessionLocal()
    try:
        uid = current_user.id
        accounts = db.query(EmailAccount).filter_by(user_id=uid, is_active=True, kind='imap').all()
        ids = es.user_account_ids(db, uid)
        counts = es.tab_counts(db.query(Email).filter(Email.account_id.in_(ids or [-1])), timeutil.kst_today())
        status_counts = dict(
            db.query(Email.analysis_status, func.count(Email.id))
            .filter(Email.account_id.in_(ids or [-1]), Email.analysis_status.in_(('queued', 'running')))
            .group_by(Email.analysis_status).all())
        cur = analysis_worker.current()
        running = None
        if cur and cur.get('user_id') == uid:
            running = {'id': cur['id'], 'subject': cur['subject']}
        return jsonify(
            sync={
                'running': sched.user_syncing(uid) or any(sched.account_syncing(a.id) for a in accounts),
                'interval_min': int(os.environ.get('SYNC_INTERVAL_MIN', 2)),
                'accounts': [{
                    'id': a.id, 'label': a.label, 'email_address': a.email_address,
                    'last_sync_at': timeutil.to_kst_str(a.last_synced),
                    'status': a.last_sync_status, 'error': a.last_sync_error,
                    'new_count': a.last_sync_new,
                } for a in accounts],
            },
            analysis={
                'queued': int(status_counts.get('queued', 0)),
                'running': running,
                'running_count': int(status_counts.get('running', 0)),
                'failed': counts['failed'],
                'auto_enabled': es.auto_analyze_enabled(db, uid),
                'paused': analysis_worker.pause_info(uid),
            },
            counts=counts,
            latest_email_id=es.latest_email_id(db, ids),
        )
    finally:
        db.close()


# ─── 메일 목록 / 상세 ─────────────────────────────────────────────────────────

@app.route('/api/emails')
@login_required
def list_emails():
    tab = request.args.get('tab', 'todo')
    tab = tab if tab in es.TABS else 'todo'
    sort = request.args.get('sort') or ('deadline' if tab == 'due' else 'received')
    sort = sort if sort in es.SORTS else 'received'
    range_from = timeutil.parse_ymd(request.args.get('range_from'))
    range_to = timeutil.parse_ymd(request.args.get('range_to'))
    max_pp = 1000 if (range_from or range_to) else 200
    page = _int_arg('page', 1, lo=1)
    per_page = _int_arg('per_page', 50, lo=1, hi=max_pp)

    db = SessionLocal()
    try:
        accounts = es.user_accounts(db, current_user.id)
        ids = [a.id for a in accounts]
        source = request.args.get('source')
        if source in ('imap', 'upload'):
            ids = [a.id for a in accounts if (a.kind or 'imap') == source]
        account_id = request.args.get('account_id')
        if account_id:
            try:
                ids = [i for i in ids if i == int(account_id)]
            except ValueError:
                ids = []
        empty = {k: 0 for k in ('all', 'todo', 'due', 'done', 'excluded', 'failed', 'unread')}
        if not ids:
            return jsonify(total=0, page=page, per_page=per_page, counts=empty, emails=[])

        today = timeutil.kst_today()
        base = es.filtered_query(
            db, ids,
            q=(request.args.get('q') or '').strip(),
            priorities=[p for p in request.args.getlist('priority') if p],
            types=[t for t in request.args.getlist('type') if t],
            unread=request.args.get('unread') in ('1', 'true'),
            range_from=range_from, range_to=range_to,
        )
        counts = es.tab_counts(base, today)
        q = es.apply_tab_and_sort(base.options(*es.list_options()), tab, sort, today)
        total = counts[tab]
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        kinds = {a.id: (a.kind or 'imap') for a in accounts}
        return jsonify(total=total, page=page, per_page=per_page, counts=counts,
                       emails=[es.to_row(e, kinds) for e in rows])
    finally:
        db.close()


def _serialize_attachments(e):
    """첨부 목록에 프론트엔드용 메타(idx, has_original) 부가. 내부 저장 파일명은 노출하지 않음."""
    try:
        attachments = json.loads(e.attachments_json or '[]')
    except Exception:
        attachments = []
    fetchable = bool(es.imap_uid(e)) or bool(e.raw_stored)
    out = []
    for i, a in enumerate(attachments):
        a = dict(a)
        a['idx'] = i
        a['has_original'] = bool(a.get('stored')) or fetchable
        a.pop('stored', None)
        out.append(a)
    return out


@app.route('/api/emails/<int:email_id>')
@login_required
def get_email(email_id):
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return _not_found()
        if request.args.get('mark_read') == '1' and not e.is_read:
            e.is_read = True
            db.commit()
        html_len = db.query(func.length(Email.body_html)).filter(Email.id == e.id).scalar()
        row = _row(db, e)
        row.update({
            'recipient': e.recipient,
            'body_text': e.body_text,
            'analysis_result': e.analysis_result,
            'analysis_provider': e.analysis_provider,
            'tokens_in': e.analysis_tokens_in or 0,
            'tokens_out': e.analysis_tokens_out or 0,
            'attachments': _serialize_attachments(e),
            'has_html': None if html_len is None else html_len > 0,
            'can_reprocess': bool(es.imap_uid(e) or e.raw_stored),
        })
        return jsonify(row)
    finally:
        db.close()


@app.route('/api/emails/<int:email_id>', methods=['PATCH'])
@login_required
def patch_email(email_id):
    data = request.get_json() or {}
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return _not_found()
        if 'is_completed' in data:
            done = bool(data['is_completed'])
            if done != bool(e.is_completed):
                e.is_completed = done
                e.completed_at = timeutil.utcnow() if done else None
        if 'is_excluded' in data:
            e.is_excluded = bool(data['is_excluded'])
            e.exclude_reason = None          # 직접 바꾼 경우 규칙 표시를 지운다
            if e.is_excluded and e.analysis_status == 'queued':
                e.analysis_status, e.analysis_next_at = 'none', None
        if 'is_read' in data:
            e.is_read = bool(data['is_read'])
        if 'deadline' in data:
            raw = data.get('deadline')
            d = timeutil.parse_ymd(raw) if raw else None
            if raw and d is None:
                return jsonify(success=False, message='마감일 형식이 올바르지 않습니다 (YYYY-MM-DD)'), 400
            e.deadline, e.deadline_manual = d, True
        elif data.get('deadline_manual') is False:
            e.deadline_manual = False
            e.deadline = es.extract_deadline((e.analysis_result or {}).get('마감일'))
        db.commit()
        return jsonify(_row(db, e))
    finally:
        db.close()


@app.route('/api/emails/<int:email_id>', methods=['DELETE'])
@login_required
def delete_email(email_id):
    """앱에서 메일 삭제 (메일 서버의 원본은 그대로). 디스크 첨부도 함께 삭제."""
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return _not_found()
        es.delete_emails(db, [e])
        return jsonify(success=True)
    finally:
        db.close()


BULK_ACTIONS = ('complete', 'uncomplete', 'exclude', 'unexclude', 'read', 'unread',
                'delete', 'analyze', 'cancel_analysis')


@app.route('/api/emails/bulk', methods=['POST'])
@login_required
def bulk_emails():
    data = request.get_json() or {}
    action = data.get('action')
    if action not in BULK_ACTIONS:
        return jsonify(success=False, message='알 수 없는 작업입니다'), 400
    try:
        req_ids = [int(i) for i in (data.get('ids') or [])][:5000]
    except (TypeError, ValueError):
        return jsonify(success=False, message='잘못된 메일 ID'), 400

    db = SessionLocal()
    try:
        acc_ids = es.user_account_ids(db, current_user.id, active_only=False)
        if not req_ids or not acc_ids:
            return jsonify(success=True, affected=0)
        ids = [i for (i,) in db.query(Email.id).filter(Email.id.in_(req_ids), Email.account_id.in_(acc_ids))]
        if not ids:
            return jsonify(success=True, affected=0)

        def upd(where=None, **values):
            stmt = update(Email).where(Email.id.in_(ids))
            if where is not None:
                stmt = stmt.where(where)
            r = db.execute(stmt.values(**values).execution_options(synchronize_session=False))
            db.commit()
            return r.rowcount

        now = timeutil.utcnow()
        if action == 'complete':
            n = upd(Email.is_completed == False, is_completed=True, completed_at=now)  # noqa: E712
        elif action == 'uncomplete':
            n = upd(Email.is_completed == True, is_completed=False, completed_at=None)  # noqa: E712
        elif action == 'exclude':
            n = upd(None, is_excluded=True, exclude_reason=None)
            upd(Email.analysis_status == 'queued', analysis_status='none', analysis_next_at=None)
        elif action == 'unexclude':
            n = upd(None, is_excluded=False, exclude_reason=None)
        elif action == 'read':
            n = upd(None, is_read=True)
        elif action == 'unread':
            n = upd(None, is_read=False)
        elif action == 'delete':
            n = es.delete_emails(db, db.query(Email).filter(Email.id.in_(ids)).all())
        elif action == 'analyze':
            n = es.queue_analysis(db, ids)
            analysis_worker.wake()
        else:  # cancel_analysis
            n = es.cancel_analysis(db, ids)
        return jsonify(success=True, affected=n)
    finally:
        db.close()


@app.route('/api/emails/<int:email_id>/analyze', methods=['POST'])
@login_required
def analyze_email(email_id):
    """분석(재분석) 요청: 대기열 맨 앞에 올리고 바로 반환."""
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return _not_found()
        es.queue_analysis(db, [e.id])
        db.refresh(e)
        analysis_worker.wake()
        return jsonify(_row(db, e))
    finally:
        db.close()


@app.route('/api/emails/<int:email_id>/reprocess', methods=['POST'])
@login_required
def reprocess_email_attachments(email_id):
    """원본을 다시 받아 첨부파일·본문 HTML을 최신 로직으로 재처리 (분석 상태는 유지)."""
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return jsonify(success=False, message='메일을 찾을 수 없습니다'), 404
        if not (es.imap_uid(e) or e.raw_stored):
            return jsonify(success=False, message='원본이 없어 다시 처리할 수 없습니다'), 400
        raw = _load_raw(db, e)
        if not raw:
            return jsonify(success=False, message='원본 메일을 가져오지 못했습니다 (서버에서 삭제되었을 수 있음)'), 404

        parsed = parse_eml(raw)
        old_atts = json.loads(e.attachments_json or '[]')
        atts, _ = es.build_attachments(parsed.get('attachments'))
        for a in old_atts:   # 이전 디스크 사본 정리
            if a.get('stored'):
                from attachment_store import delete_original
                delete_original(a['stored'])
        e.attachments_json = json.dumps(atts, ensure_ascii=False)
        e.has_attachments = bool(atts)
        html = parsed.get('body_html') or ''
        e.body_html = html if len(html) <= es.BODY_HTML_MAX else ''
        if parsed.get('received_date'):
            e.received_date = parsed['received_date'][:50]
        if parsed.get('received_at'):
            e.received_at = parsed['received_at']
        db.commit()
        return jsonify(success=True, attachments=_serialize_attachments(e))
    except Exception as ex:
        logger.error(f'[reprocess] {email_id}: {ex}')
        return jsonify(success=False, message=str(ex)), 500
    finally:
        db.close()


def _sanitize_email_html(html: str, allow_remote_images: bool) -> str:
    """외부에서 온 신뢰할 수 없는 메일 HTML을 안전하게 정제.
    (프론트는 스크립트가 꺼진 샌드박스 iframe + CSP로 한 번 더 막는다)
    - 실행 가능 요소(script/iframe/object/embed/form/base/link/meta refresh) 제거
    - on* 이벤트 핸들러, javascript: 링크 제거
    - 원격 이미지(http/https, //host, CSS url(), background 속성)는 기본 차단(추적 방지)
    - cid: 참조 이미지는 매핑하지 않으므로 빈 표시로 대체"""
    import re
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')

    for tag in soup(['script', 'iframe', 'object', 'embed', 'form', 'base', 'link']):
        tag.decompose()
    for tag in soup.find_all('meta'):
        if (tag.get('http-equiv') or '').lower() == 'refresh':
            tag.decompose()

    remote_url = re.compile(r'url\(\s*[\'"]?\s*(https?:)?//[^)]*\)', re.I)
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on'):
                del tag.attrs[attr]
        href = tag.attrs.get('href', '')
        if isinstance(href, str) and href.strip().lower().startswith(('javascript:', 'vbscript:', 'data:')):
            tag.attrs['href'] = '#'
        if tag.name == 'a' and tag.attrs.get('href'):
            tag.attrs['target'] = '_blank'
            tag.attrs['rel'] = 'noopener noreferrer'
        if not allow_remote_images:
            style = tag.attrs.get('style')
            if isinstance(style, str) and 'url(' in style.lower():
                tag.attrs['style'] = remote_url.sub('none', style)
            bg = tag.attrs.get('background')
            if isinstance(bg, str) and bg.strip().lower().startswith(('http:', 'https:', '//')):
                del tag.attrs['background']
    if not allow_remote_images:
        for st in soup.find_all('style'):
            if st.string:
                st.string.replace_with(remote_url.sub('none', st.string))

    for img in soup.find_all('img'):
        img.attrs.pop('srcset', None)  # src만 검사하면 srcset으로 원격 이미지 차단이 우회됨
        src = (img.attrs.get('src') or '').strip()
        if src.lower().startswith('cid:'):
            img.attrs['src'] = ''
            img.attrs['alt'] = img.attrs.get('alt') or '[인라인 이미지: 첨부파일 탭에서 확인]'
        elif src.lower().startswith(('http://', 'https://', '//')) and not allow_remote_images:
            img.attrs['data-blocked-src'] = src
            img.attrs['src'] = ''
            img.attrs['alt'] = '[원격 이미지 차단됨]'

    return str(soup)


@app.route('/api/emails/<int:email_id>/body-html')
@login_required
def get_email_body_html(email_id):
    """원본 HTML 본문 (DB 캐시 우선, 없으면 원본에서 1회 추출해 저장) → 정제 후 반환."""
    allow_images = request.args.get('images') == '1'
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return jsonify(html=None, message='메일을 찾을 수 없습니다'), 404
        html = e.body_html
        if html is None:
            raw = _load_raw(db, e)
            if raw is None:
                return jsonify(html=None, has_html=False, message='원본을 조회할 수 없습니다 (서버에서 삭제되었거나 원본이 없는 메일)')
            html = parse_eml(raw).get('body_html') or ''
            e.body_html = html if len(html) <= es.BODY_HTML_MAX else ''
            db.commit()
        if not html:
            return jsonify(html=None, has_html=False, message='HTML 본문이 없는 메일입니다')
        blocked = not allow_images
        return jsonify(html=_sanitize_email_html(html, allow_images), has_html=True, images_blocked=blocked)
    except Exception as ex:
        logger.error(f'[body-html] email={email_id} 조회 실패: {ex}')
        return jsonify(html=None, message=f'원본을 불러오지 못했습니다: {ex}'), 500
    finally:
        db.close()


@app.route('/api/emails/<int:email_id>/attachment/<int:idx>/raw')
@login_required
def get_attachment_raw(email_id, idx):
    """첨부파일 원본 (디스크 저장본 우선, 없으면 원본 메일에서 받아 디스크에 캐시).
    ?dl=1 이면 다운로드(첨부), 아니면 인라인(브라우저 표시)."""
    download = request.args.get('dl') == '1'
    db = SessionLocal()
    try:
        e = _owned(db, email_id)
        if not e:
            return jsonify(error='메일을 찾을 수 없습니다'), 404

        atts = json.loads(e.attachments_json or '[]')
        if idx < 0 or idx >= len(atts):
            return jsonify(error='첨부파일을 찾을 수 없습니다'), 404
        att = atts[idx]
        filename = att.get('filename') or f'attachment_{idx}'
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        data = read_original(att.get('stored'))
        if data is None:
            try:
                raw = _load_raw(db, e)
                if raw:
                    src = parse_eml(raw).get('attachments', [])
                    if idx < len(src) and src[idx].get('data'):
                        data = src[idx]['data']
                        meta = save_original(data, src[idx].get('extension') or ext)
                        if meta:
                            att['stored'] = meta['stored']
                            att['size'] = meta['size']
                            e.attachments_json = json.dumps(atts, ensure_ascii=False)
                            db.commit()
            except Exception as ex:
                logger.error(f'[attachment raw] 원본 조회 실패 email={email_id} idx={idx}: {ex}')

        if data is None:
            return jsonify(error='원본 파일을 가져오지 못했습니다 (서버에서 삭제되었을 수 있음)'), 404

        return send_file(
            io.BytesIO(data),
            mimetype=content_type_for(ext),
            as_attachment=download,
            download_name=filename,
            max_age=0,
        )
    finally:
        db.close()


# ─── EML 업로드 (DB 메일함에 저장 → 분석 대기열) ────────────────────────────

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_eml():
    f = request.files.get('file') or next(iter(request.files.getlist('files[]')), None)
    if f is None:
        return jsonify(success=False, message='파일이 없습니다'), 400
    filename = (f.filename or 'upload.eml')[:200]
    data = f.read()
    if not data:
        return jsonify(success=False, error={'filename': filename, 'message': '빈 파일입니다'}), 400

    db = SessionLocal()
    try:
        acc = es.get_or_create_upload_account(db, current_user.id)
        uid = 'upload-' + hashlib.sha1(data).hexdigest()
        existing = db.query(Email).filter_by(account_id=acc.id, message_uid=uid).first()
        if existing is None:
            # 이미 메일 계정으로 받은 메일이면 중복 (Message-ID 기준)
            try:
                mid = parse_eml(data).get('message_id')
            except Exception:
                mid = None
            if mid:
                ids = es.user_account_ids(db, current_user.id, active_only=False)
                existing = db.query(Email).filter(Email.account_id.in_(ids), Email.message_id == mid).first()
        if existing is not None:
            return jsonify(success=True, duplicate=_row(db, existing), filename=filename)

        meta = save_original(data, 'eml')
        email_id, status = es.ingest_message(
            db, acc, uid, data, queue_mode='force',
            raw_stored=meta['stored'] if meta else None,
            is_read=True, fallback_subject=filename)
        analysis_worker.wake()
        e = db.get(Email, email_id) if email_id else None
        if e is None:
            return jsonify(success=False, error={'filename': filename, 'message': '저장하지 못했습니다'}), 500
        if status == 'duplicate':
            return jsonify(success=True, duplicate=_row(db, e), filename=filename)
        if status == 'failed':
            return jsonify(success=False, created=_row(db, e), filename=filename,
                           error={'filename': filename, 'message': e.analysis_last_error or 'EML을 해석할 수 없습니다'})
        return jsonify(success=True, created=_row(db, e), filename=filename)
    finally:
        db.close()


@app.route('/api/upload/legacy', methods=['POST'])
@login_required
def import_legacy_uploads():
    """예전 버전이 브라우저(localStorage)에만 저장한 업로드 분석 결과를 1회 가져온다.
    원문은 없고 분석 결과만 옮긴다. 같은 항목은 다시 가져오지 않는다."""
    items = (request.get_json() or {}).get('items') or []
    db = SessionLocal()
    imported = 0
    try:
        acc = es.get_or_create_upload_account(db, current_user.id)
        for it in items[:2000]:
            res = it.get('result') if isinstance(it, dict) else None
            if not isinstance(res, dict):
                continue
            key = hashlib.sha1(json.dumps({'f': it.get('filename'), 't': it.get('timestamp'), 'r': res},
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            uid = 'legacy-' + key[:40]
            if db.query(Email.id).filter_by(account_id=acc.id, message_uid=uid).first():
                continue
            flags = {k: res.get(k) for k in ('완료', '제외')}
            result = {k: v for k, v in res.items() if k not in ('완료', '제외')}
            ts = timeutil.parse_kst(str(it.get('timestamp') or '').replace('T', ' ')[:16]) or timeutil.utcnow()
            received_at = timeutil.parse_kst(result.get('수신일')) or ts
            deadline = es.extract_deadline(result.get('마감일'))
            e = Email(
                account_id=acc.id, message_uid=uid,
                subject=str(it.get('filename') or '업로드 기록')[:500],
                sender=str(result.get('요청_업체') or '')[:500],
                received_date=timeutil.to_kst_str(received_at), received_at=received_at,
                body_text='', attachments_json='[]', body_html='',
                analysis_result=result, is_analyzed=True, analyzed_at=ts, analysis_status='done',
                is_read=True, is_completed=bool(flags.get('완료')), is_excluded=bool(flags.get('제외')),
                deadline=deadline, deadline_manual=deadline is not None,
                **es.analysis_fields(result),
            )
            db.add(e)
            imported += 1
        db.commit()
        return jsonify(success=True, imported=imported)
    finally:
        db.close()


# ─── 발신자·도메인 제외 규칙 ──────────────────────────────────────────────────

def _rule_json(db, r):
    ids = es.user_account_ids(db, current_user.id, active_only=False)
    excluded = db.query(func.count(Email.id)).filter(
        Email.account_id.in_(ids or [-1]), Email.is_excluded == True,  # noqa: E712
        Email.exclude_reason == es.rule_label(r.kind, r.value)).scalar() or 0
    return {'id': r.id, 'kind': r.kind, 'value': r.value, 'excluded_count': excluded,
            'created_at': timeutil.to_kst_str(r.created_at)}


@app.route('/api/sender-rules', methods=['GET'])
@login_required
def list_sender_rules():
    from db import SenderRule
    db = SessionLocal()
    try:
        rules = db.query(SenderRule).filter_by(user_id=current_user.id).order_by(SenderRule.created_at.desc()).all()
        return jsonify(rules=[_rule_json(db, r) for r in rules],
                       suggestions=es.suggest_domains(db, current_user.id))
    finally:
        db.close()


@app.route('/api/sender-rules/preview', methods=['POST'])
@login_required
def preview_sender_rule():
    """규칙을 만들기 전에: 정리된 값과, 기존 메일 중 새로 제외될 건수."""
    data = request.get_json() or {}
    try:
        value = es.normalize_rule(data.get('kind'), data.get('value'))
    except ValueError as ex:
        return jsonify(success=False, message=str(ex)), 400
    db = SessionLocal()
    try:
        return jsonify(success=True, kind=data.get('kind'), value=value,
                       matches=es.count_rule_matches(db, current_user.id, data.get('kind'), value))
    finally:
        db.close()


@app.route('/api/sender-rules', methods=['POST'])
@login_required
def add_sender_rule():
    from db import SenderRule
    data = request.get_json() or {}
    kind = data.get('kind')
    try:
        value = es.normalize_rule(kind, data.get('value'))
    except ValueError as ex:
        return jsonify(success=False, message=str(ex)), 400
    db = SessionLocal()
    try:
        r = db.query(SenderRule).filter_by(user_id=current_user.id, kind=kind, value=value).first()
        if r is None:
            r = SenderRule(user_id=current_user.id, kind=kind, value=value)
            db.add(r)
            db.commit()
        affected = es.apply_rule_to_existing(db, current_user.id, kind, value) if data.get('apply_existing', True) else 0
        return jsonify(success=True, rule=_rule_json(db, r), affected=affected)
    finally:
        db.close()


@app.route('/api/sender-rules/<int:rule_id>', methods=['DELETE'])
@login_required
def delete_sender_rule(rule_id):
    """규칙 삭제. ?restore=1 이면 그 규칙으로 자동 제외됐던 메일도 되돌린다."""
    from db import SenderRule
    db = SessionLocal()
    try:
        r = db.query(SenderRule).filter_by(id=rule_id, user_id=current_user.id).first()
        if r is None:
            return jsonify(success=False, message='규칙을 찾을 수 없습니다'), 404
        restored = es.restore_rule(db, current_user.id, r.kind, r.value) if request.args.get('restore') == '1' else 0
        db.delete(r)
        db.commit()
        return jsonify(success=True, restored=restored)
    finally:
        db.close()


# ─── 통계 ─────────────────────────────────────────────────────────────────────

@app.route('/api/stats')
@login_required
def get_stats():
    db = SessionLocal()
    try:
        ids = es.user_account_ids(db, current_user.id)
        if not ids:
            return jsonify(today_count=0, total_count=0)
        start = timeutil.kst_day_start_utc(timeutil.kst_today())
        base = db.query(func.count(Email.id)).filter(Email.account_id.in_(ids), Email.is_analyzed == True)  # noqa: E712
        return jsonify(today_count=base.filter(Email.analyzed_at >= start).scalar() or 0,
                       total_count=base.scalar() or 0)
    finally:
        db.close()


@app.route('/api/stats/chart')
@login_required
def get_stats_chart():
    """최근 14일(KST) 일별 수신/분석 건수."""
    db = SessionLocal()
    try:
        ids = es.user_account_ids(db, current_user.id)
        days = 14
        today = timeutil.kst_today()
        first = today - timedelta(days=days - 1)
        start = timeutil.kst_day_start_utc(first)
        buckets = {(first + timedelta(days=i)).isoformat(): {'received': 0, 'analyzed': 0} for i in range(days)}
        if ids:
            for (dt,) in db.query(Email.received_at).filter(Email.account_id.in_(ids), Email.received_at >= start):
                k = timeutil.to_kst_str(dt, '%Y-%m-%d')
                if k in buckets:
                    buckets[k]['received'] += 1
            for (dt,) in db.query(Email.analyzed_at).filter(
                    Email.account_id.in_(ids), Email.is_analyzed == True, Email.analyzed_at >= start):  # noqa: E712
                k = timeutil.to_kst_str(dt, '%Y-%m-%d')
                if k in buckets:
                    buckets[k]['analyzed'] += 1
        return jsonify([{'date': d, **v} for d, v in buckets.items()])
    finally:
        db.close()


# ─── 설정 (API 키 — 프로바이더별 관리) ─────────────────────────────────────────

KEY_PROVIDERS = {
    'anthropic': 'anthropic_api_key',
    'openai': 'openai_api_key',
}


def _mask_key(key):
    """키 원문을 프론트에 절대 내려보내지 않고, 확인용 마스킹 미리보기만 생성."""
    if not key:
        return None
    if len(key) <= 8:
        return key[:2] + '...'
    return key[:5] + '...' + key[-4:]


def _setting_put(db, user_id, key, value):
    s = db.query(Setting).filter_by(user_id=user_id, key=key).first()
    enc = encrypt(value) if value else ''
    if s:
        s.value_enc = enc
    else:
        db.add(Setting(user_id=user_id, key=key, value_enc=enc))


@app.route('/api/settings/keys', methods=['GET'])
@login_required
def list_api_keys():
    db = SessionLocal()
    try:
        out = {}
        for provider, setting_key in KEY_PROVIDERS.items():
            val = es.get_setting(db, current_user.id, setting_key)
            out[provider] = {'set': bool(val), 'preview': _mask_key(val)}
        return jsonify(out)
    finally:
        db.close()


@app.route('/api/settings/keys/<provider>', methods=['PUT'])
@login_required
def set_api_key(provider):
    if provider not in KEY_PROVIDERS:
        return jsonify(success=False, message='알 수 없는 프로바이더입니다'), 400
    data = request.get_json() or {}
    key = (data.get('api_key') or '').strip()
    if not key:
        return jsonify(success=False, message='API 키를 입력하세요'), 400
    db = SessionLocal()
    try:
        _setting_put(db, current_user.id, KEY_PROVIDERS[provider], key)
        db.commit()
    finally:
        db.close()
    analysis_worker.clear_pause(current_user.id)
    return jsonify(success=True, preview=_mask_key(key))


@app.route('/api/settings/keys/<provider>', methods=['DELETE'])
@login_required
def delete_api_key(provider):
    if provider not in KEY_PROVIDERS:
        return jsonify(success=False, message='알 수 없는 프로바이더입니다'), 400
    db = SessionLocal()
    try:
        s = db.query(Setting).filter_by(user_id=current_user.id, key=KEY_PROVIDERS[provider]).first()
        if s:
            db.delete(s)
            db.commit()
        return jsonify(success=True)
    finally:
        db.close()


@app.route('/api/settings/llm', methods=['GET'])
@login_required
def get_llm_settings():
    import llm_provider
    db = SessionLocal()
    try:
        return jsonify(
            provider=es.get_setting(db, current_user.id, 'llm_provider') or 'remote',
            model=es.get_setting(db, current_user.id, 'llm_model') or '',
            options=llm_provider.model_options(),
        )
    finally:
        db.close()


@app.route('/api/settings/llm', methods=['PUT'])
@login_required
def set_llm_settings():
    data = request.get_json() or {}
    provider = (data.get('provider') or 'remote').strip()
    model = (data.get('model') or '').strip()
    db = SessionLocal()
    try:
        _setting_put(db, current_user.id, 'llm_provider', provider)
        _setting_put(db, current_user.id, 'llm_model', model)
        db.commit()
    finally:
        db.close()
    analysis_worker.clear_pause(current_user.id)
    return jsonify(success=True)


@app.route('/api/settings/llm/test', methods=['POST'])
@login_required
def test_llm_connection():
    """설정 화면 '연결 테스트' 버튼. api_key가 함께 오면(저장 전 임시 테스트) 그 값을 우선 사용."""
    import llm_provider
    data = request.get_json() or {}
    provider = (data.get('provider') or 'remote').strip()
    model = (data.get('model') or '').strip() or None
    typed_key = (data.get('api_key') or '').strip() or None

    db = SessionLocal()
    try:
        anthropic_key = (typed_key if provider == 'anthropic' else None) or es.get_setting(db, current_user.id, 'anthropic_api_key')
        openai_key = (typed_key if provider == 'openai' else None) or es.get_setting(db, current_user.id, 'openai_api_key')
    finally:
        db.close()

    result = llm_provider.test_connection(provider, model, anthropic_key, openai_key)
    return jsonify(result)


@app.route('/api/settings/auto-analyze', methods=['GET'])
@login_required
def get_auto_analyze_setting():
    db = SessionLocal()
    try:
        return jsonify(enabled=es.auto_analyze_enabled(db, current_user.id))
    finally:
        db.close()


@app.route('/api/settings/auto-analyze', methods=['PUT'])
@login_required
def set_auto_analyze_setting():
    data = request.get_json() or {}
    enabled = bool(data.get('enabled'))
    db = SessionLocal()
    try:
        _setting_put(db, current_user.id, 'auto_analyze_enabled', 'true' if enabled else 'false')
        db.commit()
        changed = es.queue_recent_unanalyzed(db, current_user.id) if enabled else es.unqueue_auto(db, current_user.id)
    finally:
        db.close()
    if enabled:
        analysis_worker.wake()
    return jsonify(success=True, enabled=enabled, changed=changed)


@app.route('/api/settings/prompt', methods=['GET'])
@login_required
def get_analysis_prompt_api():
    db = SessionLocal()
    try:
        return jsonify(prompt=es.get_analysis_prompt(db, current_user.id))
    finally:
        db.close()


@app.route('/api/settings/prompt', methods=['PUT'])
@login_required
def set_analysis_prompt_api():
    data = request.get_json() or {}
    prompt = (data.get('prompt') or '').strip()
    db = SessionLocal()
    try:
        _setting_put(db, current_user.id, 'analysis_prompt', prompt)
        db.commit()
        return jsonify(success=True)
    finally:
        db.close()


# ─── 자연어 메일 검색 에이전트 ─────────────────────────────────────────────────

@app.route('/api/search/ask', methods=['POST'])
@login_required
def search_ask():
    """자연어로 '내가 분석한 메일'을 검색. 현재 설정된 LLM 프로바이더/모델을 사용."""
    import search_agent
    data = request.get_json() or {}
    query = (data.get('query') or '').strip()
    if not query:
        return jsonify(success=False, message='검색어를 입력하세요'), 400

    db = SessionLocal()
    try:
        cfg = es.get_llm_cfg(db, current_user.id)
        account_ids = es.user_account_ids(db, current_user.id)
        answer, matches, usage = search_agent.search(
            query, db, account_ids,
            provider=cfg['provider'], model=cfg['model'],
            anthropic_key=cfg['anthropic_key'], openai_key=cfg['openai_key'],
        )
        kinds = es.account_kinds(db, current_user.id)
        rows = {e.id: e for e in db.query(Email).options(*es.list_options())
                .filter(Email.id.in_([m['id'] for m in matches] or [-1]))}
        matches = [es.to_row(rows[m['id']], kinds) for m in matches if m['id'] in rows]
        return jsonify(success=True, answer=answer, matches=matches, usage=usage)
    except Exception as ex:
        logger.error(f'[search] 질의 실패: {ex}')
        return jsonify(success=False, message=str(ex)), 500
    finally:
        db.close()


# ─── Excel 다운로드 ───────────────────────────────────────────────────────────

_STATUS_KO = {'none': '미분석', 'queued': '분석 대기', 'running': '분석 중', 'done': '분석 완료', 'failed': '분석 실패'}


def _to_excel(emails) -> bytes:
    rows = []
    for e in emails:
        d = e.analysis_result or {}
        rows.append({
            '제목':            e.subject or '',
            '요청_업체':       d.get('요청_업체', '') or (e.sender or ''),
            '수신일':          timeutil.to_kst_str(e.received_at) or (e.received_date or ''),
            '마감일':          e.deadline.isoformat() if e.deadline else '',
            '마감일_원문':     d.get('마감일_원문', ''),
            '요청_유형':       d.get('요청_유형', ''),
            '우선순위':        d.get('우선순위', ''),
            '처리상태':        '제외' if e.is_excluded else ('완료' if e.is_completed else '미완료'),
            '분석상태':        _STATUS_KO.get(e.analysis_status, e.analysis_status or ''),
            '요구사항_요약':   d.get('요구사항_요약', ''),
            '상세_요구사항':   '\n'.join(str(x) for x in (d.get('상세_요구사항') or [])),
            '추가_참고사항':   d.get('추가_참고사항', ''),
        })
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='요구사항분석')
        wb = writer.book
        ws = writer.sheets['요구사항분석']
        hfmt = wb.add_format({'bold': True, 'bg_color': '#1d4ed8', 'font_color': 'white',
                               'border': 1, 'align': 'center', 'valign': 'vcenter'})
        wfmt = wb.add_format({'text_wrap': True, 'valign': 'top', 'border': 1})
        widths = [30, 20, 18, 12, 25, 14, 8, 10, 10, 50, 50, 30]
        for i, col in enumerate(df.columns):
            ws.write(0, i, col, hfmt)
            ws.set_column(i, i, widths[i] if i < len(widths) else 20, wfmt)
        ws.set_row(0, 22)
        ws.freeze_panes(1, 0)
    buf.seek(0)
    return buf.getvalue()


@app.route('/download/emails', methods=['POST'])
@login_required
def download_emails():
    """선택한 메일의 분석 결과 Excel 다운로드."""
    data = request.get_json() or {}
    email_ids = data.get('email_ids') or []
    db = SessionLocal()
    try:
        ids = es.user_account_ids(db, current_user.id, active_only=False)
        emails = db.query(Email).filter(Email.id.in_(email_ids or [-1]), Email.account_id.in_(ids or [-1])) \
            .order_by(Email.received_at.desc()).all()
        if not emails:
            return jsonify(success=False, message='다운로드할 메일이 없습니다'), 400
        excel = _to_excel(emails)
    finally:
        db.close()
    return send_file(
        io.BytesIO(excel),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='요구사항_분석결과.xlsx',
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
