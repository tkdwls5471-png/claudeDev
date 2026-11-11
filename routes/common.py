"""라우트 공용 도우미: DB 세션, 소유권 확인, 쿼리 인자, 원본 EML 조회."""
from contextlib import contextmanager

from flask import jsonify, request
from flask_login import current_user

import email_service as es
from db import SessionLocal, EmailAccount, decrypt
from attachment_store import read_original
from imap_connector import fetch_email_by_uid


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def owned(db, email_id):
    return es.owned_email(db, current_user.id, email_id)


def not_found(message='메일을 찾을 수 없습니다'):
    return jsonify(success=False, message=message), 404


def bad_request(message):
    return jsonify(success=False, message=message), 400


def row(db, e):
    return es.to_row(e, es.account_kinds(db, current_user.id))


def int_arg(name, default, lo=None, hi=None):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def json_body():
    return request.get_json(silent=True) or {}


def load_raw(db, e):
    """원본 EML 바이트: 업로드는 디스크, IMAP은 메일 서버에서 (없으면 None)."""
    if e.raw_stored:
        return read_original(e.raw_stored)
    uid = es.imap_uid(e)
    acc = db.get(EmailAccount, e.account_id)
    if not uid or acc is None or (acc.kind or 'imap') != 'imap':
        return None
    return fetch_email_by_uid(acc.imap_server, acc.imap_port, acc.imap_username,
                              decrypt(acc.imap_password_enc), uid, acc.use_ssl)
