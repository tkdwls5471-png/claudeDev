import pytest

import email_service as es
import scheduler
from db import Email
from conftest import make_user, make_account, login
from fakes import make_eml


@pytest.mark.parametrize('sender, rules, hit', [
    ('Anthropic <no-reply@mail.anthropic.com>', [('domain', 'anthropic.com')], ('domain', 'anthropic.com')),
    ('x@anthropic.com', [('domain', 'anthropic.com')], ('domain', 'anthropic.com')),
    ('x@notanthropic.com', [('domain', 'anthropic.com')], None),          # 하위 도메인만, 이름이 겹치는 다른 도메인은 아님
    ('"Slack" <No-Reply@Slack.com>', [('sender', 'no-reply@slack.com')], ('sender', 'no-reply@slack.com')),
    ('other@slack.com', [('sender', 'no-reply@slack.com')], None),
    ('', [('domain', 'a.com')], None),
])
def test_match_rule(sender, rules, hit):
    assert es.match_rule(sender, rules) == hit


@pytest.mark.parametrize('kind, value, norm', [
    ('domain', '@Mail.Anthropic.com ', 'mail.anthropic.com'),
    ('domain', 'someone@gabia.com', 'gabia.com'),
    ('sender', 'Gabia <NoReply_Notice@gabia.com>', 'noreply_notice@gabia.com'),
])
def test_normalize_rule(kind, value, norm):
    assert es.normalize_rule(kind, value) == norm


@pytest.mark.parametrize('kind, value', [('domain', 'localhost'), ('sender', 'not-an-email'), ('regex', 'x')])
def test_normalize_rule_rejects(kind, value):
    with pytest.raises(ValueError):
        es.normalize_rule(kind, value)


def test_sync_auto_excludes_matching_sender(db, clock, fake_imap):
    import db as dbm
    u = make_user(db)
    acc = make_account(db, u)
    db.add(dbm.SenderRule(user_id=u.id, kind='domain', value='slack.com'))
    db.commit()
    fake_imap.messages = {1: make_eml(subject='work', sender='client@a.com'),
                          2: make_eml(subject='notice', sender='Slack <no-reply@slack.com>')}
    scheduler.sync_account(acc.id)
    db.expire_all()
    rows = {e.subject: e for e in db.query(Email).filter_by(account_id=acc.id)}
    assert rows['work'].analysis_status == 'queued' and not rows['work'].is_excluded
    assert rows['notice'].is_excluded and rows['notice'].analysis_status == 'none'
    assert rows['notice'].exclude_reason == '도메인 규칙: slack.com'


def test_rule_api_applies_to_existing_and_restores(db, clock, fake_imap, app):
    u = make_user(db, 'alice', 'pw-a')
    acc = make_account(db, u)
    fake_imap.messages = {1: make_eml(subject='n1', sender='news@mail.ads.com'),
                          2: make_eml(subject='n2', sender='promo@ads.com'),
                          3: make_eml(subject='done', sender='promo@ads.com'),
                          4: make_eml(subject='work', sender='client@a.com')}
    scheduler.sync_account(acc.id)
    done = db.query(Email).filter_by(subject='done').one()
    done.is_completed = True
    db.commit()
    c = login(app, 'alice', 'pw-a')

    p = c.post('/api/sender-rules/preview', json={'kind': 'domain', 'value': 'ADS.com'}).get_json()
    assert p['value'] == 'ads.com' and p['matches'] == 3

    r = c.post('/api/sender-rules', json={'kind': 'domain', 'value': 'ads.com'}).get_json()
    assert r['affected'] == 2                                  # 완료한 메일은 건드리지 않음
    db.expire_all()
    st = {e.subject: (e.is_excluded, e.analysis_status) for e in db.query(Email)}
    assert st['n1'] == (True, 'none') and st['n2'] == (True, 'none') and st['done'][0] is False
    assert st['work'] == (False, 'queued')

    lst = c.get('/api/sender-rules').get_json()
    assert lst['rules'][0]['excluded_count'] == 2

    # 하나는 직접 복원 → 규칙 표시가 지워지고, 규칙 삭제 시 복원 대상에서도 빠짐
    n1 = db.query(Email).filter_by(subject='n1').one()
    c.patch(f'/api/emails/{n1.id}', json={'is_excluded': False})
    c.patch(f'/api/emails/{n1.id}', json={'is_excluded': True})
    d = c.delete(f"/api/sender-rules/{r['rule']['id']}?restore=1").get_json()
    assert d['restored'] == 1
    db.expire_all()
    assert db.query(Email).filter_by(subject='n2').one().is_excluded is False
    assert db.query(Email).filter_by(subject='n1').one().is_excluded is True    # 직접 제외한 건 유지


def test_rules_are_per_user(db, clock, app):
    make_user(db, 'alice', 'pw-a')
    make_user(db, 'bob', 'pw-b')
    a, b = login(app, 'alice', 'pw-a'), login(app, 'bob', 'pw-b')
    rid = a.post('/api/sender-rules', json={'kind': 'domain', 'value': 'x.com'}).get_json()['rule']['id']
    assert b.get('/api/sender-rules').get_json()['rules'] == []
    assert b.delete(f'/api/sender-rules/{rid}').status_code == 404
    assert a.post('/api/sender-rules', json={'kind': 'domain', 'value': 'bad'}).status_code == 400


def test_upload_ignores_rules(db, clock, app):
    import io
    import db as dbm
    u = make_user(db, 'alice', 'pw-a')
    db.add(dbm.SenderRule(user_id=u.id, kind='domain', value='ads.com'))
    db.commit()
    c = login(app, 'alice', 'pw-a')
    r = c.post('/api/upload', data={'file': (io.BytesIO(make_eml(sender='promo@ads.com')), 'a.eml')},
               content_type='multipart/form-data').get_json()
    assert r['created']['is_excluded'] is False       # 직접 올린 메일은 규칙을 적용하지 않음


def test_suggest_domains(db, clock, fake_imap):
    u = make_user(db)
    acc = make_account(db, u, email='me@uxis.co.kr')
    fake_imap.messages = {i: make_eml(subject=f's{i}', sender=s) for i, s in enumerate(
        ['a@news.com', 'b@news.com', 'c@news.com', 'x@uxis.co.kr', 'y@uxis.co.kr', 'z@once.com'], 1)}
    scheduler.sync_account(acc.id)
    assert es.suggest_domains(db, u.id) == [{'domain': 'news.com', 'count': 3}]
