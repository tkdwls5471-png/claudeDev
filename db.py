import os
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, SmallInteger, String, Text, Boolean,
    Date, DateTime, ForeignKey, UniqueConstraint, Index, JSON, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, deferred
from sqlalchemy.engine import URL
from cryptography.fernet import Fernet
import bcrypt

load_dotenv()

Base = declarative_base()

# 새로 추가하는 NOT NULL 컬럼은 server_default를 둔다 → 롤백한 옛 코드도 INSERT 가능
_FALSE = text('0')


# ─── Models ──────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = 'eml_users'
    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    accounts      = relationship('EmailAccount', backref='user', cascade='all, delete-orphan')
    settings      = relationship('Setting',      backref='user', cascade='all, delete-orphan')


class EmailAccount(Base):
    __tablename__ = 'eml_accounts'
    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(Integer, ForeignKey('eml_users.id'), nullable=False)
    label             = Column(String(100))
    email_address     = Column(String(255), nullable=False)
    imap_server       = Column(String(255), nullable=False)
    imap_port         = Column(Integer, default=993)
    imap_username     = Column(String(255), nullable=False)
    imap_password_enc = Column(Text, nullable=False)   # Fernet 암호화
    use_ssl           = Column(Boolean, default=True)
    last_uid          = Column(Integer, default=0)     # 마지막 수신 UID
    last_synced       = Column(DateTime)               # 마지막 동기화 시도 시각 (UTC)
    is_active         = Column(Boolean, default=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    # 'imap' = 실제 메일 계정, 'upload' = 사용자별 업로드 EML 보관용 가상 계정
    kind              = Column(String(10), nullable=False, default='imap', server_default='imap')
    uidvalidity       = Column(BigInteger)
    last_sync_status  = Column(String(10))             # ok | error
    last_sync_error   = Column(Text)
    last_sync_new     = Column(Integer)
    emails            = relationship('Email', backref='account', cascade='all, delete-orphan')


class Email(Base):
    __tablename__ = 'eml_emails'
    id               = Column(Integer, primary_key=True, autoincrement=True)
    account_id       = Column(Integer, ForeignKey('eml_accounts.id'), nullable=False)
    message_uid      = Column(String(255), nullable=False)
    subject          = Column(Text)
    sender           = Column(String(500))
    recipient        = Column(Text)
    received_date    = Column(String(50))                # 표시용 KST 문자열 (구버전 호환)
    body_text        = Column(Text(16777215))           # MEDIUMTEXT
    attachments_json = Column(Text(16777215))           # 처리된 첨부파일 JSON (이미지 base64는 저장하지 않음)
    is_analyzed      = Column(Boolean, default=False)   # 실제 분석 결과가 있음
    analysis_result  = Column(JSON(none_as_null=True))  # LLM 출력만 담는다 (작업 상태는 아래 컬럼)
    analyzed_at      = Column(DateTime)
    analysis_provider   = Column(String(30))    # remote / anthropic / openai
    analysis_model      = Column(String(80))    # 실제 사용 모델명
    analysis_tokens_in  = Column(Integer, default=0)
    analysis_tokens_out = Column(Integer, default=0)
    analysis_retry_count = Column(Integer, default=0)   # 연속 실패 횟수
    analysis_last_error  = Column(Text)                 # 마지막 분석 실패 원인
    created_at       = Column(DateTime, default=datetime.utcnow)

    # ── 작업 상태 (분석 결과와 분리 → 재분석해도 보존) ──
    is_read          = Column(Boolean, nullable=False, default=False, server_default=_FALSE)
    is_completed     = Column(Boolean, nullable=False, default=False, server_default=_FALSE)
    completed_at     = Column(DateTime)
    is_excluded      = Column(Boolean, nullable=False, default=False, server_default=_FALSE)
    exclude_reason   = Column(String(300))               # 규칙으로 자동 제외된 경우 그 규칙 (직접 제외면 NULL)
    deadline         = Column(Date)
    deadline_manual  = Column(Boolean, nullable=False, default=False, server_default=_FALSE)

    # ── 목록·필터용 비정규화 컬럼 (JSON을 읽지 않고 SQL로 처리) ──
    priority         = Column(String(10))
    req_type         = Column(String(50))
    company          = Column(String(255))
    summary          = Column(Text)
    has_attachments  = Column(Boolean, nullable=False, default=False, server_default=_FALSE)
    received_at      = Column(DateTime)                 # UTC
    message_id       = Column(String(255))
    raw_stored       = Column(String(64))               # 업로드 원본 .eml 디스크 파일명
    body_html        = deferred(Column(Text(16777215)))  # 원본 HTML 캐시 ('' = HTML 파트 없음)

    # ── 분석 대기열 ──
    analysis_status   = Column(String(10), nullable=False, default='none', server_default='none')
    analysis_next_at  = Column(DateTime)
    analysis_priority = Column(SmallInteger, nullable=False, default=0, server_default=_FALSE)

    __table_args__   = (
        UniqueConstraint('account_id', 'message_uid', name='uq_eml_account_message'),
        Index('ix_eml_emails_acc_received', 'account_id', 'received_at'),
        Index('ix_eml_emails_acc_state', 'account_id', 'is_completed', 'is_excluded'),
        Index('ix_eml_emails_acc_deadline', 'account_id', 'deadline'),
        Index('ix_eml_emails_acc_analyzed', 'account_id', 'analyzed_at'),
        Index('ix_eml_emails_acc_msgid', 'account_id', 'message_id'),
        Index('ix_eml_emails_queue', 'analysis_status', 'analysis_next_at'),
    )


class SenderRule(Base):
    """발신자·도메인 제외 규칙: 맞는 메일은 수신 즉시 '제외'로 보내고 분석하지 않는다."""
    __tablename__ = 'eml_sender_rules'
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey('eml_users.id'), nullable=False)
    kind       = Column(String(10), nullable=False)      # sender | domain
    value      = Column(String(255), nullable=False)     # 소문자 메일 주소 또는 도메인
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('user_id', 'kind', 'value', name='uq_eml_sender_rule'),)


class Setting(Base):
    __tablename__ = 'eml_settings'
    id        = Column(Integer, primary_key=True, autoincrement=True)
    user_id   = Column(Integer, ForeignKey('eml_users.id'), nullable=False)
    key       = Column(String(100), nullable=False)
    value_enc = Column(Text)   # Fernet 암호화
    __table_args__ = (UniqueConstraint('user_id', 'key', name='uq_eml_user_key'),)


# ─── Engine / Session ─────────────────────────────────────────────────────────

def _build_engine():
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:  # 테스트/로컬 개발용 (예: sqlite:///dev.db)
        kwargs = {'connect_args': {'check_same_thread': False}} if url.startswith('sqlite') else {}
        return create_engine(url, **kwargs)
    # 접속 정보는 .env에서만 읽는다 (저장소에 서버 주소·계정을 남기지 않음)
    missing = [k for k in ('DB_HOST', 'DB_USER', 'DB_NAME') if not os.environ.get(k, '').strip()]
    if missing:
        raise RuntimeError(f'.env에 DB 설정이 없습니다: {", ".join(missing)} (.env.example 참고)')
    db_url = URL.create(
        drivername='mysql+pymysql',
        username=os.environ['DB_USER'].strip(),
        password=os.environ.get('DB_PASSWORD', ''),
        host=os.environ['DB_HOST'].strip(),
        port=int(os.environ.get('DB_PORT', '3306').strip() or 3306),
        database=os.environ['DB_NAME'].strip(),
    )
    return create_engine(db_url, pool_pre_ping=True, pool_recycle=3600)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine)


def init_db():
    """테이블 생성 + 스키마 보강(마이그레이션) + Fernet 키 확인."""
    ensure_fernet_key()
    Base.metadata.create_all(engine)
    import migrate
    migrate.run(engine)
    print('[db] Tables ready.')


# ─── Fernet 암호화 ─────────────────────────────────────────────────────────────

def ensure_fernet_key():
    key = os.environ.get('FERNET_KEY', '').strip()
    if not key:
        key = Fernet.generate_key().decode()
        os.environ['FERNET_KEY'] = key
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        with open(env_path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('FERNET_KEY=', f'FERNET_KEY={key}')
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print('[db] FERNET_KEY 자동 생성 완료.')


def _fernet() -> Fernet:
    key = os.environ.get('FERNET_KEY', '').strip()
    if not key:
        raise RuntimeError('FERNET_KEY가 설정되지 않았습니다.')
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode()).decode()


# ─── bcrypt 헬퍼 ──────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())
