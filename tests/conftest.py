"""테스트 공통 설정: SQLite 임시 DB + 백그라운드 작업 끔 + 조절 가능한 시계 + 가짜 IMAP/LLM.

환경변수는 db·attachment_store를 import 하기 전에 정해야 한다.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix='eml_test_')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'test.db').replace('\\', '/')
os.environ['EML_DISABLE_BACKGROUND'] = '1'
os.environ['ATTACHMENT_STORE_DIR'] = os.path.join(_TMP, 'store')
from cryptography.fernet import Fernet  # noqa: E402
os.environ['FERNET_KEY'] = Fernet.generate_key().decode()

import pytest  # noqa: E402

import timeutil  # noqa: E402
import db as dbm  # noqa: E402
import migrate  # noqa: E402
import analysis_worker  # noqa: E402
import scheduler  # noqa: E402
import imap_connector  # noqa: E402
import llm_provider  # noqa: E402

from fakes import FakeImap, FakeLLM  # noqa: E402


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture(autouse=True)
def fresh_db():
    dbm.Base.metadata.drop_all(dbm.engine)
    dbm.Base.metadata.create_all(dbm.engine)
    migrate.run(dbm.engine)
    analysis_worker._pauses.clear()
    analysis_worker._current.clear()
    scheduler._login_backoff.clear()
    yield


@pytest.fixture
def clock(monkeypatch):
    c = Clock(datetime(2026, 10, 14, 1, 0, 0))   # = 2026-10-14 10:00 KST
    monkeypatch.setattr(timeutil, 'utcnow', c)
    return c


@pytest.fixture
def db():
    s = dbm.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def fake_imap(monkeypatch):
    f = FakeImap()
    monkeypatch.setattr(imap_connector, 'open_session', f.open_session)
    return f


@pytest.fixture
def fake_llm(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(llm_provider, 'analyze', f.analyze)
    return f


def make_user(db, name='alice', password='pw-alice', auto=True):
    u = dbm.User(username=name, password_hash=dbm.hash_password(password))
    db.add(u)
    db.commit()
    if not auto:
        db.add(dbm.Setting(user_id=u.id, key='auto_analyze_enabled', value_enc=dbm.encrypt('false')))
        db.commit()
    return u


def make_account(db, user, email='a@example.com', **kw):
    acc = dbm.EmailAccount(
        user_id=user.id, label=email, email_address=email, imap_server='imap.example.com',
        imap_port=993, imap_username=email, imap_password_enc=dbm.encrypt('secret'),
        use_ssl=True, kind='imap', **kw)
    db.add(acc)
    db.commit()
    return acc


@pytest.fixture
def app():
    import flask_app
    flask_app.app.config['TESTING'] = True
    return flask_app.app


def login(app, name, password):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': name, 'password': password})
    assert r.status_code == 200, r.get_json()
    return c
