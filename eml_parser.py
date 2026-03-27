import email
import email.header
import email.utils
from email import policy
from datetime import datetime
from typing import Optional


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
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str


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


def _strip_html(html: str) -> str:
    """HTML 태그 제거 (간단한 방식)"""
    import re
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"&lt;", "<", html)
    html = re.sub(r"&gt;", ">", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


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

    return {
        "subject": subject,
        "sender": sender,
        "to": to,
        "received_date": received_date,
        "body": body,
        "attachments": attachments,
    }
