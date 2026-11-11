"""메일 목록·상세·상태 변경·일괄 처리·분석 요청·원본/첨부 조회."""
import io
import json
import logging

from flask import Blueprint, jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import func, update

import analysis_worker
import email_service as es
import timeutil
from attachment_store import content_type_for, delete_original, read_original, save_original
from db import Email
from eml_parser import parse_eml
from routes.common import db_session, int_arg, json_body, load_raw, not_found, owned, row
from routes.html_sanitize import sanitize_email_html

logger = logging.getLogger(__name__)
bp = Blueprint('emails', __name__)

EMPTY_COUNTS = {k: 0 for k in ('all', 'todo', 'due', 'done', 'excluded', 'failed', 'unread')}


@bp.route('/api/emails')
@login_required
def list_emails():
    tab = request.args.get('tab', 'todo')
    tab = tab if tab in es.TABS else 'todo'
    sort = request.args.get('sort') or ('deadline' if tab == 'due' else 'received')
    sort = sort if sort in es.SORTS else 'received'
    page = int_arg('page', 1, lo=1)
    per_page = int_arg('per_page', 50, lo=1, hi=200)

    with db_session() as db:
        accounts = es.user_accounts(db, current_user.id)
        ids = [a.id for a in accounts]
        source = request.args.get('source')
        if source in ('imap', 'upload'):
            ids = [a.id for a in accounts if (a.kind or 'imap') == source]
        account_id = request.args.get('account_id')
        if account_id:
            ids = [i for i in ids if str(i) == account_id]
        if not ids:
            return jsonify(total=0, page=page, per_page=per_page, counts=EMPTY_COUNTS, emails=[])

        base = es.filtered_query(
            db, ids,
            q=(request.args.get('q') or '').strip(),
            priorities=[p for p in request.args.getlist('priority') if p],
            types=[t for t in request.args.getlist('type') if t],
            unread=request.args.get('unread') in ('1', 'true'),
        )
        today = timeutil.kst_today()
        counts = es.tab_counts(base, today)
        q = es.apply_tab_and_sort(base.options(*es.list_options()), tab, sort, today)
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        kinds = {a.id: (a.kind or 'imap') for a in accounts}
        return jsonify(total=counts[tab], page=page, per_page=per_page, counts=counts,
                       emails=[es.to_row(e, kinds) for e in rows])


def _serialize_attachments(e):
    """첨부 목록에 프론트용 메타(idx, has_original)를 붙인다. 내부 저장 파일명은 노출하지 않는다."""
    try:
        attachments = json.loads(e.attachments_json or '[]')
    except Exception:
        attachments = []
    fetchable = bool(es.imap_uid(e)) or bool(e.raw_stored)
    out = []
    for i, a in enumerate(attachments):
        a = dict(a)
        a['idx'] = i
        a['has_original'] = bool(a.pop('stored', None)) or fetchable
        out.append(a)
    return out


@bp.route('/api/emails/<int:email_id>')
@login_required
def get_email(email_id):
    with db_session() as db:
        e = owned(db, email_id)
        if not e:
            return not_found()
        if request.args.get('mark_read') == '1' and not e.is_read:
            e.is_read = True
            db.commit()
        html_len = db.query(func.length(Email.body_html)).filter(Email.id == e.id).scalar()
        out = row(db, e)
        out.update({
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
        return jsonify(out)


@bp.route('/api/emails/<int:email_id>', methods=['PATCH'])
@login_required
def patch_email(email_id):
    data = json_body()
    with db_session() as db:
        e = owned(db, email_id)
        if not e:
            return not_found()
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
        return jsonify(row(db, e))


@bp.route('/api/emails/<int:email_id>', methods=['DELETE'])
@login_required
def delete_email(email_id):
    """앱에서 메일 삭제 (메일 서버의 원본은 그대로). 디스크 첨부도 함께 삭제."""
    with db_session() as db:
        e = owned(db, email_id)
        if not e:
            return not_found()
        es.delete_emails(db, [e])
        return jsonify(success=True)


def _bulk_update(db, ids, where=None, **values):
    stmt = update(Email).where(Email.id.in_(ids))
    if where is not None:
        stmt = stmt.where(where)
    r = db.execute(stmt.values(**values).execution_options(synchronize_session=False))
    db.commit()
    return r.rowcount


def _bulk_actions(now):
    """action → (db, ids) → 처리 건수"""
    upd = _bulk_update

    def exclude(db, ids):
        n = upd(db, ids, None, is_excluded=True, exclude_reason=None)
        upd(db, ids, Email.analysis_status == 'queued', analysis_status='none', analysis_next_at=None)
        return n

    def analyze(db, ids):
        n = es.queue_analysis(db, ids)
        analysis_worker.wake()
        return n

    return {
        'complete': lambda db, ids: upd(db, ids, Email.is_completed == False, is_completed=True, completed_at=now),  # noqa: E712
        'uncomplete': lambda db, ids: upd(db, ids, Email.is_completed == True, is_completed=False, completed_at=None),  # noqa: E712
        'exclude': exclude,
        'unexclude': lambda db, ids: upd(db, ids, None, is_excluded=False, exclude_reason=None),
        'read': lambda db, ids: upd(db, ids, None, is_read=True),
        'unread': lambda db, ids: upd(db, ids, None, is_read=False),
        'delete': lambda db, ids: es.delete_emails(db, db.query(Email).filter(Email.id.in_(ids)).all()),
        'analyze': analyze,
        'cancel_analysis': es.cancel_analysis,
    }


@bp.route('/api/emails/bulk', methods=['POST'])
@login_required
def bulk_emails():
    data = json_body()
    actions = _bulk_actions(timeutil.utcnow())
    action = actions.get(data.get('action'))
    if action is None:
        return jsonify(success=False, message='알 수 없는 작업입니다'), 400
    try:
        req_ids = [int(i) for i in (data.get('ids') or [])][:5000]
    except (TypeError, ValueError):
        return jsonify(success=False, message='잘못된 메일 ID'), 400

    with db_session() as db:
        acc_ids = es.user_account_ids(db, current_user.id, active_only=False)
        if not req_ids or not acc_ids:
            return jsonify(success=True, affected=0)
        ids = [i for (i,) in db.query(Email.id).filter(Email.id.in_(req_ids), Email.account_id.in_(acc_ids))]
        if not ids:
            return jsonify(success=True, affected=0)
        return jsonify(success=True, affected=action(db, ids))


@bp.route('/api/emails/<int:email_id>/analyze', methods=['POST'])
@login_required
def analyze_email(email_id):
    """분석(재분석) 요청: 대기열 맨 앞에 올리고 바로 반환."""
    with db_session() as db:
        e = owned(db, email_id)
        if not e:
            return not_found()
        es.queue_analysis(db, [e.id])
        db.refresh(e)
        analysis_worker.wake()
        return jsonify(row(db, e))


@bp.route('/api/emails/<int:email_id>/reprocess', methods=['POST'])
@login_required
def reprocess_email(email_id):
    """원본을 다시 받아 첨부파일·본문 HTML을 최신 로직으로 재처리 (분석 상태는 유지)."""
    with db_session() as db:
        try:
            e = owned(db, email_id)
            if not e:
                return not_found()
            if not (es.imap_uid(e) or e.raw_stored):
                return jsonify(success=False, message='원본이 없어 다시 처리할 수 없습니다'), 400
            raw = load_raw(db, e)
            if not raw:
                return jsonify(success=False, message='원본 메일을 가져오지 못했습니다 (서버에서 삭제되었을 수 있음)'), 404

            parsed = parse_eml(raw)
            old_atts = json.loads(e.attachments_json or '[]')
            atts, _ = es.build_attachments(parsed.get('attachments'))
            for a in old_atts:   # 이전 디스크 사본 정리
                if a.get('stored'):
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


@bp.route('/api/emails/<int:email_id>/body-html')
@login_required
def get_email_body_html(email_id):
    """원본 HTML 본문 (DB 캐시 우선, 없으면 원본에서 1회 추출해 저장) → 정제 후 반환."""
    allow_images = request.args.get('images') == '1'
    with db_session() as db:
        try:
            e = owned(db, email_id)
            if not e:
                return jsonify(html=None, message='메일을 찾을 수 없습니다'), 404
            html = e.body_html
            if html is None:
                raw = load_raw(db, e)
                if raw is None:
                    return jsonify(html=None, has_html=False,
                                   message='원본을 조회할 수 없습니다 (서버에서 삭제되었거나 원본이 없는 메일)')
                html = parse_eml(raw).get('body_html') or ''
                e.body_html = html if len(html) <= es.BODY_HTML_MAX else ''
                db.commit()
            if not html:
                return jsonify(html=None, has_html=False, message='HTML 본문이 없는 메일입니다')
            return jsonify(html=sanitize_email_html(html, allow_images), has_html=True,
                           images_blocked=not allow_images)
        except Exception as ex:
            logger.error(f'[body-html] email={email_id} 조회 실패: {ex}')
            return jsonify(html=None, message=f'원본을 불러오지 못했습니다: {ex}'), 500


def _fetch_attachment(db, e, atts, idx, ext):
    """디스크에 없으면 원본 메일에서 꺼내 디스크에 캐시한다."""
    try:
        raw = load_raw(db, e)
        if not raw:
            return None
        src = parse_eml(raw).get('attachments', [])
        if idx >= len(src) or not src[idx].get('data'):
            return None
        data = src[idx]['data']
        meta = save_original(data, src[idx].get('extension') or ext)
        if meta:
            atts[idx]['stored'], atts[idx]['size'] = meta['stored'], meta['size']
            e.attachments_json = json.dumps(atts, ensure_ascii=False)
            db.commit()
        return data
    except Exception as ex:
        logger.error(f'[attachment raw] 원본 조회 실패 email={e.id} idx={idx}: {ex}')
        return None


@bp.route('/api/emails/<int:email_id>/attachment/<int:idx>/raw')
@login_required
def get_attachment_raw(email_id, idx):
    """첨부파일 원본. ?dl=1 이면 다운로드, 아니면 브라우저 표시."""
    with db_session() as db:
        e = owned(db, email_id)
        if not e:
            return jsonify(error='메일을 찾을 수 없습니다'), 404
        atts = json.loads(e.attachments_json or '[]')
        if idx < 0 or idx >= len(atts):
            return jsonify(error='첨부파일을 찾을 수 없습니다'), 404
        filename = atts[idx].get('filename') or f'attachment_{idx}'
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        data = read_original(atts[idx].get('stored'))
        if data is None:
            data = _fetch_attachment(db, e, atts, idx, ext)
        if data is None:
            return jsonify(error='원본 파일을 가져오지 못했습니다 (서버에서 삭제되었을 수 있음)'), 404
        return send_file(io.BytesIO(data), mimetype=content_type_for(ext),
                         as_attachment=request.args.get('dl') == '1', download_name=filename, max_age=0)
