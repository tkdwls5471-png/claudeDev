"""테스트용 가짜 메일 서버 / LLM / EML 생성기."""
import io
import email.utils
from datetime import datetime, timezone
from email.message import EmailMessage

import imap_connector


def make_eml(subject='견적 요청', body='견적 부탁드립니다.', sender='client@a.com',
             date=None, message_id=None, html=True, attachments=()):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = 'me@example.com'
    msg['Date'] = email.utils.format_datetime(date or datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc))
    if message_id:
        msg['Message-ID'] = message_id
    msg.set_content(body)
    if html:
        msg.add_alternative(f'<p>{body}</p><img src="https://tracker.example/p.gif">', subtype='html')
    for fname, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    return msg.as_bytes()


def png_bytes(w=40, h=30):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (w, h), (200, 30, 30)).save(buf, format='PNG')
    return buf.getvalue()


class FakeSession:
    def __init__(self, server):
        self.server = server

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        pass

    def select_inbox(self, folder='INBOX'):
        return self.server.uidvalidity

    def search_uids(self, min_uid=0):
        uids = sorted(self.server.messages)
        if min_uid > 0:
            res = [u for u in uids if u >= min_uid]
            # 실제 서버처럼 'n:*'는 더 큰 UID가 없어도 마지막 UID를 돌려준다
            return res or uids[-1:]
        return uids

    def fetch(self, uid):
        self.server.fetched.append(uid)
        return self.server.messages.get(int(uid))


class FakeImap:
    def __init__(self):
        self.messages = {}
        self.uidvalidity = 1
        self.login_error = False
        self.fetched = []

    def open_session(self, server, port, username, password, use_ssl=True):
        if self.login_error:
            raise imap_connector.ImapLoginError('AUTHENTICATIONFAILED')
        return FakeSession(self)


DEFAULT_RESULT = {
    '요구사항_요약': '견적서 제출 요청',
    '상세_요구사항': ['단가 산정', '납기 회신'],
    '수신일': '2026-10-13 09:00',
    '마감일': '2026-10-20',
    '마감일_원문': '10월 20일까지',
    '요청_업체': 'A사',
    '요청_유형': '견적요청',
    '우선순위': '높음 (마감 임박)',
    '추가_참고사항': '',
}


class FakeLLM:
    """analyze 호출을 기록하고, 미리 넣어 둔 결과(또는 예외)를 순서대로 돌려준다."""

    def __init__(self):
        self.outcomes = []
        self.calls = []

    def push(self, *outcomes):
        self.outcomes.extend(outcomes)

    def analyze(self, eml_data, attachments, system_prompt=None, **kw):
        self.calls.append({'eml': eml_data, 'attachments': attachments, 'system_prompt': system_prompt, **kw})
        out = self.outcomes.pop(0) if self.outcomes else dict(DEFAULT_RESULT)
        if isinstance(out, Exception):
            raise out
        return out, {'provider': 'remote', 'model': 'fake-model', 'tokens_in': 10, 'tokens_out': 5}
