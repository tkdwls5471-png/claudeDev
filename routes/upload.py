"""EML 업로드 (업로드 메일함에 저장 → 분석 대기열)."""
import hashlib

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import analysis_worker
import email_service as es
from attachment_store import save_original
from db import Email
from eml_parser import parse_eml
from routes.common import db_session, row

bp = Blueprint('upload', __name__)


def _find_duplicate(db, acc, uid, data):
    """같은 파일(내용 해시) 또는 이미 메일 계정으로 받은 메일(Message-ID)."""
    existing = db.query(Email).filter_by(account_id=acc.id, message_uid=uid).first()
    if existing is not None:
        return existing
    try:
        mid = parse_eml(data).get('message_id')
    except Exception:
        mid = None
    if not mid:
        return None
    ids = es.user_account_ids(db, current_user.id, active_only=False)
    return db.query(Email).filter(Email.account_id.in_(ids), Email.message_id == mid).first()


@bp.route('/api/upload', methods=['POST'])
@login_required
def upload_eml():
    f = request.files.get('file') or next(iter(request.files.getlist('files[]')), None)
    if f is None:
        return jsonify(success=False, message='파일이 없습니다'), 400
    filename = (f.filename or 'upload.eml')[:200]
    data = f.read()
    if not data:
        return jsonify(success=False, error={'filename': filename, 'message': '빈 파일입니다'}), 400

    with db_session() as db:
        acc = es.get_or_create_upload_account(db, current_user.id)
        uid = 'upload-' + hashlib.sha1(data).hexdigest()
        existing = _find_duplicate(db, acc, uid, data)
        if existing is not None:
            return jsonify(success=True, duplicate=row(db, existing), filename=filename)

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
            return jsonify(success=True, duplicate=row(db, e), filename=filename)
        if status == 'failed':
            return jsonify(success=False, created=row(db, e), filename=filename,
                           error={'filename': filename, 'message': e.analysis_last_error or 'EML을 해석할 수 없습니다'})
        return jsonify(success=True, created=row(db, e), filename=filename)
