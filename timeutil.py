"""시간 도우미.

DB의 DATETIME 컬럼은 모두 naive UTC로 저장하고, 화면 표시와 '오늘' 같은 날짜 경계는
KST(UTC+9) 기준으로 계산한다. 테스트에서 시계를 바꿀 수 있도록 다른 모듈은
반드시 `timeutil.utcnow()`로 현재 시각을 얻는다.
"""
import re
from datetime import datetime, date, timedelta, timezone

KST = timezone(timedelta(hours=9))
_KST_OFFSET = timedelta(hours=9)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def kst_today() -> date:
    return (utcnow() + _KST_OFFSET).date()


def kst_day_start_utc(d: date) -> datetime:
    """KST 기준 d일 00:00을 naive UTC로."""
    return datetime(d.year, d.month, d.day) - _KST_OFFSET


def to_kst_str(dt, fmt='%Y-%m-%d %H:%M'):
    """naive UTC → KST 문자열 (None이면 None)."""
    if not dt:
        return None
    return (dt + _KST_OFFSET).strftime(fmt)


_KST_RE = re.compile(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?')


def parse_kst(s):
    """'YYYY-MM-DD[ HH:MM[:SS]]' 형식의 KST 문자열 → naive UTC (해석 불가면 None)."""
    if not s:
        return None
    m = _KST_RE.match(str(s))
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        return datetime(y, mo, d, hh, mm, ss) - _KST_OFFSET
    except ValueError:
        return None


def parse_ymd(s):
    """'YYYY-MM-DD' → date (형식이 다르면 None)."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        return None
