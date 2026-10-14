"""분석 대기열 워커.

워커 스레드 하나가 analysis_status='queued'인 메일을 우선순위(수동·업로드 먼저) →
최신 수신 순으로 하나씩 분석한다. 기존의 '5분 주기 자동분석'과 '일괄분석 스레드'를
대체하므로 같은 메일을 두 번 분석하거나 '이미 진행 중'으로 막히는 일이 없다.

오류 처리 (llm_provider.classify):
- transient : 그 메일은 2·5·15·30·60분 뒤 재시도, 6번째 실패 시 실패. 사용자 대기열은 1분만 쉰다
- config    : 메일은 대기 유지(재시도 횟수 그대로), 사용자 대기열 10분 일시정지.
              LLM 설정·키를 저장하면 clear_pause()로 즉시 재개
- invalid   : 이 메일 요청 자체가 거부됨 → 실패
- parse     : 1분 뒤 1회 재시도 후 실패
"""
import json
import logging
import threading
from datetime import timedelta

from sqlalchemy import update, select, or_

import timeutil
import email_service as es
import llm_provider
from db import SessionLocal, Email, EmailAccount

logger = logging.getLogger(__name__)

BACKOFF_MIN = [2, 5, 15, 30, 60]
MAX_ATTEMPTS = 6
CLAIM_TTL = timedelta(minutes=15)
CONFIG_PAUSE = timedelta(minutes=10)
TRANSIENT_PAUSE = timedelta(minutes=1)
IDLE_WAIT_SEC = 30

ERROR_LABEL = {
    'transient': '[일시 오류]',
    'config': '[설정 오류]',
    'invalid': '[요청 거부]',
    'parse': '[응답 해석 실패]',
}

_wake = threading.Event()
_lock = threading.Lock()
_pauses = {}            # user_id -> (until_utc, reason)
_current = {}           # {'id', 'subject', 'user_id'} — 지금 분석 중인 메일
_thread = None


# ─── 외부 API ────────────────────────────────────────────────────────────────

def start():
    """서버 시작 시 1회: 중단된 'running'을 대기로 되돌리고 워커 스레드 시작."""
    global _thread
    if _thread and _thread.is_alive():
        return
    db = SessionLocal()
    try:
        n = db.execute(update(Email).where(Email.analysis_status == 'running')
                       .values(analysis_status='queued', analysis_next_at=None)
                       .execution_options(synchronize_session=False)).rowcount
        db.commit()
        if n:
            logger.info(f'[worker] 중단된 분석 {n}건을 대기열로 되돌림')
    finally:
        db.close()
    _thread = threading.Thread(target=_loop, name='analysis-worker', daemon=True)
    _thread.start()


def wake():
    _wake.set()


def current():
    with _lock:
        return dict(_current) if _current else None


def pause_info(user_id):
    with _lock:
        p = _pauses.get(user_id)
    if not p or p[0] <= timeutil.utcnow():
        return None
    return {'until': timeutil.to_kst_str(p[0]), 'reason': p[1]}


def clear_pause(user_id):
    with _lock:
        _pauses.pop(user_id, None)
    wake()


def _pause(user_id, until, reason):
    with _lock:
        _pauses[user_id] = (until, reason)


def _paused_users():
    now = timeutil.utcnow()
    with _lock:
        return [u for u, (until, _) in _pauses.items() if until > now]


# ─── 루프 ────────────────────────────────────────────────────────────────────

def _loop():
    while True:
        try:
            did = run_once()
        except Exception as ex:   # 워커는 절대 죽지 않는다
            logger.exception(f'[worker] 예기치 않은 오류: {ex}')
            did = False
        if not did:
            _wake.wait(IDLE_WAIT_SEC)
            _wake.clear()


def run_once() -> bool:
    """대기 1건을 선점해 처리. 처리했으면 True."""
    claimed = _claim()
    if not claimed:
        return False
    _process(*claimed)
    return True


def _claim():
    now = timeutil.utcnow()
    db = SessionLocal()
    try:
        # 비정상 종료 등으로 오래 'running'에 머문 건 회수
        db.execute(update(Email).where(Email.analysis_status == 'running', Email.analysis_next_at < now)
                   .values(analysis_status='queued', analysis_next_at=None)
                   .execution_options(synchronize_session=False))
        db.commit()

        paused = _paused_users()
        q = (select(Email.id, EmailAccount.user_id)
             .join(EmailAccount, Email.account_id == EmailAccount.id)
             .where(Email.analysis_status == 'queued',
                    or_(Email.analysis_next_at.is_(None), Email.analysis_next_at <= now),
                    EmailAccount.is_active == True))  # noqa: E712
        if paused:
            q = q.where(EmailAccount.user_id.notin_(paused))
        q = q.order_by(Email.analysis_priority.desc(), Email.received_at.desc(), Email.id.desc()).limit(5)
        for email_id, user_id in db.execute(q).all():
            r = db.execute(update(Email)
                           .where(Email.id == email_id, Email.analysis_status == 'queued')
                           .values(analysis_status='running', analysis_next_at=now + CLAIM_TTL)
                           .execution_options(synchronize_session=False))
            db.commit()
            if r.rowcount == 1:
                return email_id, user_id
        return None
    finally:
        db.close()


def _process(email_id, user_id):
    db = SessionLocal()
    try:
        e = db.get(Email, email_id)
        if e is None:
            return
        cfg = es.get_llm_cfg(db, user_id)
        extra = es.get_analysis_prompt(db, user_id)
        eml_data = {
            'subject': e.subject or '', 'sender': e.sender or '', 'to': e.recipient or '',
            'received_date': timeutil.to_kst_str(e.received_at) or e.received_date or '',
            'body': e.body_text or '',
        }
        try:
            atts = json.loads(e.attachments_json or '[]')
        except Exception:
            atts = []
        subject = e.subject or ''
    finally:
        db.close()   # LLM 호출 동안 DB 커넥션을 잡고 있지 않는다

    from claude_analyzer import SYSTEM_PROMPT
    system_prompt = SYSTEM_PROMPT
    if extra:
        system_prompt += f"\n\n=== 사용자 추가 지침 (우선 반영) ===\n{extra}"

    with _lock:
        _current.clear()
        _current.update({'id': email_id, 'subject': subject, 'user_id': user_id})
    try:
        result, usage = llm_provider.analyze(
            eml_data, es.hydrate_attachments(atts), system_prompt=system_prompt,
            provider=cfg.get('provider'), model=cfg.get('model'),
            anthropic_key=cfg.get('anthropic_key'), openai_key=cfg.get('openai_key'),
        )
    except Exception as ex:
        _on_error(email_id, user_id, ex)
        return
    finally:
        with _lock:
            _current.clear()

    db = SessionLocal()
    try:
        e = db.get(Email, email_id)
        if e is None or e.analysis_status != 'running':
            return   # 그 사이 삭제·취소됨
        es.apply_analysis_success(e, result, usage)
        db.commit()
        logger.info(f'[worker] 분석 완료 email={email_id}')
    finally:
        db.close()


def _on_error(email_id, user_id, ex):
    kind = llm_provider.classify(ex)
    label = ERROR_LABEL.get(kind, '[오류]')
    msg = f'{label} {str(ex) or type(ex).__name__}'[:500]
    now = timeutil.utcnow()
    logger.warning(f'[worker] 분석 실패 email={email_id} kind={kind}: {ex}')

    db = SessionLocal()
    try:
        e = db.get(Email, email_id)
        if e is None or e.analysis_status != 'running':
            return

        def final():
            # 이전에 성공한 결과가 있으면 그 결과를 유지하고 오류만 남긴다
            e.analysis_status = 'done' if e.is_analyzed else 'failed'
            e.analysis_next_at = None

        if kind == 'config':
            e.analysis_status, e.analysis_next_at = 'queued', None
            e.analysis_last_error = msg
            _pause(user_id, now + CONFIG_PAUSE, msg)
        elif kind == 'transient':
            rc = (e.analysis_retry_count or 0) + 1
            e.analysis_retry_count, e.analysis_last_error = rc, msg
            if rc >= MAX_ATTEMPTS:
                final()
            else:
                delay = timedelta(minutes=BACKOFF_MIN[min(rc, len(BACKOFF_MIN)) - 1])
                e.analysis_status, e.analysis_next_at = 'queued', now + delay
                # 서버가 내려간 경우를 대비해 사용자 대기열은 잠깐만 쉰다. 이 메일만의 문제일 수도 있으므로
                # 긴 대기는 이 메일에만 적용 (메일마다 각자 간격을 두고 재시도하므로 장애가 길어도 바로 소진되지 않음)
                _pause(user_id, now + TRANSIENT_PAUSE, msg)
        elif kind == 'parse':
            already = (e.analysis_last_error or '').startswith(ERROR_LABEL['parse'])
            e.analysis_retry_count = (e.analysis_retry_count or 0) + 1
            e.analysis_last_error = msg
            if already:
                final()
            else:
                e.analysis_status, e.analysis_next_at = 'queued', now + timedelta(minutes=1)
        else:   # invalid
            e.analysis_retry_count = (e.analysis_retry_count or 0) + 1
            e.analysis_last_error = msg
            final()
        db.commit()
    finally:
        db.close()
