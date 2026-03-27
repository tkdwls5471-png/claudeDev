import io
import base64
from typing import Optional


SUPPORTED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp",
                        "xlsx", "xls", "pptx", "ppt"}


def extract_text_from_pdf(data: bytes) -> str:
    """PDF에서 텍스트 추출 (텍스트 기반 PDF)"""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts).strip()
    except Exception as e:
        return f"[PDF 텍스트 추출 실패: {e}]"


def is_scanned_pdf(data: bytes) -> bool:
    """스캔본 PDF 여부 판별 (텍스트가 거의 없으면 스캔본으로 간주)"""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            total_text = ""
            for page in pdf.pages[:3]:  # 처음 3페이지만 확인
                text = page.extract_text() or ""
                total_text += text
        return len(total_text.strip()) < 50
    except Exception:
        return False


def pdf_to_images_base64(data: bytes) -> list:
    """스캔 PDF를 이미지로 변환하여 base64 반환"""
    try:
        import fitz  # pymupdf
        images = []
        doc = fitz.open(stream=data, filetype="pdf")
        for page in doc:
            mat = fitz.Matrix(2.0, 2.0)  # 2x 해상도
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            images.append(b64)
        doc.close()
        return images
    except Exception as e:
        return []


def extract_text_from_excel(data: bytes) -> str:
    """Excel 파일에서 텍스트 추출"""
    try:
        import pandas as pd
        excel_file = io.BytesIO(data)
        xls = pd.ExcelFile(excel_file)
        parts = []
        for sheet_name in xls.sheet_names:
            df = xls.parse(sheet_name)
            df = df.dropna(how="all").fillna("")
            sheet_text = f"[시트: {sheet_name}]\n"
            sheet_text += df.to_string(index=False)
            parts.append(sheet_text)
        return "\n\n".join(parts).strip()
    except Exception as e:
        return f"[Excel 추출 실패: {e}]"


def extract_text_from_ppt(data: bytes) -> str:
    """PPT 파일에서 텍스트 추출"""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            slide_texts.append(text)
                # 표 내용도 추출
                if shape.has_table:
                    for row in shape.table.rows:
                        row_text = " | ".join(
                            cell.text.strip() for cell in row.cells if cell.text.strip()
                        )
                        if row_text:
                            slide_texts.append(row_text)
            if slide_texts:
                parts.append(f"[슬라이드 {i}]\n" + "\n".join(slide_texts))
        return "\n\n".join(parts).strip()
    except Exception as e:
        return f"[PPT 추출 실패: {e}]"


def image_to_base64(data: bytes, content_type: str = "image/png") -> tuple:
    """이미지를 base64로 인코딩"""
    b64 = base64.standard_b64encode(data).decode("utf-8")
    return b64, content_type


def get_image_media_type(extension: str) -> str:
    """확장자로 미디어 타입 반환"""
    mapping = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "webp": "image/webp",
    }
    return mapping.get(extension.lower(), "image/png")


def process_attachment(attachment: dict) -> dict:
    """
    첨부파일을 처리하여 텍스트 또는 이미지 데이터 반환
    반환 형식:
    {
        "type": "text" | "image" | "images" | "unsupported",
        "filename": str,
        "content": str | list,         # text이면 str, image/images이면 base64 list
        "media_type": str | None,       # image 타입일 때 미디어 타입
    }
    """
    filename = attachment["filename"]
    ext = attachment["extension"]
    data = attachment["data"]

    if ext == "pdf":
        if is_scanned_pdf(data):
            images = pdf_to_images_base64(data)
            if images:
                return {"type": "images", "filename": filename, "content": images, "media_type": "image/png"}
            else:
                return {"type": "text", "filename": filename, "content": "[스캔 PDF 이미지 변환 실패]", "media_type": None}
        else:
            text = extract_text_from_pdf(data)
            return {"type": "text", "filename": filename, "content": text, "media_type": None}

    elif ext in {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"}:
        b64, _ = image_to_base64(data)
        media_type = get_image_media_type(ext)
        return {"type": "image", "filename": filename, "content": b64, "media_type": media_type}

    elif ext in {"xlsx", "xls"}:
        text = extract_text_from_excel(data)
        return {"type": "text", "filename": filename, "content": text, "media_type": None}

    elif ext in {"pptx", "ppt"}:
        text = extract_text_from_ppt(data)
        return {"type": "text", "filename": filename, "content": text, "media_type": None}

    else:
        return {"type": "unsupported", "filename": filename, "content": f"[지원하지 않는 파일 형식: {ext}]", "media_type": None}
