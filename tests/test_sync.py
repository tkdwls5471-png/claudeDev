from datetime import datetime, timezone

import email_service as es
import scheduler
from db import Email, EmailAccount
from conftest import make_user, make_account
from fakes import make_eml


def _uids(db, acc):
    db.expire_all()
    return sorted(int(u) for (u,) in db.query(Email.message_uid).filter_by(account_id=acc.id))


def test_first_sync_takes_latest_50(db, clock, fake_imap):
    acc = make_account(db, make_user(db))
    fake_imap.messages = {u: make_eml(subject=f'm{u}') for u in range(1, 121)}

    r = scheduler.sync_account(acc.id)

    assert r['status'] == 'ok' and r['new'] == 50
    assert _uids(db, acc) == list(range(71, 121))
    db.refresh(acc)
    assert acc.last_uid == 120 and acc.last_sync_status == 'ok' and acc.last_sync_new == 50


def test_backlog_is_split_without_loss(db, clock, fake_imap):
    acc = make_account(db, make_user(db), last_uid=100)
    fake_imap.messages = {u: make_eml(subject=f'm{u}') for u in range(1, 331)}

    assert scheduler.sync_account(acc.id)['new'] == 200
    db.refresh(acc)
    assert acc.last_uid == 300
    assert scheduler.sync_account(acc.id)['new'] == 30
    assert _uids(db, acc) == list(range(101, 331))


def test_max_uid_echo_is_not_reimported(db, clock, fake_imap):
    acc = make_account(db, make_user(db), last_uid=5)
    fake_imap.messages = {u: make_eml() for u in range(1, 6)}

    r = scheduler.sync_account(acc.id)

    assert r['new'] == 0
    assert _uids(db, acc) == []
    assert fake_imap.fetched == []


def test_parse_failure_becomes_visible_failed_row(db, clock, fake_imap, monkeypatch):
    acc = make_account(db, make_user(db), last_uid=0)
    fake_imap.messages = {1: make_eml(subject='ok1'), 2: b'BROKEN', 3: make_eml(subject='ok3')}
    real_parse = es.parse_eml

    def parse(raw):
        if raw == b'BROKEN':
            raise ValueError('bad mime')
        return real_parse(raw)
    monkeypatch.setattr(es, 'parse_eml', parse)

    scheduler.sync_account(acc.id)

    db.expire_all()
    rows = {e.message_uid: e for e in db.query(Email).filter_by(account_id=acc.id)}
    assert set(rows) == {'1', '2', '3'}
    assert rows['2'].analysis_status == 'failed'
    assert rows['2'].analysis_last_error.startswith(es.PARSE_FAIL_PREFIX)
    assert rows['1'].analysis_status == 'queued'
    db.refresh(acc)
    assert acc.last_uid == 3


def test_db_failure_stops_account_and_resumes_later(db, clock, fake_imap, monkeypatch):
    acc = make_account(db, make_user(db), last_uid=100)
    fake_imap.messages = {u: make_eml(subject=f'm{u}') for u in range(101, 106)}
    real_ingest = es.ingest_message

    def flaky(db_, account, uid_str, raw, **kw):
        if uid_str == '103':
            raise RuntimeError('database went away')
        return real_ingest(db_, account, uid_str, raw, **kw)
    monkeypatch.setattr(es, 'ingest_message', flaky)

    r = scheduler.sync_account(acc.id)
    assert r['status'] == 'error'
    db.refresh(acc)
    assert acc.last_uid == 102 and acc.last_sync_status == 'error'
    assert 'database went away' in acc.last_sync_error

    monkeypatch.setattr(es, 'ingest_message', real_ingest)
    assert scheduler.sync_account(acc.id)['new'] == 3
    assert _uids(db, acc) == [101, 102, 103, 104, 105]


def test_uidvalidity_change_relinks_by_message_id(db, clock, fake_imap):
    acc = make_account(db, make_user(db))
    fake_imap.messages = {u: make_eml(subject=f'm{u}', message_id=f'<m{u}@a.com>') for u in (1, 2, 3)}
    scheduler.sync_account(acc.id)
    ids_before = {e.message_id: e.id for e in db.query(Email).filter_by(account_id=acc.id)}

    fake_imap.uidvalidity = 2
    fake_imap.messages = {10 + u: make_eml(subject=f'm{u}', message_id=f'<m{u}@a.com>') for u in (1, 2, 3)}
    fake_imap.messages[20] = make_eml(subject='new', message_id='<new@a.com>')
    r = scheduler.sync_account(acc.id)

    assert r['new'] == 1
    db.expire_all()
    rows = {e.message_id: e for e in db.query(Email).filter_by(account_id=acc.id)}
    assert len(rows) == 4
    for mid, eid in ids_before.items():
        assert rows[mid].id == eid                      # 같은 행이 새 UID로 다시 연결됨
    assert sorted(int(e.message_uid) for e in rows.values()) == [11, 12, 13, 20]
    db.refresh(acc)
    assert acc.uidvalidity == 2 and acc.last_uid == 20


def test_login_failure_is_recorded_and_backs_off(db, clock, fake_imap):
    acc = make_account(db, make_user(db))
    fake_imap.login_error = True

    r = scheduler.sync_account(acc.id)
    assert r['status'] == 'error'
    db.refresh(acc)
    assert acc.last_sync_status == 'error' and '로그인 실패' in acc.last_sync_error

    fake_imap.login_error = False
    assert scheduler.sync_account(acc.id)['status'] == 'skipped'     # 30분 대기
    clock.advance(minutes=31)
    assert scheduler.sync_account(acc.id)['status'] == 'ok'


def test_auto_queue_only_recent_mail_and_respects_setting(db, clock, fake_imap):
    alice = make_user(db, 'alice')
    acc = make_account(db, alice)
    old = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fake_imap.messages = {1: make_eml(subject='recent'), 2: make_eml(subject='old', date=old)}
    scheduler.sync_account(acc.id)
    db.expire_all()
    st = {e.subject: e.analysis_status for e in db.query(Email).filter_by(account_id=acc.id)}
    assert st == {'recent': 'queued', 'old': 'none'}

    bob = make_user(db, 'bob', auto=False)
    acc2 = make_account(db, bob, email='b@example.com')
    scheduler.sync_account(acc2.id)
    db.expire_all()
    assert {e.analysis_status for e in db.query(Email).filter_by(account_id=acc2.id)} == {'none'}


def test_ingest_stores_html_and_strips_image_base64(db, clock, fake_imap):
    from fakes import png_bytes
    import json
    acc = make_account(db, make_user(db))
    fake_imap.messages = {1: make_eml(attachments=[('photo.png', png_bytes(), 'image', 'png')])}
    scheduler.sync_account(acc.id)

    db.expire_all()
    e = db.query(Email).filter_by(account_id=acc.id).one()
    atts = json.loads(e.attachments_json)
    assert atts[0]['type'] == 'image' and atts[0]['content'] is None and atts[0]['stored']
    assert e.has_attachments and '<p>' in e.body_html
    assert e.received_at == datetime(2026, 10, 13, 0, 0) and e.received_date == '2026-10-13 09:00'


def test_inactive_and_upload_accounts_are_skipped(db, clock, fake_imap):
    u = make_user(db)
    acc = make_account(db, u, is_active=False)
    up = es.get_or_create_upload_account(db, u.id)
    fake_imap.messages = {1: make_eml()}
    assert scheduler.sync_account(acc.id)['status'] == 'skipped'
    assert scheduler.sync_account(up.id)['status'] == 'skipped'
    assert db.query(EmailAccount).filter_by(kind='upload').count() == 1
