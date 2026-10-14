"""스키마 보강 + 데이터 백필 (멱등, MariaDB·SQLite 공용).

- 모델에 있는데 테이블에 없는 컬럼·인덱스를 추가한다 (기존 컬럼은 건드리지 않음).
- received_at이 비어 있는 행(= 아직 이전 안 된 행)만 골라 새 컬럼을 채운다.
  · 분석 JSON 안의 완료/제외/마감일 → 전용 컬럼 (기존 마감일은 수동값으로 보존)
  · 기존 메일은 모두 읽음
  · 분석 상태: 실제 결과 → done / 가짜 파싱오류·재시도 소진 → failed /
               자동분석 켜짐 + 최근 30일 + 미처리 → queued / 그 밖 → none

배포 시:  python migrate.py   (서버 시작 시에도 자동 호출되지만 이미 끝났으면 할 일이 없다)
"""
import logging
from datetime import timedelta

from sqlalchemy import inspect, text, select, update, func
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)

FAKE_SUMMARY = '파싱 오류 - 원문 참조'
_FLAG_KEYS = {'완료', '제외', '마감일'}


def run(engine=None, backfill=True):
    from db import engine as default_engine, Email, EmailAccount
    engine = engine or default_engine
    for table in (EmailAccount.__table__, Email.__table__):
        insp = inspect(engine)
        if not insp.has_table(table.name):
            continue
        have = {c['name'] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = CreateColumn(col).compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {ddl}'))
            logger.info(f'[migrate] {table.name}.{col.name} 추가')
        names = {i['name'] for i in inspect(engine).get_indexes(table.name)}
        for ix in table.indexes:
            if ix.name not in names:
                ix.create(bind=engine)
                logger.info(f'[migrate] 인덱스 {ix.name} 추가')
    if backfill:
        return _backfill()
    return 0


def _backfill(batch=500):
    import timeutil
    import email_service as es
    from db import SessionLocal, Email, EmailAccount

    db = SessionLocal()
    done = 0
    try:
        db.execute(update(Email).where(Email.analysis_retry_count.is_(None)).values(analysis_retry_count=0))
        db.commit()

        acc_user = {a.id: a.user_id for a in db.query(EmailAccount).all()}
        auto_cache = {}

        def auto_on(account_id):
            uid = acc_user.get(account_id)
            if uid not in auto_cache:
                auto_cache[uid] = es.auto_analyze_enabled(db, uid) if uid else False
            return auto_cache[uid]

        recent_since = timeutil.utcnow() - timedelta(days=es.AUTO_ANALYZE_LOOKBACK_DAYS)
        last_id = 0
        while True:
            rows = db.execute(
                select(Email.id, Email.account_id, Email.received_date, Email.created_at,
                       Email.is_analyzed, Email.analysis_result, Email.analysis_retry_count,
                       Email.analysis_last_error,
                       func.length(Email.attachments_json).label('alen'))
                .where(Email.received_at.is_(None), Email.id > last_id)
                .order_by(Email.id).limit(batch)
            ).all()
            if not rows:
                break
            for r in rows:
                last_id = r.id
                res = r.analysis_result if isinstance(r.analysis_result, dict) else {}
                fake = res.get('요구사항_요약') == FAKE_SUMMARY
                flags_only = bool(res) and set(res.keys()) <= _FLAG_KEYS
                real = bool(r.is_analyzed) and bool(res) and not fake and not flags_only

                received_at = timeutil.parse_kst(r.received_date) or r.created_at or timeutil.utcnow()
                deadline = es.extract_deadline(res.get('마감일'))
                v = {
                    'received_at': received_at,
                    'is_read': True,
                    'is_completed': bool(res.get('완료')),
                    'is_excluded': bool(res.get('제외')),
                    'deadline': deadline,
                    'deadline_manual': deadline is not None,
                    'has_attachments': (r.alen or 0) > 2,
                    'analysis_priority': es.PRIORITY_AUTO,
                    'analysis_next_at': None,
                }
                v.update(es.analysis_fields(res if real else {}))
                if real:
                    v['analysis_status'] = 'done'
                elif fake or (r.analysis_retry_count or 0) >= 5:
                    v['analysis_status'] = 'failed'
                    if fake and not r.analysis_last_error:
                        v['analysis_last_error'] = '[응답 해석 실패] 이전 버전에서 분석 결과를 해석하지 못했습니다'
                elif (auto_on(r.account_id) and not v['is_excluded'] and not v['is_completed']
                      and received_at >= recent_since):
                    v['analysis_status'] = 'queued'
                else:
                    v['analysis_status'] = 'none'
                if res and not real:
                    # 가짜 결과나 '완료' 플래그만 든 JSON은 비운다 (상태는 위 컬럼으로 옮김)
                    v['analysis_result'] = None
                    v['is_analyzed'] = False
                db.execute(update(Email).where(Email.id == r.id).values(**v))
                done += 1
            db.commit()
        if done:
            logger.info(f'[migrate] 백필 {done}건')
        return done
    finally:
        db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    from db import init_db, SessionLocal, Email
    init_db()   # 테이블 생성 + run()
    db = SessionLocal()
    try:
        print('분석 상태별 건수:')
        for status, n in db.query(Email.analysis_status, func.count(Email.id)).group_by(Email.analysis_status):
            print(f'  {status}: {n}')
        missing = db.query(func.count(Email.id)).filter(Email.received_at.is_(None)).scalar()
        print(f'received_at 비어 있는 행: {missing}')
    finally:
        db.close()
