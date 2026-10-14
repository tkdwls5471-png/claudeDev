import io
from datetime import date, datetime

import pytest

import analysis_worker as aw
import email_service as es
from db import Email
from conftest import make_user, make_account, login
from fakes import make_eml


def _mail(db, acc, subject, *, received=datetime(2026, 10, 13, 0, 0), deadline=None, completed=False,
          excluded=False, status='done', priority=None, rtype=None, read=True, analyzed_at=None):
    e = Email(account_id=acc.id, message_uid=f'u-{subject}', subject=subject, sender='x@a.com',
              received_at=received, received_date='', body_text=f'{subject} 본문', attachments_json='[]',
              deadline=deadline, is_completed=completed, is_excluded=excluded, analysis_status=status,
              priority=priority, req_type=rtype, is_read=read, analyzed_at=analyzed_at,
              is_analyzed=status == 'done')
    db.add(e)
    db.commit()
    return e


@pytest.fixture
def world(db, clock, app):
    alice = make_user(db, 'alice', 'pw-a')
    bob = make_user(db, 'bob', 'pw-b')
    acc = make_account(db, alice)
    today = date(2026, 10, 14)
    m = {
        'due3': _mail(db, acc, 'due3', deadline=date(2026, 10, 17), priority='높음', rtype='견적요청', read=False),
        'due4': _mail(db, acc, 'due4', deadline=date(2026, 10, 18), priority='보통'),
        'overdue': _mail(db, acc, 'overdue', deadline=date(2026, 10, 1), priority='낮음'),
        'nodl': _mail(db, acc, 'nodl'),
        'done': _mail(db, acc, 'done', completed=True, deadline=date(2026, 10, 15)),
        'excl': _mail(db, acc, 'excl', excluded=True, deadline=date(2026, 10, 15)),
        'fail': _mail(db, acc, 'fail', status='failed'),
    }
    return {'alice': alice, 'bob': bob, 'acc': acc, 'm': m, 'today': today,
            'a': login(app, 'alice', 'pw-a'), 'b': login(app, 'bob', 'pw-b')}


def _subjects(resp):
    return [r['subject'] for r in resp.get_json()['emails']]


def test_tabs_counts_and_due_boundary(world):
    c = world['a']
    j = c.get('/api/emails?tab=due').get_json()
    assert [r['subject'] for r in j['emails']] == ['overdue', 'due3']      # 마감일 오름차순
    assert j['counts'] == {'all': 7, 'todo': 5, 'due': 2, 'done': 1, 'excluded': 1, 'failed': 1, 'unread': 1}
    assert set(_subjects(c.get('/api/emails?tab=todo'))) == {'due3', 'due4', 'overdue', 'nodl', 'fail'}
    assert _subjects(c.get('/api/emails?tab=done')) == ['done']
    assert _subjects(c.get('/api/emails?tab=excluded')) == ['excl']
    assert _subjects(c.get('/api/emails?tab=failed')) == ['fail']


def test_deadline_sort_puts_nulls_last(world):
    subj = _subjects(world['a'].get('/api/emails?tab=todo&sort=deadline'))
    assert subj[:3] == ['overdue', 'due3', 'due4'] and set(subj[3:]) == {'nodl', 'fail'}


def test_filters_and_counts_respect_filters(world):
    c = world['a']
    j = c.get('/api/emails?tab=all&priority=높음&priority=낮음').get_json()
    assert set(_subjects_from(j)) == {'due3', 'overdue'} and j['counts']['all'] == 2
    assert _subjects(c.get('/api/emails?tab=all&unread=1')) == ['due3']
    assert _subjects(c.get('/api/emails?tab=all&type=견적요청')) == ['due3']
    assert set(_subjects(c.get('/api/emails?tab=all&q=overdue'))) == {'overdue'}
    # 기간(겹침): 10/16~10/31 → 마감이 그 안이거나 이후인 메일 + 마감 없는 메일 중 수신이 기간 안인 것
    j = c.get('/api/emails?tab=all&range_from=2026-10-16&range_to=2026-10-31').get_json()
    assert set(_subjects_from(j)) == {'due3', 'due4'}


def _subjects_from(j):
    return [r['subject'] for r in j['emails']]


def test_patch_updates_state_and_counts(world):
    c, m = world['a'], world['m']
    r = c.patch(f"/api/emails/{m['due3'].id}", json={'is_completed': True}).get_json()
    assert r['is_completed'] and r['completed_at']
    j = c.get('/api/emails?tab=due').get_json()
    assert _subjects_from(j) == ['overdue'] and j['counts']['done'] == 2

    r = c.patch(f"/api/emails/{m['nodl'].id}", json={'deadline': '2026-10-15'}).get_json()
    assert r['deadline'] == '2026-10-15' and r['deadline_manual']
    assert c.patch(f"/api/emails/{m['nodl'].id}", json={'deadline': 'tomorrow'}).status_code == 400


def test_deadline_reset_to_ai_value(world, db):
    c, e = world['a'], world['m']['nodl']
    row = db.get(Email, e.id)
    row.analysis_result = {'요구사항_요약': 'x', '마감일': '2026-10-30'}
    row.deadline, row.deadline_manual = date(2026, 11, 5), True
    db.commit()
    r = c.patch(f'/api/emails/{e.id}', json={'deadline_manual': False}).get_json()
    assert r['deadline'] == '2026-10-30' and not r['deadline_manual']


def test_detail_marks_read(world):
    c, e = world['a'], world['m']['due3']
    j = c.get(f'/api/emails/{e.id}?mark_read=1').get_json()
    assert j['is_read'] and 'body_text' in j and j['has_html'] is None


def test_bulk_actions(world, db):
    c, m = world['a'], world['m']
    ids = [m['due3'].id, m['due4'].id]
    assert c.post('/api/emails/bulk', json={'ids': ids, 'action': 'complete'}).get_json()['affected'] == 2
    assert c.post('/api/emails/bulk', json={'ids': ids, 'action': 'uncomplete'}).get_json()['affected'] == 2
    assert c.post('/api/emails/bulk', json={'ids': ids, 'action': 'analyze'}).get_json()['affected'] == 2
    db.expire_all()
    assert db.get(Email, ids[0]).analysis_status == 'queued'
    assert db.get(Email, ids[0]).analysis_priority == es.PRIORITY_MANUAL
    c.post('/api/emails/bulk', json={'ids': ids, 'action': 'exclude'})
    db.expire_all()
    assert db.get(Email, ids[0]).is_excluded and db.get(Email, ids[0]).analysis_status == 'none'
    assert c.post('/api/emails/bulk', json={'ids': ids, 'action': 'delete'}).get_json()['affected'] == 2
    assert c.post('/api/emails/bulk', json={'ids': ids, 'action': 'nope'}).status_code == 400


def test_other_user_cannot_touch_mail(world):
    b, e = world['b'], world['m']['due3']
    assert b.get(f'/api/emails/{e.id}').status_code == 404
    assert b.patch(f'/api/emails/{e.id}', json={'is_completed': True}).status_code == 404
    assert b.delete(f'/api/emails/{e.id}').status_code == 404
    assert b.post(f'/api/emails/{e.id}/analyze').status_code == 404
    assert b.get(f'/api/emails/{e.id}/body-html').status_code == 404
    assert b.get(f'/api/emails/{e.id}/attachment/0/raw').status_code == 404
    assert b.post('/api/emails/bulk', json={'ids': [e.id], 'action': 'delete'}).get_json()['affected'] == 0
    assert b.get('/api/emails?tab=all').get_json()['emails'] == []


def test_unauthenticated_api_gets_401_json(app):
    r = app.test_client().get('/api/emails')
    assert r.status_code == 401 and r.get_json()['login_required']


def test_upload_dedupes_and_queues(world, db):
    c = world['b']                          # bob: 계정 없음, 자동분석 켬
    raw = make_eml(subject='업로드 메일', message_id='<up1@a.com>')
    r = c.post('/api/upload', data={'file': (io.BytesIO(raw), 'q.eml')}, content_type='multipart/form-data')
    j = r.get_json()
    assert j['success'] and j['created']['source'] == 'upload'
    assert j['created']['analysis_status'] == 'queued'
    e = db.get(Email, j['created']['id'])
    assert e.analysis_priority == es.PRIORITY_MANUAL and e.raw_stored and e.is_read

    r2 = c.post('/api/upload', data={'file': (io.BytesIO(raw), 'q.eml')}, content_type='multipart/form-data')
    assert r2.get_json()['duplicate']['id'] == e.id

    # 업로드 메일도 원본 HTML을 볼 수 있다 (디스크 원본)
    h = c.get(f'/api/emails/{e.id}/body-html').get_json()
    assert h['has_html'] and 'data-blocked-src' in h['html']


def test_upload_dedupes_against_imap_mail(world, db, fake_imap):
    import scheduler
    raw = make_eml(subject='같은 메일', message_id='<same@a.com>')
    fake_imap.messages = {1: raw}
    scheduler.sync_account(world['acc'].id)
    r = world['a'].post('/api/upload', data={'file': (io.BytesIO(raw), 'same.eml')},
                        content_type='multipart/form-data').get_json()
    assert r['duplicate']['source'] == 'imap'


def test_legacy_import_is_idempotent(world):
    c = world['b']
    items = [{'filename': 'old.eml', 'timestamp': '2026-09-01T10:00:00',
              'result': {'요구사항_요약': '옛 결과', '마감일': '2026-09-10', '우선순위': '높음', '완료': True}}]
    assert c.post('/api/upload/legacy', json={'items': items}).get_json()['imported'] == 1
    assert c.post('/api/upload/legacy', json={'items': items}).get_json()['imported'] == 0
    rows = c.get('/api/emails?tab=done').get_json()['emails']
    assert rows[0]['subject'] == 'old.eml' and rows[0]['deadline'] == '2026-09-10' and rows[0]['source'] == 'upload'


def test_status_endpoint(world, monkeypatch):
    j = world['a'].get('/api/status').get_json()
    assert j['counts']['todo'] == 5 and j['analysis']['failed'] == 1
    assert j['sync']['accounts'][0]['email_address'] == 'a@example.com'
    assert j['latest_email_id'] == world['m']['fail'].id


def test_auto_analyze_toggle_queues_and_unqueues(world, db):
    c, e = world['a'], world['m']['nodl']
    row = db.get(Email, e.id)
    row.analysis_status = 'none'
    db.commit()
    c.put('/api/settings/auto-analyze', json={'enabled': True})
    db.expire_all()
    assert db.get(Email, e.id).analysis_status == 'queued'
    c.put('/api/settings/auto-analyze', json={'enabled': False})
    db.expire_all()
    assert db.get(Email, e.id).analysis_status == 'none'


def test_saving_llm_settings_clears_pause(world):
    from datetime import timedelta
    import timeutil
    aw._pause(world['alice'].id, timeutil.utcnow() + timedelta(minutes=10), '[설정 오류] x')
    world['a'].put('/api/settings/llm', json={'provider': 'remote', 'model': ''})
    assert aw.pause_info(world['alice'].id) is None


def test_sanitizer_blocks_remote_content():
    import flask_app
    html = ('<style>body{background:url(https://t.example/a.png)}</style>'
            '<div style="background-image:url(//t.example/b.png)" background="https://t.example/c.png">'
            '<img src="//t.example/d.png"><a href="javascript:alert(1)">x</a>'
            '<img src="x" onerror="alert(1)"></div><link rel=stylesheet href="https://t.example/s.css">')
    out = flask_app._sanitize_email_html(html, allow_remote_images=False)
    assert 't.example/a.png' not in out and 't.example/b.png' not in out and 't.example/c.png' not in out
    assert 'data-blocked-src="//t.example/d.png"' in out
    assert 'javascript:' not in out and 'onerror' not in out and '<link' not in out
