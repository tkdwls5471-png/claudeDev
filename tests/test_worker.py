import json
from datetime import date, timedelta

import analysis_worker as aw
import email_service as es
import llm_provider
import scheduler
from db import Email
from conftest import make_user, make_account
from fakes import make_eml, png_bytes


def _queued_mail(db, fake_imap, user=None, subject='견적 요청', attachments=()):
    user = user or make_user(db)
    acc = make_account(db, user, email=f'{user.username}@example.com')
    fake_imap.messages = {1: make_eml(subject=subject, attachments=attachments)}
    scheduler.sync_account(acc.id)
    db.expire_all()
    e = db.query(Email).filter_by(account_id=acc.id).one()
    assert e.analysis_status == 'queued'
    return e


def test_success_fills_columns(db, clock, fake_imap, fake_llm):
    e = _queued_mail(db, fake_imap)

    assert aw.run_once() is True
    db.expire_all()
    e = db.get(Email, e.id)
    assert e.analysis_status == 'done' and e.is_analyzed
    assert e.priority == '높음' and e.req_type == '견적요청' and e.company == 'A사'
    assert e.summary == '견적서 제출 요청'
    assert e.deadline == date(2026, 10, 20) and not e.deadline_manual
    assert e.analysis_retry_count == 0 and e.analysis_last_error is None
    assert aw.run_once() is False


def test_user_prompt_applies_to_auto_analysis(db, clock, fake_imap, fake_llm):
    import db as dbm
    u = make_user(db)
    db.add(dbm.Setting(user_id=u.id, key='analysis_prompt', value_enc=dbm.encrypt('금액은 원화로')))
    db.commit()
    _queued_mail(db, fake_imap, user=u)
    aw.run_once()
    assert '금액은 원화로' in fake_llm.calls[0]['system_prompt']


def test_transient_errors_back_off_then_fail(db, clock, fake_imap, fake_llm):
    e = _queued_mail(db, fake_imap)
    delays = [2, 5, 15, 30, 60]
    for attempt in range(1, 7):
        fake_llm.push(llm_provider.LLMError('timed out', kind='transient'))
        assert aw.run_once() is True
        db.expire_all()
        row = db.get(Email, e.id)
        assert row.analysis_retry_count == attempt
        if attempt < 6:
            assert row.analysis_status == 'queued'
            assert row.analysis_next_at == clock.now + timedelta(minutes=delays[attempt - 1])
            assert aw.run_once() is False                       # 아직 재시도 시각 전
            clock.advance(minutes=delays[attempt - 1], seconds=1)
        else:
            assert row.analysis_status == 'failed'
            assert row.analysis_last_error.startswith('[일시 오류]')


def test_one_failing_mail_does_not_stall_the_queue(db, clock, fake_imap, fake_llm):
    u = make_user(db)
    acc = make_account(db, u)
    from datetime import datetime, timezone
    fake_imap.messages = {
        1: make_eml(subject='older', date=datetime(2026, 10, 12, tzinfo=timezone.utc)),
        2: make_eml(subject='newer', date=datetime(2026, 10, 13, tzinfo=timezone.utc)),
    }
    scheduler.sync_account(acc.id)
    fake_llm.push(llm_provider.LLMError('timed out', kind='transient'))
    aw.run_once()                                   # 'newer' 실패 → 그 메일만 2분 뒤 재시도
    assert aw.run_once() is False                   # 사용자 대기열은 잠깐(1분) 쉰다
    clock.advance(minutes=1, seconds=1)
    assert aw.run_once() is True                    # 다른 메일은 1분 뒤 계속 진행
    db.expire_all()
    st = {e.subject: e.analysis_status for e in db.query(Email).filter_by(account_id=acc.id)}
    assert st == {'older': 'done', 'newer': 'queued'}


def test_config_error_pauses_only_that_user(db, clock, fake_imap, fake_llm):
    alice = make_user(db, 'alice')
    bob = make_user(db, 'bob')
    ea = _queued_mail(db, fake_imap, user=alice)
    clock.advance(seconds=1)
    eb = _queued_mail(db, fake_imap, user=bob)

    # bob 메일이 최신이라 먼저 처리됨 → 설정 오류
    fake_llm.push(llm_provider.LLMError('API 키 없음', kind='config'))
    aw.run_once()
    db.expire_all()
    b = db.get(Email, eb.id)
    assert b.analysis_status == 'queued' and b.analysis_retry_count == 0
    assert aw.pause_info(bob.id)['reason'].startswith('[설정 오류]')

    aw.run_once()                                   # alice는 계속 진행
    db.expire_all()
    assert db.get(Email, ea.id).analysis_status == 'done'
    assert aw.run_once() is False                   # bob은 일시정지 중

    aw.clear_pause(bob.id)                          # 설정 저장 시
    aw.run_once()
    db.expire_all()
    assert db.get(Email, eb.id).analysis_status == 'done'


def test_parse_error_retries_once(db, clock, fake_imap, fake_llm):
    e = _queued_mail(db, fake_imap)
    fake_llm.push(llm_provider.LLMParseError('bad json'), llm_provider.LLMParseError('bad json'))
    aw.run_once()
    db.expire_all()
    row = db.get(Email, e.id)
    assert row.analysis_status == 'queued' and row.analysis_next_at == clock.now + timedelta(minutes=1)
    clock.advance(minutes=2)
    aw.run_once()
    db.expire_all()
    assert db.get(Email, e.id).analysis_status == 'failed'


def test_invalid_error_keeps_previous_result(db, clock, fake_imap, fake_llm):
    e = _queued_mail(db, fake_imap)
    aw.run_once()
    es.queue_analysis(db, [e.id])
    fake_llm.push(llm_provider.LLMError('400 bad request', kind='invalid'))
    aw.run_once()
    db.expire_all()
    row = db.get(Email, e.id)
    assert row.analysis_status == 'done' and row.is_analyzed
    assert row.analysis_last_error.startswith('[요청 거부]')
    assert row.summary == '견적서 제출 요청'


def test_reanalysis_preserves_user_state_and_sends_images(db, clock, fake_imap, fake_llm):
    e = _queued_mail(db, fake_imap, attachments=[('photo.png', png_bytes(), 'image', 'png')])
    aw.run_once()
    row = db.get(Email, e.id)
    row.is_completed, row.is_read = True, True
    row.deadline, row.deadline_manual = date(2026, 11, 1), True
    db.commit()

    es.queue_analysis(db, [e.id])
    fake_llm.push({'요구사항_요약': '재분석 결과', '상세_요구사항': ['x'], '마감일': '2026-12-31', '우선순위': '보통'})
    aw.run_once()

    db.expire_all()
    row = db.get(Email, e.id)
    assert row.summary == '재분석 결과' and row.priority == '보통'
    assert row.is_completed and row.is_read
    assert row.deadline == date(2026, 11, 1)          # 수동 마감일 보존
    sent = fake_llm.calls[-1]['attachments']
    assert sent[0]['type'] == 'image' and sent[0]['content']   # 디스크 원본에서 복원
    assert json.loads(row.attachments_json)[0]['content'] is None   # DB에는 저장 안 함


def test_running_mail_cancelled_or_deleted_is_discarded(db, clock, fake_imap, fake_llm, monkeypatch):
    eid = _queued_mail(db, fake_imap).id

    def delete_during_call(*a, **kw):
        s = __import__('db').SessionLocal()
        s.delete(s.get(Email, eid))
        s.commit()
        s.close()
        return {'요구사항_요약': 'x'}, {'provider': 'remote', 'model': 'm', 'tokens_in': 1, 'tokens_out': 1}
    monkeypatch.setattr(llm_provider, 'analyze', delete_during_call)
    assert aw.run_once() is True
    db.expunge_all()
    assert db.get(Email, eid) is None


def test_start_resets_stuck_running(db, clock, fake_imap, monkeypatch):
    e = _queued_mail(db, fake_imap)
    row = db.get(Email, e.id)
    row.analysis_status = 'running'
    db.commit()
    monkeypatch.setattr(aw.threading.Thread, 'start', lambda self: None)
    aw._thread = None
    aw.start()
    db.expire_all()
    assert db.get(Email, e.id).analysis_status == 'queued'
    aw._thread = None
