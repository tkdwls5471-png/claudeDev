"""메일 도메인 공용 로직.

- 설정 조회 (LLM 설정, 자동 분석, 추가 지침)
- 분석 결과 → 목록용 컬럼 추출 (마감일·우선순위·유형·업체·요약)
- 소유권 확인, 업로드용 가상 계정
- 메일 수집(ingest): 동기화·업로드가 함께 쓰는 2단계 저장
- 첨부 저장/복원: 이미지 base64는 DB에 두지 않고 디스크 원본에서 다시 만든다
- 분석 대기열 조작
- 목록 조회(탭·필터·정렬·건수)와 Row 직렬화
"""
import json
import re
import logging
from datetime import timedelta

from sqlalchemy import or_, and_, case, func, update, select
from sqlalchemy.orm import defer
from sqlalchemy.exc import IntegrityError

import timeutil
from db import Email, EmailAccount, Setting, decrypt
from eml_parser import parse_eml
from attachment_handler import process_attachment
from attachment_store import save_original, read_original, delete_original, MAX_STORE_BYTES

logger = logging.getLogger(__name__)

REQUEST_TYPES = ['검토요청', '보고서/자료 요청', '수정/작업요청', '견적요청', '작업완료보고', '회의요청', '기타']
PRIORITIES = ['높음', '보통', '낮음']

AUTO_ANALYZE_LOOKBACK_DAYS = 30      # 자동 분석은 최근 N일 안에 받은 메일만
DUE_SOON_DAYS = 3                    # 마감임박 = 오늘 + N일 이내 (지난 것 포함)
BODY_TEXT_MAX = 50_000
BODY_HTML_MAX = 2_000_000
ATTACH_JSON_MAX = 8_000_000          # attachments_json 상한 (DB 패킷 초과 방지)
PRIORITY_MANUAL = 10                 # 수동 요청·업로드
PRIORITY_AUTO = 0

PARSE_FAIL_PREFIX = '[수신 처리 실패]'


# ─── 설정 ─────────────────────────────────────────────────────────────────────

def get_setting(db, user_id, key, default=None):
    s = db.query(Setting).filter_by(user_id=user_id, key=key).first()
    if not s or not s.value_enc:
        return default
    try:
        return decrypt(s.value_enc)
    except Exception:
        return default


def auto_analyze_enabled(db, user_id) -> bool:
    """자동분석 켬/끔 설정. 기본값은 켬."""
    return get_setting(db, user_id, 'auto_analyze_enabled', 'true') != 'false'


def get_llm_cfg(db, user_id) -> dict:
    return {
        'provider':      get_setting(db, user_id, 'llm_provider') or 'remote',
        'model':         get_setting(db, user_id, 'llm_model'),
        'anthropic_key': get_setting(db, user_id, 'anthropic_api_key'),
        'openai_key':    get_setting(db, user_id, 'openai_api_key'),
    }


def get_analysis_prompt(db, user_id) -> str:
    return get_setting(db, user_id, 'analysis_prompt', '') or ''


# ─── 분석 결과 → 컬럼 ─────────────────────────────────────────────────────────

def normalize_label(s):
    """'높음 (마감 임박)' → '높음' 처럼 괄호 부연을 제거."""
    s = str(s or '').strip()
    return re.split(r'[(（]', s, 1)[0].strip() if s else ''


_DEADLINE_RE = re.compile(r'(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})')


def extract_deadline(s):
    """'2026-10-20', '2026.10.20 (협의)', '2026년 10월 20일' → date. 없으면 None."""
    if not s:
        return None
    m = _DEADLINE_RE.search(str(s))
    if not m:
        return None
    from datetime import date
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def analysis_fields(res) -> dict:
    """분석 결과 dict에서 목록·필터용 컬럼 값 추출."""
    res = res if isinstance(res, dict) else {}
    pr = normalize_label(res.get('우선순위'))
    rt = normalize_label(res.get('요청_유형'))
    return {
        'priority': pr[:10] or None,
        'req_type': rt[:50] or None,
        'company': (str(res.get('요청_업체') or '').strip()[:255]) or None,
        'summary': (str(res.get('요구사항_요약') or '').strip()) or None,
    }


def apply_analysis_success(e: Email, result: dict, usage: dict):
    """분석 성공 반영. 완료·제외·읽음과 수동 마감일은 건드리지 않는다."""
    e.analysis_result = result
    e.is_analyzed = True
    e.analyzed_at = timeutil.utcnow()
    e.analysis_provider = usage.get('provider')
    e.analysis_model = (usage.get('model') or '')[:80] or None
    e.analysis_tokens_in = usage.get('tokens_in') or 0
    e.analysis_tokens_out = usage.get('tokens_out') or 0
    for k, v in analysis_fields(result).items():
        setattr(e, k, v)
    if not e.deadline_manual:
        e.deadline = extract_deadline(result.get('마감일'))
    e.analysis_status = 'done'
    e.analysis_retry_count = 0
    e.analysis_last_error = None
    e.analysis_next_at = None


# ─── 소유권 ───────────────────────────────────────────────────────────────────

def user_accounts(db, user_id, active_only=True):
    q = db.query(EmailAccount).filter(EmailAccount.user_id == user_id)
    if active_only:
        q = q.filter(EmailAccount.is_active == True)  # noqa: E712
    return q.all()


def user_account_ids(db, user_id, active_only=True):
    return [a.id for a in user_accounts(db, user_id, active_only)]


def owned_email(db, user_id, email_id):
    """현재 사용자 소유 메일 (아니면 None)."""
    ids = user_account_ids(db, user_id, active_only=False)
    if not ids:
        return None
    return db.query(Email).filter(Email.id == email_id, Email.account_id.in_(ids)).first()


def get_or_create_upload_account(db, user_id) -> EmailAccount:
    acc = db.query(EmailAccount).filter_by(user_id=user_id, kind='upload').first()
    if acc:
        if not acc.is_active:
            acc.is_active = True
            db.commit()
        return acc
    acc = EmailAccount(
        user_id=user_id, label='업로드', email_address='upload', kind='upload',
        imap_server='', imap_port=0, imap_username='', imap_password_enc='',
        use_ssl=False, is_active=True, last_uid=0,
    )
    db.add(acc)
    db.commit()
    return acc


def imap_uid(e: Email):
    """IMAP에서 다시 받을 수 있는 UID (업로드·재구성 전 UID면 None)."""
    uid = str(e.message_uid or '')
    return uid if uid.isdigit() else None


# ─── 첨부 ─────────────────────────────────────────────────────────────────────

def _ext_of(filename):
    filename = filename or ''
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def build_attachments(raw_atts):
    """파싱된 첨부 목록 → (DB 저장용 리스트, 용량 초과 안내 목록).
    원본은 디스크에 저장하고, 이미지/스캔PDF 이미지는 DB에 base64로 두지 않는다."""
    out, oversized = [], []
    for a in raw_atts or []:
        data = a.get('data') or b''
        fn = a.get('filename') or '첨부파일'
        if len(data) > MAX_STORE_BYTES:
            oversized.append((fn, len(data) / 1024 / 1024))
            out.append({'type': 'unsupported', 'filename': fn,
                        'content': '[용량 초과(100MB)로 저장하지 않은 첨부파일]',
                        'media_type': None, 'stored': None, 'size': len(data), 'oversized': True})
            continue
        try:
            p = process_attachment({'filename': fn, 'extension': a.get('extension') or _ext_of(fn),
                                    'data': data, 'content_type': a.get('content_type')})
        except Exception as ex:
            p = {'type': 'text', 'filename': fn, 'content': f'[첨부 처리 실패: {ex}]', 'media_type': None}
        meta = save_original(data, a.get('extension') or _ext_of(fn))
        entry = {'type': p.get('type'), 'filename': fn, 'content': p.get('content'),
                 'media_type': p.get('media_type'),
                 'stored': meta['stored'] if meta else None,
                 'size': meta['size'] if meta else len(data)}
        if entry['type'] in ('image', 'images') and meta:
            entry['content'] = None   # 디스크 원본에서 필요할 때 다시 만든다
        elif entry['type'] == 'images' and isinstance(entry['content'], list):
            entry['content'] = entry['content'][:3]
        out.append(entry)
    return _cap_attachments(out), oversized


def _cap_attachments(atts):
    """JSON 크기가 상한을 넘으면 HTML 속 data: 이미지 → 긴 텍스트 순으로 줄인다."""
    def size():
        return len(json.dumps(atts, ensure_ascii=False))
    if size() <= ATTACH_JSON_MAX:
        return atts
    for a in atts:
        if a.get('type') == 'html' and isinstance(a.get('content'), str):
            a['content'] = re.sub(r'data:[a-zA-Z/+.-]+;base64,[A-Za-z0-9+/=\s]+', '', a['content'])
    while size() > ATTACH_JSON_MAX:
        big = max(atts, key=lambda x: len(json.dumps(x.get('content'), ensure_ascii=False)), default=None)
        if not big or not big.get('content'):
            break
        c = big['content']
        if isinstance(c, str) and len(c) > 20_000:
            big['content'] = c[: len(c) // 2] + '\n…(용량 제한으로 잘림)'
        else:
            big['content'] = None
            big['truncated'] = True
    return atts


def hydrate_attachments(atts):
    """분석용: 이미지 content가 비어 있으면 디스크 원본으로 다시 만든다 (DB에는 저장 안 함)."""
    out = []
    for a in atts or []:
        a = dict(a)
        if a.get('type') in ('image', 'images') and not a.get('content') and a.get('stored'):
            data = read_original(a['stored'])
            if data:
                try:
                    p = process_attachment({'filename': a.get('filename') or '',
                                            'extension': _ext_of(a.get('filename')) or _ext_of(a['stored']),
                                            'data': data})
                    if p.get('type') in ('image', 'images'):
                        a['content'] = p.get('content')
                        a['media_type'] = p.get('media_type') or a.get('media_type')
                except Exception as ex:
                    logger.warning(f'[hydrate] {a.get("filename")}: {ex}')
        out.append(a)
    return out


def delete_email_files(e: Email):
    try:
        for a in json.loads(e.attachments_json or '[]'):
            if a.get('stored'):
                delete_original(a['stored'])
    except Exception:
        pass
    if e.raw_stored:
        delete_original(e.raw_stored)


# ─── 수집 (동기화·업로드 공용) ────────────────────────────────────────────────

def _should_auto_queue(received_at):
    if not received_at:
        return True
    return received_at >= timeutil.utcnow() - timedelta(days=AUTO_ANALYZE_LOOKBACK_DAYS)


def ingest_message(db, account: EmailAccount, uid_str: str, raw: bytes, *,
                   queue_mode='none', advance_uid=None, raw_stored=None,
                   is_read=False, fallback_subject=None):
    """메일 1건 저장. 반환: (email_id 또는 None, 'created' | 'duplicate' | 'failed').

    1단계: 헤더만으로 최소 행을 만들고 (동기화면 last_uid와 함께) 커밋한다.
           이후 어디서 죽어도 '실패' 행이 남고 같은 메일에서 계정이 멈추지 않는다.
    2단계: 본문·첨부·원본 HTML을 채우고 분석 상태를 정해 다시 커밋한다.
    queue_mode: 'auto'(최근 메일만 자동 대기) | 'force'(우선 대기, 업로드·수동) | 'none'
    DB 오류(1단계)는 호출자에게 그대로 던진다 → 동기화는 그 계정을 멈추고 UID를 올리지 않는다.
    """
    parse_error = None
    try:
        parsed = parse_eml(raw)
    except Exception as ex:
        parsed, parse_error = {}, ex

    message_id = parsed.get('message_id')
    received_at = parsed.get('received_at') or timeutil.utcnow()

    # 중복: 같은 계정의 같은 UID, 또는 (UIDVALIDITY 재구성 후) 같은 Message-ID
    dup = db.query(Email.id).filter_by(account_id=account.id, message_uid=uid_str).first()
    if not dup and message_id:
        old = db.query(Email).filter(Email.account_id == account.id, Email.message_id == message_id).first()
        if old is not None:
            if old.message_uid != uid_str and not imap_uid(old) and account.kind == 'imap':
                old.message_uid = uid_str          # 재구성 전 UID → 새 UID로 다시 연결
            dup = (old.id,)
    if dup:
        if advance_uid is not None and advance_uid > (account.last_uid or 0):
            account.last_uid = advance_uid
        db.commit()
        return dup[0], 'duplicate'

    subject = parsed.get('subject') or fallback_subject or ''
    e = Email(
        account_id=account.id, message_uid=uid_str,
        subject=subject if not parse_error else (fallback_subject or '(메일 파싱 실패)'),
        sender=(parsed.get('sender') or '')[:500],
        recipient=parsed.get('to') or '',
        received_date=(parsed.get('received_date') or timeutil.to_kst_str(received_at) or '')[:50],
        received_at=received_at, message_id=message_id,
        body_text='', attachments_json='[]', raw_stored=raw_stored,
        is_read=is_read, analysis_status='failed',
        analysis_last_error=f'{PARSE_FAIL_PREFIX} 수신 처리 중 중단됨',
    )
    db.add(e)
    if advance_uid is not None and advance_uid > (account.last_uid or 0):
        account.last_uid = advance_uid
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        dup = db.query(Email.id).filter_by(account_id=account.id, message_uid=uid_str).first()
        if advance_uid is not None:
            acc = db.get(EmailAccount, account.id)
            if advance_uid > (acc.last_uid or 0):
                acc.last_uid = advance_uid
                db.commit()
        return (dup[0] if dup else None), 'duplicate'

    if parse_error is not None:
        e.analysis_last_error = f'{PARSE_FAIL_PREFIX} 메일을 해석할 수 없습니다: {parse_error}'[:500]
        db.commit()
        return e.id, 'failed'

    try:
        atts, oversized = build_attachments(parsed.get('attachments'))
        body = (parsed.get('body') or '')[:BODY_TEXT_MAX]
        if oversized:
            notice = '\n'.join(f"· {n} ({mb:.0f}MB)" for n, mb in oversized)
            body += f"\n\n──────────\n⚠ 용량 초과(100MB)로 저장하지 않은 첨부파일:\n{notice}"
        html = parsed.get('body_html') or ''
        e.body_text = body
        e.attachments_json = json.dumps(atts, ensure_ascii=False)
        e.has_attachments = bool(atts)
        e.body_html = html if len(html) <= BODY_HTML_MAX else ''
        if queue_mode == 'force':
            e.analysis_status, e.analysis_priority = 'queued', PRIORITY_MANUAL
        elif queue_mode == 'auto' and _should_auto_queue(received_at):
            e.analysis_status, e.analysis_priority = 'queued', PRIORITY_AUTO
        else:
            e.analysis_status = 'none'
        e.analysis_last_error = None
        db.commit()
        return e.id, 'created'
    except Exception as ex:
        logger.error(f'[ingest] {account.id}/{uid_str} 본문·첨부 저장 실패: {ex}')
        db.rollback()
        row = db.get(Email, e.id)
        if row is not None:
            row.analysis_status = 'failed'
            row.analysis_last_error = f'{PARSE_FAIL_PREFIX} 본문·첨부 저장 실패: {ex}'[:500]
            db.commit()
        return e.id, 'failed'


# ─── 분석 대기열 ──────────────────────────────────────────────────────────────

def queue_analysis(db, ids, priority=PRIORITY_MANUAL):
    """지정 메일을 대기열에 올린다 (분석 중인 메일은 그대로). 반환: 올린 건수."""
    if not ids:
        return 0
    r = db.execute(
        update(Email)
        .where(Email.id.in_(ids), Email.analysis_status != 'running')
        .values(analysis_status='queued', analysis_priority=priority, analysis_next_at=None,
                analysis_retry_count=0, analysis_last_error=None)
        .execution_options(synchronize_session=False))
    db.commit()
    return r.rowcount


def cancel_analysis(db, ids):
    if not ids:
        return 0
    r = db.execute(
        update(Email).where(Email.id.in_(ids), Email.analysis_status == 'queued')
        .values(analysis_status='none', analysis_next_at=None)
        .execution_options(synchronize_session=False))
    db.commit()
    return r.rowcount


def queue_recent_unanalyzed(db, user_id):
    """자동 분석을 켤 때: 최근 N일 미분석·미처리 메일을 자동 우선순위로 대기열에 올린다."""
    ids = user_account_ids(db, user_id)
    if not ids:
        return 0
    since = timeutil.utcnow() - timedelta(days=AUTO_ANALYZE_LOOKBACK_DAYS)
    r = db.execute(
        update(Email).where(
            Email.account_id.in_(ids), Email.analysis_status == 'none',
            Email.is_excluded == False, Email.is_completed == False,  # noqa: E712
            Email.received_at >= since)
        .values(analysis_status='queued', analysis_priority=PRIORITY_AUTO, analysis_next_at=None)
        .execution_options(synchronize_session=False))
    db.commit()
    return r.rowcount


def unqueue_auto(db, user_id):
    """자동 분석을 끌 때: 자동으로 올라간 대기 건만 내린다 (수동 요청은 유지)."""
    ids = user_account_ids(db, user_id)
    if not ids:
        return 0
    r = db.execute(
        update(Email).where(Email.account_id.in_(ids), Email.analysis_status == 'queued',
                            Email.analysis_priority == PRIORITY_AUTO)
        .values(analysis_status='none', analysis_next_at=None)
        .execution_options(synchronize_session=False))
    db.commit()
    return r.rowcount


# ─── 목록 조회 ────────────────────────────────────────────────────────────────

TABS = ('todo', 'due', 'done', 'excluded', 'failed', 'all')
SORTS = ('received', 'deadline', 'priority', 'analyzed')

_TODO = and_(Email.is_completed == False, Email.is_excluded == False)  # noqa: E712


def _due_cond(today):
    return and_(_TODO, Email.deadline.isnot(None),
                Email.deadline <= today + timedelta(days=DUE_SOON_DAYS))


def _tab_cond(tab, today):
    if tab == 'todo':
        return _TODO
    if tab == 'due':
        return _due_cond(today)
    if tab == 'done':
        return and_(Email.is_completed == True, Email.is_excluded == False)  # noqa: E712
    if tab == 'excluded':
        return Email.is_excluded == True  # noqa: E712
    if tab == 'failed':
        return Email.analysis_status == 'failed'
    return None


def list_options():
    """목록 조회 시 큰 컬럼은 지연 로드."""
    return (defer(Email.body_text), defer(Email.attachments_json), defer(Email.analysis_result),
            defer(Email.recipient))


def filtered_query(db, account_ids, *, q='', priorities=(), types=(), unread=False,
                   range_from=None, range_to=None):
    """탭을 제외한 모든 필터를 적용한 쿼리. 행을 읽을 때는 .options(*list_options())를 붙인다."""
    query = db.query(Email).filter(Email.account_id.in_(account_ids))
    if q:
        like = '%' + q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_') + '%'
        query = query.filter(or_(
            Email.subject.ilike(like, escape='\\'), Email.sender.ilike(like, escape='\\'),
            Email.company.ilike(like, escape='\\'), Email.summary.ilike(like, escape='\\'),
            Email.body_text.ilike(like, escape='\\'),
        ))
    if priorities:
        query = query.filter(Email.priority.in_(list(priorities)))
    if types:
        query = query.filter(Email.req_type.in_(list(types)))
    if unread:
        query = query.filter(Email.is_read == False)  # noqa: E712
    if range_to:
        query = query.filter(Email.received_at < timeutil.kst_day_start_utc(range_to + timedelta(days=1)))
    if range_from:
        query = query.filter(or_(
            Email.deadline >= range_from,
            and_(Email.deadline.is_(None), Email.received_at >= timeutil.kst_day_start_utc(range_from)),
        ))
    return query


def tab_counts(query, today) -> dict:
    """필터가 적용된 쿼리에서 탭별 건수를 한 번에 센다."""
    def s(cond):
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)
    row = query.with_entities(
        func.count(Email.id),
        s(_TODO), s(_due_cond(today)), s(_tab_cond('done', today)),
        s(_tab_cond('excluded', today)), s(_tab_cond('failed', today)),
        s(Email.is_read == False),  # noqa: E712
    ).order_by(None).one()
    keys = ('all', 'todo', 'due', 'done', 'excluded', 'failed', 'unread')
    return {k: int(v or 0) for k, v in zip(keys, row)}


def apply_tab_and_sort(query, tab, sort, today):
    cond = _tab_cond(tab, today)
    if cond is not None:
        query = query.filter(cond)
    if sort == 'deadline':
        return query.order_by(Email.deadline.is_(None), Email.deadline.asc(),
                              Email.received_at.desc(), Email.id.desc())
    if sort == 'priority':
        rank = case((Email.priority == '높음', 0), (Email.priority == '보통', 1),
                    (Email.priority == '낮음', 2), else_=3)
        return query.order_by(rank, Email.received_at.desc(), Email.id.desc())
    if sort == 'analyzed':
        return query.order_by(Email.analyzed_at.is_(None), Email.analyzed_at.desc(), Email.id.desc())
    return query.order_by(Email.received_at.desc(), Email.id.desc())


def account_kinds(db, user_id):
    return {a.id: (a.kind or 'imap') for a in user_accounts(db, user_id, active_only=False)}


def to_row(e: Email, kinds: dict) -> dict:
    return {
        'id': e.id,
        'subject': e.subject or '',
        'sender': e.sender or '',
        'company': e.company,
        'received_at': timeutil.to_kst_str(e.received_at) or (e.received_date or ''),
        'source': 'upload' if kinds.get(e.account_id) == 'upload' else 'imap',
        'account_id': e.account_id,
        'has_attachments': bool(e.has_attachments),
        'is_read': bool(e.is_read),
        'is_completed': bool(e.is_completed),
        'completed_at': timeutil.to_kst_str(e.completed_at),
        'is_excluded': bool(e.is_excluded),
        'deadline': e.deadline.isoformat() if e.deadline else None,
        'deadline_manual': bool(e.deadline_manual),
        'priority': e.priority,
        'req_type': e.req_type,
        'summary': e.summary,
        'analysis_status': e.analysis_status or 'none',
        'analysis_error': e.analysis_last_error,
        'analysis_retry_count': e.analysis_retry_count or 0,
        'analysis_next_at': timeutil.to_kst_str(e.analysis_next_at) if e.analysis_status == 'queued' else None,
        'analyzed_at': timeutil.to_kst_str(e.analyzed_at),
        'analysis_model': e.analysis_model,
    }


def delete_emails(db, emails):
    n = 0
    for e in emails:
        delete_email_files(e)
        db.delete(e)
        n += 1
    db.commit()
    return n


def latest_email_id(db, account_ids):
    if not account_ids:
        return 0
    return db.execute(select(func.max(Email.id)).where(Email.account_id.in_(account_ids))).scalar() or 0
