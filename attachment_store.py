"""첨부파일 원본 바이트를 서버 디스크에 저장/조회한다.

- 원본은 DB가 아닌 파일시스템(STORE_DIR)에 저장하여 DB를 가볍게 유지한다.
- 저장 경로는 ATTACHMENT_STORE_DIR 환경변수로 변경 가능(기본: 프로젝트/attachment_store).
"""
import os
import uuid
import logging

logger = logging.getLogger(__name__)

STORE_DIR = (
    os.environ.get("ATTACHMENT_STORE_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "attachment_store")
)

# 디스크에 보관할 원본 1개당 최대 크기 (초과 시 저장하지 않음 → 본문에 안내)
MAX_STORE_BYTES = 100 * 1024 * 1024  # 100MB

# 확장자 → Content-Type (브라우저 표시/다운로드용)
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "hwp": "application/x-hwp",
    "hwpx": "application/hwpx",
}


def content_type_for(ext: str) -> str:
    return CONTENT_TYPES.get((ext or "").lower(), "application/octet-stream")


def save_original(data: bytes, ext: str):
    """원본 바이트를 디스크에 저장하고 {'stored': 파일명, 'size': 바이트수} 반환.
    저장하지 않으면(빈 데이터/초과/오류) None."""
    if not data or len(data) > MAX_STORE_BYTES:
        return None
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        safe_ext = "".join(c for c in (ext or "") if c.isalnum())[:8]
        name = uuid.uuid4().hex + (("." + safe_ext) if safe_ext else "")
        with open(os.path.join(STORE_DIR, name), "wb") as f:
            f.write(data)
        return {"stored": name, "size": len(data)}
    except Exception as e:
        logger.error(f"[attachment_store] 저장 실패: {e}")
        return None


def stored_path(name: str):
    """저장 파일명에 해당하는 실제 경로 반환(존재할 때만). 경로 탈출 방지."""
    if not name:
        return None
    name = os.path.basename(str(name))
    path = os.path.join(STORE_DIR, name)
    return path if os.path.isfile(path) else None


def delete_original(name: str) -> bool:
    """저장 파일 삭제 (없으면 False)."""
    path = stored_path(name)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except Exception as e:
        logger.error(f"[attachment_store] 삭제 실패: {e}")
        return False


def read_original(name: str):
    """저장된 원본 바이트를 읽어 반환(없으면 None)."""
    path = stored_path(name)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"[attachment_store] 읽기 실패: {e}")
        return None
