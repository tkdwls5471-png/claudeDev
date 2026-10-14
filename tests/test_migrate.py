"""옛 스키마(재설계 전 운영 DB 모양)에 마이그레이션을 돌려 백필 규칙을 확인한다."""
import json
from datetime import date, datetime

from sqlalchemy import text

import db as dbm
import migrate
from db import Email

LEGACY_DDL = [
    """CREATE TABLE eml_users (id INTEGER PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL,
       password_hash VARCHAR(255) NOT NULL, created_at DATETIME)""",
    """CREATE TABLE eml_accounts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, label VARCHAR(100),
       email_address VARCHAR(255) NOT NULL, imap_server VARCHAR(255) NOT NULL, imap_port INTEGER,
       imap_username VARCHAR(255) NOT NULL, imap_password_enc TEXT NOT NULL, use_ssl BOOLEAN,
       last_uid INTEGER, last_synced DATETIME, is_active BOOLEAN, created_at DATETIME)""",
    """CREATE TABLE eml_emails (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
       message_uid VARCHAR(255) NOT NULL, subject TEXT, sender VARCHAR(500), recipient TEXT,
       received_date VARCHAR(50), body_text TEXT, attachments_json TEXT, is_analyzed BOOLEAN,
       analysis_result JSON, analyzed_at DATETIME, analysis_provider VARCHAR(30),
       analysis_model VARCHAR(80), analysis_tokens_in INTEGER, analysis_tokens_out INTEGER,
       analysis_retry_count INTEGER, analysis_last_error TEXT, created_at DATETIME,
       CONSTRAINT uq_eml_account_message UNIQUE (account_id, message_uid))""",
    """CREATE TABLE eml_settings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, key VARCHAR(100) NOT NULL,
       value_enc TEXT, CONSTRAINT uq_eml_user_key UNIQUE (user_id, key))""",
]

REAL = {'요구사항_요약': '요약', '상세_요구사항': ['a'], '마감일': '2026-10-20 (협의)',
        '요청_업체': 'A사', '요청_유형': '견적요청', '우선순위': '높음', '완료': True}
FAKE = {'요구사항_요약': '파싱 오류 - 원문 참조', '상세_요구사항': [], '마감일': '불명확'}


def _legacy_db(clock):
    dbm.Base.metadata.drop_all(dbm.engine)
    with dbm.engine.begin() as c:
        for ddl in LEGACY_DDL:
            c.execute(text(ddl))
        c.execute(text("INSERT INTO eml_users VALUES (1,'alice','x','2026-01-01')"))
        c.execute(text("INSERT INTO eml_accounts VALUES (1,1,'a','a@x','imap.x',993,'a','enc',1,9,NULL,1,'2026-01-01')"))
        rows = [
            # id, received_date, is_analyzed, result, retry, created_at
            (1, '2026-10-13 09:00', 1, REAL, 0, '2026-10-13 00:01:00'),
            (2, '2026-10-12 09:00', 1, FAKE, 0, '2026-10-12 00:01:00'),
            (3, '2026-10-10 09:00', 0, None, 0, '2026-10-10 00:01:00'),     # 최근 미분석 → queued
            (4, '2026-01-02 09:00', 0, None, 0, '2026-01-02 00:01:00'),     # 오래된 미분석 → none
            (5, '2026-10-11 09:00', 0, {'완료': True}, 0, '2026-10-11 00:01:00'),  # complete-batch 흔적
            (6, '2026-10-11 09:00', 0, None, 5, '2026-10-11 00:01:00'),     # 재시도 소진 → failed
            (7, 'Mon, garbage', 0, None, None, '2026-10-09 03:00:00'),     # 날짜 해석 불가
        ]
        for i, rd, an, res, rc, created in rows:
            c.execute(text(
                "INSERT INTO eml_emails (id, account_id, message_uid, subject, received_date, body_text, "
                "attachments_json, is_analyzed, analysis_result, analysis_retry_count, created_at) "
                "VALUES (:i, 1, :uid, :s, :rd, '', :att, :an, :res, :rc, :created)"),
                dict(i=i, uid=str(i), s=f'm{i}', rd=rd, att='[{"type":"text"}]' if i == 1 else '[]', an=an,
                     res=json.dumps(res, ensure_ascii=False) if res is not None else None, rc=rc, created=created))


def test_legacy_schema_migrates_and_backfills(clock, db):
    _legacy_db(clock)
    dbm.Base.metadata.create_all(dbm.engine)          # 없는 테이블만 생성 (기존 테이블은 그대로)
    assert migrate.run(dbm.engine) == 7

    db.expire_all()
    e = {r.id: r for r in db.query(Email).all()}
    assert e[1].analysis_status == 'done' and e[1].is_completed and e[1].is_read
    assert e[1].deadline == date(2026, 10, 20) and e[1].deadline_manual
    assert e[1].priority == '높음' and e[1].company == 'A사' and e[1].has_attachments
    assert e[1].received_at == datetime(2026, 10, 13, 0, 0)

    assert e[2].analysis_status == 'failed' and e[2].analysis_result is None and not e[2].is_analyzed
    assert e[3].analysis_status == 'queued'
    assert e[4].analysis_status == 'none'
    assert e[5].is_completed and e[5].analysis_result is None and e[5].analysis_status == 'none'
    assert e[6].analysis_status == 'failed'
    assert e[7].received_at == datetime(2026, 10, 9, 3, 0) and e[7].analysis_retry_count == 0

    # 두 번째 실행은 아무것도 바꾸지 않는다
    assert migrate.run(dbm.engine) == 0
