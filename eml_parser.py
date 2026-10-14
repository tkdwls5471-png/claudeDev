import email
import email.header
import email.utils
from email import policy
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup

# 한국 표준시 (UTC+9, DST 없음)
KST = timezone(timedelta(hours=9))


def decode_header_value(value: str) -> str:
    """인코딩된 헤더 값을 디코딩"""
    if not value:
        return ""
    decoded_parts = email.header.decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                result.append(part.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def parse_date(date_str: str) -> Optional[str]:
    """이메일 날짜 문자열을 읽기 쉬운 형식으로 변환"""
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        # 오프셋 없는(naive) 값은 UTC로 간주 후, 항상 KST로 변환해 표시
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(KST)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str


def parse_date_utc(date_str: str) -> Optional[datetime]:
    """이메일 날짜 문자열 → naive UTC datetime (해석 실패 시 None)"""
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def get_body(msg) -> str:
    """이메일 본문 추출 (plain text 우선, 없으면 html에서 텍스트 추출)"""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                try:
                    body = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    pass

        # plain text가 없으면 html에서 추출
        if not body:
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if content_type == "text/html" and "attachment" not in content_disposition:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html = part.get_payload(decode=True).decode(charset, errors="replace")
                        body = _strip_html(html)
                        break
                    except Exception:
                        pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                body = _strip_html(body)
        except Exception:
            body = str(msg.get_payload())

    return body.strip()


def get_body_html(msg) -> Optional[str]:
    """이메일의 원본 HTML 본문을 태그 제거 없이 추출 (없으면 None)."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/html" and "attachment" not in content_disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    return None
        return None
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            try:
                return msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                return None
        return None


def _strip_html(html: str) -> str:
    """HTML 태그 제거 (BeautifulSoup 사용)"""
    soup = BeautifulSoup(html, 'html.parser')
    # 스크립트, 스타일 제거
    for tag in soup(['script', 'style', 'head']):
        tag.decompose()
    # 줄바꿈 태그를 개행으로 변환
    for br in soup.find_all('br'):
        br.replace_with('\n')
    for p in soup.find_all('p'):
        p.insert_after('\n')
    text = soup.get_text(separator=' ')
    # 연속된 공백/개행 정리
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def get_attachments(msg) -> list:
    """첨부파일 목록 추출 (파일명, 데이터, 확장자)"""
    attachments = []

    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        content_type = part.get_content_type()

        if "attachment" in content_disposition or part.get_filename():
            filename = part.get_filename()
            if filename:
                filename = decode_header_value(filename)
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                data = part.get_payload(decode=True)
                if data:
                    attachments.append({
                        "filename": filename,
                        "extension": ext,
                        "data": data,
                        "content_type": content_type,
                    })

    return attachments


def parse_eml(file_bytes: bytes) -> dict:
    """EML 파일 전체 파싱"""
    msg = email.message_from_bytes(file_bytes, policy=policy.compat32)

    subject = decode_header_value(msg.get("Subject", ""))
    sender = decode_header_value(msg.get("From", ""))
    to = decode_header_value(msg.get("To", ""))
    date_raw = msg.get("Date", "")
    received_date = parse_date(date_raw)
    body = get_body(msg)
    attachments = get_attachments(msg)

    message_id = (decode_header_value(msg.get("Message-ID", "")) or "").strip()

    return {
        "subject": subject,
        "sender": sender,
        "to": to,
        "received_date": received_date,
        "received_at": parse_date_utc(date_raw),
        "message_id": message_id[:255] or None,
        "body": body,
        "body_html": get_body_html(msg),
        "attachments": attachments,
    }
