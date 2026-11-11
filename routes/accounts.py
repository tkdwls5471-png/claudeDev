"""IMAP 메일 계정 관리와 동기화·처리 현황."""
import os

from flask import Blueprint, jsonify
from flask_login import current_user, login_required
from sqlalchemy import func

import analysis_worker
import email_service as es
import scheduler as sched
import timeutil
from db import Email, EmailAccount, encrypt, decrypt
from imap_connector import test_connection
from routes.common import db_session, json_body

bp = Blueprint('accounts', __name__)


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


def _active_accounts(db):
    return db.query(EmailAccount).filter_by(user_id=current_user.id, is_active=True, kind='imap').all()


def _imap_account(db, account_id):
    return db.query(EmailAccount).filter_by(id=account_id, user_id=current_user.id, kind='imap').first()


@bp.route('/api/accounts', methods=['GET'])
@login_required
def list_accounts():
    with db_session() as db:
        return jsonify([_account_json(a) for a in _active_accounts(db)])


@bp.route('/api/accounts', methods=['POST'])
@login_required
def add_account():
    data = json_body()
    required = ('email_address', 'imap_server', 'imap_port', 'imap_username', 'imap_password')
    if not all(data.get(k) for k in required):
        return jsonify(success=False, message='필수 항목이 누락되었습니다'), 400
    try:
        port = int(data['imap_port'])
    except (TypeError, ValueError):
        return jsonify(success=False, message='포트 번호가 올바르지 않습니다'), 400
    server, user, pw = data['imap_server'], data['imap_username'], data['imap_password']
    use_ssl = data.get('use_ssl', True)

    ok, err = test_connection(server, port, user, pw, use_ssl)
    if not ok:
        return jsonify(success=False, message=f'연결 실패: {err}'), 400
    if data.get('_test_only'):   # 연결 테스트 전용: 계정을 만들지 않는다
        return jsonify(success=True, message='연결 성공')

    fields = dict(
        label=data.get('label') or data['email_address'],
        imap_server=server, imap_port=port, imap_username=user,
        imap_password_enc=encrypt(pw), use_ssl=use_ssl,
    )
    with db_session() as db:
        existing = db.query(EmailAccount).filter_by(
            user_id=current_user.id, email_address=data['email_address'], kind='imap').first()
        if existing and existing.is_active:
            return jsonify(success=False, message='이미 등록된 계정입니다'), 409
        if existing:   # 삭제(비활성)했던 계정 → 재활성화, 최근 메일부터 다시 받음 (중복은 제외됨)
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.is_active, existing.last_uid, existing.uidvalidity = True, 0, None
            acc = existing
        else:
            acc = EmailAccount(user_id=current_user.id, email_address=data['email_address'], kind='imap', **fields)
            db.add(acc)
        db.commit()
        acc_id = acc.id
    sched.clear_login_backoff(acc_id)
    sched.request_sync(current_user.id)
    return jsonify(success=True, id=acc_id)


@bp.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def delete_account(account_id):
    with db_session() as db:
        acc = _imap_account(db, account_id)
        if not acc:
            return jsonify(success=False, message='계정을 찾을 수 없습니다'), 404
        acc.is_active = False
        db.commit()
        return jsonify(success=True)


@bp.route('/api/accounts/<int:account_id>/test', methods=['POST'])
@login_required
def test_account(account_id):
    with db_session() as db:
        acc = _imap_account(db, account_id)
        if not acc:
            return jsonify(success=False, message='계정을 찾을 수 없습니다'), 404
        ok, err = test_connection(acc.imap_server, acc.imap_port, acc.imap_username,
                                  decrypt(acc.imap_password_enc), acc.use_ssl)
        if ok:
            sched.clear_login_backoff(acc.id)
        return jsonify(success=ok, message=err or '연결 성공')


@bp.route('/api/sync', methods=['POST'])
@login_required
def manual_sync():
    """현재 사용자 계정을 백그라운드에서 바로 동기화 (결과는 /api/status)."""
    started = sched.request_sync(current_user.id)
    return jsonify(success=True, started=started, running=True)


@bp.route('/api/status')
@login_required
def get_status():
    uid = current_user.id
    with db_session() as db:
        accounts = _active_accounts(db)
        ids = es.user_account_ids(db, uid) or [-1]
        counts = es.tab_counts(db.query(Email).filter(Email.account_id.in_(ids)), timeutil.kst_today())
        status_counts = dict(
            db.query(Email.analysis_status, func.count(Email.id))
            .filter(Email.account_id.in_(ids), Email.analysis_status.in_(('queued', 'running')))
            .group_by(Email.analysis_status).all())
        cur = analysis_worker.current()
        running = {'id': cur['id'], 'subject': cur['subject']} if cur and cur.get('user_id') == uid else None
        start = timeutil.kst_day_start_utc(timeutil.kst_today())
        analyzed_today = db.query(func.count(Email.id)).filter(
            Email.account_id.in_(ids), Email.is_analyzed == True, Email.analyzed_at >= start).scalar() or 0  # noqa: E712
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
                'today': analyzed_today,
                'auto_enabled': es.auto_analyze_enabled(db, uid),
                'paused': analysis_worker.pause_info(uid),
            },
            counts=counts,
            latest_email_id=es.latest_email_id(db, [i for i in ids if i != -1]),
        )
