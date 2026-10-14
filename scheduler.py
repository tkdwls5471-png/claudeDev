"""메일 동기화 엔진 + 백그라운드 작업 시작.

- 계정별 잠금으로 수동·자동 동기화가 겹치지 않는다 (진행 중이면 건너뜀).
- 새 UID를 오래된 순으로 1건씩 저장하고, 저장(커밋)한 만큼만 last_uid를 올린다.
  첫 동기화만 최근 INITIAL_LIMIT건, 이후에는 한 번에 CYCLE_LIMIT건씩 이어 받는다 → 누락 없음.
- 동기화 결과(성공/오류/신규 건수)를 계정에 저장해 화면에 보여 준다.
- 분석은 analysis_worker가 따로 처리한다 (동기화 후 wake).
"""
import os
import logging
import atexit
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import update, literal

import timeutil
import email_service as es
import analysis_worker
import imap_connector
from db import SessionLocal, EmailAccount, Email, decrypt

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(timezone='Asia/Seoul')

INITIAL_LIMIT = 50      # 계정 첫 동기화 때 가져올 최근 메일 수
CYCLE_LIMIT = 200       # 한 번 동기화에서 받을 최대 건수 (나머지는 다음 주기)
LOGIN_BACKOFF = timedelta(minutes=30)

_guard = threading.Lock()
_account_locks = {}     # account_id -> Lock
_login_backoff = {}     # account_id -> until(UTC)
_user_syncing = set()   # 수동 동기화 진행 중인 user_id


def _lock_for(account_id):
    with _guard:
        return _account_locks.setdefault(account_id, threading.Lock())


def clear_login_backoff(account_id):
    with _guard:
        _login_backoff.pop(account_id, None)


def account_syncing(account_id) -> bool:
    return _lock_for(account_id).locked()


# ─── 계정 1개 동기화 ──────────────────────────────────────────────────────────

def sync_account(account_id) -> dict:
    """반환: {'status': 'ok'|'error'|'busy'|'skipped', 'new': int, 'error': str|None}"""
    lock = _lock_for(account_id)
    if not lock.acquire(blocking=False):
        return {'status': 'busy', 'new': 0, 'error': None}
    try:
        return _sync_locked(account_id)
    finally:
        lock.release()


def _record(db, account_id, status, error=None, new=0):
    acc = db.get(EmailAccount, account_id)
    if acc is None:
        return
    acc.last_synced = timeutil.utcnow()
    acc.last_sync_status = status
    acc.last_sync_error = (error or None) and str(error)[:1000]
    acc.last_sync_new = new
    db.commit()


def _sync_locked(account_id) -> dict:
    db = SessionLocal()
    new = 0
    try:
        acc = db.get(EmailAccount, account_id)
        if acc is None or not acc.is_active or (acc.kind or 'imap') != 'imap':
            return {'status': 'skipped', 'new': 0, 'error': None}
        with _guard:
            until = _login_backoff.get(account_id)
        if until and until > timeutil.utcnow():
            return {'status': 'skipped', 'new': 0, 'error': None}

        try:
            password = decrypt(acc.imap_password_enc)
        except Exception:
            err = '저장된 비밀번호를 읽을 수 없습니다. 설정에서 계정을 다시 등록해 주세요.'
            _record(db, account_id, 'error', err)
            return {'status': 'error', 'new': 0, 'error': err}

        try:
            session = imap_connector.open_session(
                acc.imap_server, acc.imap_port, acc.imap_username, password, acc.use_ssl)
        except imap_connector.ImapLoginError as ex:
            with _guard:
                _login_backoff[account_id] = timeutil.utcnow() + LOGIN_BACKOFF
            err = f'로그인 실패: {ex} (30분 뒤 다시 시도합니다. 비밀번호/앱 비밀번호를 확인하세요)'
            _record(db, account_id, 'error', err)
            return {'status': 'error', 'new': 0, 'error': err}
        except Exception as ex:
            err = f'메일 서버 연결 실패: {ex or type(ex).__name__}'
            _record(db, account_id, 'error', err)
            return {'status': 'error', 'new': 0, 'error': err}

        auto_on = es.auto_analyze_enabled(db, acc.user_id)
        with session:
            uidvalidity = session.select_inbox()
            if uidvalidity and acc.uidvalidity and uidvalidity != acc.uidvalidity:
                _handle_uidvalidity_change(db, acc, uidvalidity)
            elif uidvalidity and not acc.uidvalidity:
                acc.uidvalidity = uidvalidity
                db.commit()

            last = acc.last_uid or 0
            uids = [u for u in session.search_uids(last + 1 if last else 0) if u > last]
            if last == 0 and len(uids) > INITIAL_LIMIT:
                uids = uids[-INITIAL_LIMIT:]
            uids = uids[:CYCLE_LIMIT]

            for uid in uids:
                raw = session.fetch(uid)
                if raw is None:   # 그 사이 서버에서 지워진 메일
                    acc.last_uid = max(acc.last_uid or 0, uid)
                    db.commit()
                    continue
                _, status = es.ingest_message(
                    db, acc, str(uid), raw,
                    queue_mode='auto' if auto_on else 'none', advance_uid=uid)
                if status in ('created', 'failed'):
                    new += 1

        _record(db, account_id, 'ok', None, new)
        if new:
            logger.info(f'[sync] 계정 {account_id}: 신규 {new}건')
            analysis_worker.wake()
        return {'status': 'ok', 'new': new, 'error': None}
    except Exception as ex:
        logger.exception(f'[sync] 계정 {account_id} 동기화 오류: {ex}')
        try:
            db.rollback()
            _record(db, account_id, 'error', f'동기화 중 오류: {ex}', new)
        except Exception:
            pass
        if new:
            analysis_worker.wake()
        return {'status': 'error', 'new': new, 'error': str(ex)}
    finally:
        db.close()


def _handle_uidvalidity_change(db, acc, uidvalidity):
    """메일함이 재구성되어 UID 체계가 바뀜: 기존 UID에 접두사를 붙여 충돌을 피하고
    처음부터 다시 받는다. 이미 있는 메일은 Message-ID로 새 UID에 다시 연결된다."""
    old = acc.uidvalidity
    logger.warning(f'[sync] 계정 {acc.id} UIDVALIDITY 변경 {old} → {uidvalidity}')
    db.execute(update(Email)
               .where(Email.account_id == acc.id, Email.message_uid.notlike('%:%'))
               .values(message_uid=literal(f'v{old}:') + Email.message_uid)
               .execution_options(synchronize_session=False))
    acc.uidvalidity = uidvalidity
    acc.last_uid = 0
    db.commit()


# ─── 전체/사용자 동기화 ───────────────────────────────────────────────────────

def sync_all():
    """스케줄러 잡: 모든 활성 IMAP 계정 동기화 (한 계정 오류가 다른 계정을 막지 않음)."""
    db = SessionLocal()
    try:
        ids = [a.id for a in db.query(EmailAccount.id).filter(
            EmailAccount.is_active == True, EmailAccount.kind == 'imap')]  # noqa: E712
    finally:
        db.close()
    for account_id in ids:
        try:
            sync_account(account_id)
        except Exception as ex:
            logger.exception(f'[sync] 계정 {account_id}: {ex}')


def user_syncing(user_id) -> bool:
    with _guard:
        return user_id in _user_syncing


def request_sync(user_id) -> bool:
    """사용자 계정들을 백그라운드에서 바로 동기화. 이미 진행 중이면 False."""
    with _guard:
        if user_id in _user_syncing:
            return False
        _user_syncing.add(user_id)

    def run():
        try:
            db = SessionLocal()
            try:
                ids = [a.id for a in db.query(EmailAccount.id).filter(
                    EmailAccount.user_id == user_id, EmailAccount.is_active == True,  # noqa: E712
                    EmailAccount.kind == 'imap')]
            finally:
                db.close()
            for account_id in ids:
                clear_login_backoff(account_id)   # 사용자가 직접 요청하면 대기 없이 시도
                sync_account(account_id)
        except Exception as ex:
            logger.exception(f'[sync] 사용자 {user_id} 수동 동기화 오류: {ex}')
        finally:
            with _guard:
                _user_syncing.discard(user_id)

    threading.Thread(target=run, name=f'sync-user-{user_id}', daemon=True).start()
    return True


# ─── 백그라운드 시작 ──────────────────────────────────────────────────────────

_lock_file = None


def _acquire_process_lock() -> bool:
    """백그라운드 작업은 프로세스 하나에서만 (gunicorn 워커를 실수로 늘려도 중복 실행 방지)."""
    global _lock_file
    try:
        import fcntl
    except ImportError:      # Windows 개발 환경
        return True
    path = os.environ.get('EML_BG_LOCK', os.path.join(os.path.dirname(os.path.abspath(__file__)), '.background.lock'))
    try:
        f = open(path, 'w')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file = f
        return True
    except OSError:
        return False


def start_background(interval_minutes=None) -> bool:
    """분석 워커 + 주기 동기화 시작. EML_DISABLE_BACKGROUND=1이면 시작하지 않는다."""
    if os.environ.get('EML_DISABLE_BACKGROUND') == '1':
        logger.info('[scheduler] 백그라운드 작업 비활성화됨')
        return False
    if not _acquire_process_lock():
        # 다른 프로세스가 이미 실행 중 (재시작 중 겹침 포함) → 30초 뒤 다시 시도
        logger.info('[scheduler] 다른 프로세스가 백그라운드 작업 중 → 30초 뒤 재시도')
        t = threading.Timer(30, start_background, args=(interval_minutes,))
        t.daemon = True
        t.start()
        return False

    interval = interval_minutes or int(os.environ.get('SYNC_INTERVAL_MIN', 2))
    analysis_worker.start()
    _scheduler.add_job(
        func=sync_all,
        trigger=IntervalTrigger(minutes=interval),
        id='imap_sync',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    if not _scheduler.running:
        _scheduler.start()
        atexit.register(lambda: _scheduler.shutdown(wait=False))
    logger.info(f'[scheduler] 시작 (동기화 간격: {interval}분)')
    return True


def stop():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
