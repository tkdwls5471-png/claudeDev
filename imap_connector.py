import os
import imaplib
import ssl
from typing import List, Optional, Tuple

# 큰 메일함에서 'UID SEARCH ALL' 응답 한 줄이 기본 한도(1MB)를 넘을 수 있다
imaplib._MAXLINE = 10_000_000

IMAP_TIMEOUT = float(os.environ.get('IMAP_TIMEOUT_SEC', 30))

# 메일 서버 프리셋
PRESET_SERVERS = {
    'daum':   ('imap.daum.net',   993, True),
    'kakao':  ('imap.kakao.com',  993, True),
    'gmail':  ('imap.gmail.com',  993, True),
    'naver':  ('imap.naver.com',  993, True),
    'outlook':('outlook.office365.com', 993, True),
}


class ImapLoginError(Exception):
    """아이디·비밀번호(앱 비밀번호) 문제로 로그인이 거부됨."""


def _connect(server: str, port: int, username: str, password: str, use_ssl: bool) -> imaplib.IMAP4:
    if use_ssl:
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(server, port, ssl_context=ctx, timeout=IMAP_TIMEOUT)
    else:
        conn = imaplib.IMAP4(server, port, timeout=IMAP_TIMEOUT)
    try:
        conn.login(username, password)
    except imaplib.IMAP4.error as e:
        _close_quietly(conn)
        raise ImapLoginError(str(e)) from e
    except Exception:
        _close_quietly(conn)
        raise
    return conn


def _close_quietly(conn):
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


class ImapSession:
    """로그인된 IMAP 연결. with 문으로 쓰면 끝날 때 로그아웃한다."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        _close_quietly(self.conn)

    def select_inbox(self, folder: str = 'INBOX') -> Optional[int]:
        """폴더 선택(읽기 전용). UIDVALIDITY를 반환 (서버가 안 주면 None)."""
        typ, _ = self.conn.select(folder, readonly=True)
        if typ != 'OK':
            raise RuntimeError(f'{folder} 폴더를 열 수 없습니다')
        _, data = self.conn.response('UIDVALIDITY')
        try:
            return int(data[0]) if data and data[0] is not None else None
        except (TypeError, ValueError):
            return None

    def search_uids(self, min_uid: int = 0) -> List[int]:
        """min_uid 이상 UID 목록(오름차순). min_uid가 0이면 전체."""
        crit = f'UID {min_uid}:*' if min_uid > 0 else 'ALL'
        typ, data = self.conn.uid('search', None, crit)
        if typ != 'OK' or not data or not data[0]:
            return []
        uids = sorted({int(u) for u in data[0].decode().split() if u.isdigit()})
        # 'n:*'는 n보다 큰 UID가 없어도 마지막 UID를 돌려주므로 걸러낸다
        return [u for u in uids if u >= min_uid]

    def fetch(self, uid) -> Optional[bytes]:
        typ, msg_data = self.conn.uid('fetch', str(uid), '(RFC822)')
        if typ == 'OK' and msg_data:
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                    return part[1]
        return None


def open_session(server: str, port: int, username: str, password: str, use_ssl: bool = True) -> ImapSession:
    return ImapSession(_connect(server, port, username, password, use_ssl))


def test_connection(server: str, port: int, username: str, password: str, use_ssl: bool = True) -> Tuple[bool, str]:
    """IMAP 연결 테스트. (성공여부, 오류메시지) 반환."""
    try:
        open_session(server, port, username, password, use_ssl).close()
        return True, ''
    except ImapLoginError as e:
        return False, f'IMAP 인증 실패: {e}'
    except Exception as e:
        return False, str(e) or type(e).__name__


def fetch_email_by_uid(
    server: str, port: int, username: str, password: str,
    uid: str, use_ssl: bool = True, folder: str = 'INBOX'
) -> Optional[bytes]:
    """단일 UID의 원본 eml 바이트를 가져온다 (없으면 None)."""
    with open_session(server, port, username, password, use_ssl) as s:
        s.select_inbox(folder)
        return s.fetch(uid)
