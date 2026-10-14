"""로컬 개발 서버 — 운영 DB·실제 메일 서버·LLM에 접속하지 않는다.

- DB: .devdata/dev.db (SQLite)
- 메일 서버: 가짜 IMAP (샘플 메일 40여 통, 90초마다 새 메일 1통 도착)
- LLM: 가짜 분석기 (1.5초 걸리고, 제목에 '실패'가 들어가면 일시 오류를 낸다)
- 로그인: demo / demo1234

실행: python scripts/dev_server.py   →  http://localhost:5055
"""
import os
import sys
import time
import hashlib
import threading
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

DATA = os.path.join(ROOT, '.devdata')
os.makedirs(DATA, exist_ok=True)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(DATA, 'dev.db').replace('\\', '/')
os.environ['ATTACHMENT_STORE_DIR'] = os.path.join(DATA, 'store')
os.environ['SYNC_INTERVAL_MIN'] = os.environ.get('SYNC_INTERVAL_MIN', '1')
_key_file = os.path.join(DATA, 'fernet.key')
if not os.path.exists(_key_file):
    from cryptography.fernet import Fernet
    with open(_key_file, 'w') as f:
        f.write(Fernet.generate_key().decode())
os.environ['FERNET_KEY'] = open(_key_file).read().strip()

DEV_USER, DEV_PASSWORD = 'demo', 'demo1234'

import imap_connector  # noqa: E402
import llm_provider  # noqa: E402
from fakes import FakeImap, make_eml, png_bytes  # noqa: E402

# ─── 가짜 메일 서버 ───────────────────────────────────────────────────────────
COMPANIES = ['A테크', '한빛건설', '서울물산', '미래로직스', '그린에너지', '다온상사']
TOPICS = [
    ('견적 요청드립니다 - 서버 증설', '견적요청'), ('주간 보고서 자료 요청', '보고서/자료 요청'),
    ('홈페이지 메인 배너 수정 요청', '수정/작업요청'), ('계약서 초안 검토 부탁드립니다', '검토요청'),
    ('다음 주 화요일 회의 일정 문의', '회의요청'), ('3차 개발 작업 완료 보고', '작업완료보고'),
    ('(실패 테스트) 첨부 해석 불가 메일', '기타'), ('납품 일정 협의 요청', '기타'),
]

fake_imap = FakeImap()
fake_imap.uidvalidity = 7
_now = datetime.now(timezone.utc)
for i in range(1, 43):
    topic, _ = TOPICS[i % len(TOPICS)]
    company = COMPANIES[i % len(COMPANIES)]
    received = _now - timedelta(days=(42 - i) * 0.9, hours=i % 7)
    atts = [('사진.png', png_bytes(80 + i, 60), 'image', 'png')] if i % 9 == 0 else []
    fake_imap.messages[i] = make_eml(
        subject=f'[{company}] {topic} #{i}', sender=f'{company} 담당자 <manager{i}@{company}.co.kr>',
        body=f'안녕하세요, {company}입니다.\n{topic} 관련하여 연락드립니다.\n가능하면 이번 달 안에 회신 부탁드립니다.',
        date=received, message_id=f'<dev-{i}@example.com>', attachments=atts)


def _arrivals():
    n = 100
    while True:
        time.sleep(90)
        n += 1
        topic, _ = TOPICS[n % len(TOPICS)]
        company = COMPANIES[n % len(COMPANIES)]
        fake_imap.messages[n] = make_eml(
            subject=f'[{company}] {topic} (새 메일 {n})', sender=f'manager@{company}.co.kr',
            body=f'{topic} 건으로 연락드립니다.', date=datetime.now(timezone.utc), message_id=f'<dev-new-{n}@example.com>')
        print(f'[dev] 새 메일 도착 UID {n}')


imap_connector.open_session = fake_imap.open_session

# ─── 가짜 LLM ────────────────────────────────────────────────────────────────
def fake_analyze(eml_data, attachments, system_prompt=None, **kw):
    time.sleep(1.5)
    subject = eml_data.get('subject', '')
    if '실패' in subject:
        raise llm_provider.LLMError('Request timed out (dev 가짜 오류)', kind='transient')
    h = int(hashlib.md5(subject.encode()).hexdigest(), 16)
    rtype = next((t for s, t in TOPICS if s.split(' - ')[0][:6] in subject), '기타')
    received = (eml_data.get('received_date') or '')[:10] or datetime.now().strftime('%Y-%m-%d')
    deadline = (datetime.strptime(received, '%Y-%m-%d') + timedelta(days=2 + h % 12)).strftime('%Y-%m-%d') if h % 5 else '명시되지 않음'
    result = {
        '요구사항_요약': f'{subject}에 대한 회신과 처리를 요청하는 메일입니다.',
        '상세_요구사항': ['요청 내용 확인', '담당자 지정', f'{deadline}까지 회신'],
        '수신일': eml_data.get('received_date', ''),
        '마감일': deadline, '마감일_원문': '이번 달 안에' if h % 5 else '',
        '요청_업체': subject.split(']')[0].strip('['), '요청_유형': rtype,
        '우선순위': ['높음', '보통', '낮음'][h % 3], '추가_참고사항': '' if h % 4 else '첨부 자료를 함께 확인해 주세요.',
    }
    return result, {'provider': 'remote', 'model': 'dev-fake', 'tokens_in': 900 + h % 300, 'tokens_out': 200 + h % 80}


llm_provider.analyze = fake_analyze

# ─── 앱 시작 + 시드 ───────────────────────────────────────────────────────────
import flask_app  # noqa: E402
from db import SessionLocal, User, EmailAccount, hash_password, encrypt  # noqa: E402


def _seed():
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=DEV_USER).first()
        if not user:
            user = User(username=DEV_USER, password_hash=hash_password(DEV_PASSWORD))
            db.add(user)
            db.commit()
        if not db.query(EmailAccount).filter_by(user_id=user.id, kind='imap').first():
            db.add(EmailAccount(user_id=user.id, label='개발용 메일함', email_address='demo@example.com',
                                imap_server='imap.example.com', imap_port=993, imap_username='demo@example.com',
                                imap_password_enc=encrypt('x'), use_ssl=True, kind='imap'))
            db.commit()
    finally:
        db.close()


if __name__ == '__main__':
    _seed()
    threading.Thread(target=_arrivals, daemon=True).start()
    port = int(os.environ.get('PORT', 5055))
    print(f'[dev] http://localhost:{port}  (로그인 {DEV_USER} / {DEV_PASSWORD})')
    flask_app.app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
