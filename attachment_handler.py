import io
import base64

# 그리드/표 렌더링 시 페이로드 폭주 방지를 위한 상한
_TABLE_MAX_ROWS = 500
_TABLE_MAX_COLS = 50


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


def pdf_to_images_base64(data: bytes, max_pages: int = 3) -> list:
    """스캔 PDF 앞쪽 max_pages 페이지를 이미지로 변환하여 base64 반환"""
    try:
        import fitz  # pymupdf
        images = []
        doc = fitz.open(stream=data, filetype="pdf")
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            mat = fitz.Matrix(2.0, 2.0)  # 2x 해상도
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            images.append(b64)
        doc.close()
        return images
    except Exception as e:
        return []


def is_encrypted_office(data: bytes) -> bool:
    """암호화된 OOXML(xlsx/docx/pptx) 여부 — OLE 컨테이너의 EncryptedPackage 스트림 확인."""
    if data[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return False  # OLE 매직이 아니면 일반 zip 기반 OOXML
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        names = ole.listdir()
        ole.close()
        return any(n and n[0] == 'EncryptedPackage' for n in names)
    except Exception:
        return False


def extract_grid_from_excel(data: bytes) -> list:
    """Excel을 시트별 2차원 셀 그리드로 변환 (원본 셀 배치 유지)"""
    import pandas as pd
    xls = pd.ExcelFile(io.BytesIO(data))
    sheets = []
    for sheet_name in xls.sheet_names:
        # header=None: 머리글 추론 없이 원본 셀 그대로
        df = xls.parse(sheet_name, header=None, dtype=str)
        df = df.dropna(how="all").fillna("")
        truncated = df.shape[0] > _TABLE_MAX_ROWS or df.shape[1] > _TABLE_MAX_COLS
        df = df.iloc[:_TABLE_MAX_ROWS, :_TABLE_MAX_COLS]
        rows = df.astype(str).values.tolist()
        sheets.append({"name": sheet_name, "rows": rows, "truncated": truncated})
    return sheets


def extract_html_from_docx(data: bytes) -> str:
    """Word(.docx)를 HTML로 변환 (서식 유지, 원본에 가깝게)"""
    try:
        import mammoth
        result = mammoth.convert_to_html(io.BytesIO(data))
        return result.value or ""
    except Exception as e:
        return f"<p>[Word 변환 실패: {e}]</p>"


def extract_text_from_doc(data: bytes) -> str:
    """구형 Word(.doc) — 바이너리에서 텍스트만 best-effort 추출"""
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        if ole.exists("WordDocument"):
            raw = ole.openstream("WordDocument").read()
            ole.close()
            # 인쇄가능 문자 위주로 정리
            text = raw.decode("utf-16-le", errors="ignore")
            import re
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
            return text.strip() or "[.doc 텍스트 없음]"
        ole.close()
        return "[.doc 형식을 읽을 수 없습니다]"
    except Exception as e:
        return f"[.doc 추출 실패: {e}]"


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


# HWP PARA_TEXT 제어문자 분류 (한글 문서 포맷 5.0 스펙)
_HWP_CHAR_CONTROLS     = {0, 10, 13}
_HWP_INLINE_CONTROLS   = {4, 5, 6, 7, 8, 9, 19, 20}
_HWP_EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}


def _decode_hwp_para_text(rec: bytes) -> str:
    """HWPTAG_PARA_TEXT 레코드(UTF-16LE + 제어문자)를 일반 텍스트로 변환"""
    import struct
    out = []
    i, n = 0, len(rec)
    while i + 1 < n:
        wch = struct.unpack_from("<H", rec, i)[0]
        if wch in _HWP_INLINE_CONTROLS or wch in _HWP_EXTENDED_CONTROLS:
            i += 16  # 제어문자(1) + 데이터(6) + 닫는 제어문자(1) = 8 WCHAR
        elif wch in _HWP_CHAR_CONTROLS:
            if wch in (10, 13):
                out.append("\n")
            i += 2
        else:
            out.append(chr(wch))
            i += 2
    return "".join(out)


def _parse_hwp_section(buf: bytes) -> str:
    """HWP BodyText 섹션 스트림에서 문단 텍스트 레코드만 추출"""
    import struct
    parts = []
    i, size = 0, len(buf)
    while i + 4 <= size:
        rec_header = struct.unpack_from("<I", buf, i)[0]
        tag_id   = rec_header & 0x3FF
        rec_size = (rec_header >> 20) & 0xFFF
        i += 4
        if rec_size == 0xFFF:  # 확장 크기: 다음 4바이트가 실제 길이
            if i + 4 > size:
                break
            rec_size = struct.unpack_from("<I", buf, i)[0]
            i += 4
        if tag_id == 67:  # HWPTAG_PARA_TEXT
            parts.append(_decode_hwp_para_text(buf[i:i + rec_size]))
        i += rec_size
    return "\n".join(parts)


def extract_text_from_hwp(data: bytes) -> str:
    """HWP 5.0 (OLE 복합문서) 파일에서 텍스트 추출"""
    try:
        import olefile
        import zlib
        ole = olefile.OleFileIO(io.BytesIO(data))
        dirs = ole.listdir()
        if ["FileHeader"] not in dirs:
            ole.close()
            return "[HWP 5.0 형식이 아닙니다]"
        header = ole.openstream("FileHeader").read()
        is_compressed = bool(header[36] & 1)
        section_nums = sorted(
            int(d[1].replace("Section", ""))
            for d in dirs
            if len(d) > 1 and d[0] == "BodyText" and d[1].startswith("Section")
        )
        text_parts = []
        for num in section_nums:
            stream = ole.openstream(f"BodyText/Section{num}").read()
            unpacked = zlib.decompress(stream, -15) if is_compressed else stream
            text_parts.append(_parse_hwp_section(unpacked))
        ole.close()
        result = "\n".join(p for p in text_parts if p).strip()
        return result or "[HWP 텍스트 없음]"
    except Exception as e:
        return f"[HWP 추출 실패: {e}]"


def extract_text_from_hwpx(data: bytes) -> str:
    """HWPX (ZIP+XML) 파일에서 텍스트 추출"""
    try:
        import zipfile
        import re
        parts = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(
                n for n in z.namelist()
                if n.startswith("Contents/section") and n.endswith(".xml")
            )
            for name in names:
                xml = z.read(name).decode("utf-8", errors="ignore")
                # <hp:t ...>텍스트</hp:t> 내부 텍스트만 수집
                for m in re.findall(r"<hp:t[^>]*>(.*?)</hp:t>", xml, re.DOTALL):
                    txt = re.sub(r"<[^>]+>", "", m)  # 잔여 태그 제거
                    txt = (txt.replace("&lt;", "<").replace("&gt;", ">")
                              .replace("&amp;", "&").replace("&quot;", '"'))
                    if txt.strip():
                        parts.append(txt)
        return "\n".join(parts).strip() or "[HWPX 텍스트 없음]"
    except Exception as e:
        return f"[HWPX 추출 실패: {e}]"


def _hwpx_local(tag: str) -> str:
    """네임스페이스 접두사를 제거한 로컬 태그명."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


_HWPX_ALIGN = {
    "center": "center", "right": "right",
    "justify": "justify", "both": "justify", "distribute": "justify",
}
_HWPX_IMG_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff",
}
_HWPX_IMG_MAX = 1_500_000      # 인라인 이미지 1개 최대 바이트
_HWPX_IMG_TOTAL_MAX = 5_000_000  # 인라인 이미지 총합 최대 바이트


def extract_html_from_hwpx(data: bytes) -> str:
    """HWPX(ZIP+OWPML XML)를 서식 보존 HTML로 변환.

    문단(정렬 포함)·표·이미지·줄바꿈을 살린다. 그라데이션/폰트/정확한 페이지
    레이아웃 등 장식 요소까지 100% 동일하진 않지만 내용 구조는 보존된다.
    """
    try:
        import io as _io
        import zipfile
        import html as _html
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(_io.BytesIO(data)) as z:
            names = z.namelist()
            L = _hwpx_local

            def _children(node, name):
                return [c for c in list(node) if L(c.tag) == name]

            # 1) header.xml → 문단 정렬(paraPr) / 굵게(charPr) 맵
            align_map, bold_map = {}, {}
            header_name = next(
                (n for n in names if n.endswith("header.xml") and "ontents" in n), None)
            if header_name:
                try:
                    hroot = ET.fromstring(z.read(header_name))
                    for el in hroot.iter():
                        ln = L(el.tag)
                        if ln == "paraPr":
                            pid = el.get("id")
                            al = None
                            for ch in el.iter():
                                if L(ch.tag) == "align":
                                    al = (ch.get("horizontal") or ch.get("align") or "").lower()
                                    break
                            if pid is not None and al:
                                align_map[pid] = al
                        elif ln == "charPr":
                            cid = el.get("id")
                            if cid is not None and any(L(ch.tag) == "bold" for ch in list(el)):
                                bold_map[cid] = True
                except Exception:
                    pass

            # 2) 이미지 매니페스트(.hpf) → id → zip 내부 경로
            img_manifest = {}
            hpf = next((n for n in names if n.endswith(".hpf")), None)
            if hpf:
                try:
                    proot = ET.fromstring(z.read(hpf))
                    for el in proot.iter():
                        if L(el.tag) == "item":
                            iid, href = el.get("id"), el.get("href")
                            if iid and href:
                                img_manifest[iid] = href
                except Exception:
                    pass

            def _resolve_bin(idref):
                if not idref:
                    return None
                href = img_manifest.get(idref)
                cands = []
                if href:
                    base = href.rsplit("/", 1)[-1]
                    cands += [href, "Contents/" + href, href.lstrip("/")]
                    cands += [n for n in names if n.endswith("/" + base) or n == base]
                # 매니페스트가 없으면 BinData 폴더에서 idref 포함 파일 탐색
                cands += [n for n in names if "indata" in n.lower() and idref in n]
                for c in cands:
                    if c in names:
                        return c
                return None

            img_budget = [0]

            def render_pic(pic):
                ref = None
                for el in pic.iter():
                    if L(el.tag) == "img":
                        ref = el.get("binaryItemIDRef") or el.get("binaryitemIDRef")
                        if ref:
                            break
                path = _resolve_bin(ref)
                if not path:
                    return ""
                try:
                    raw = z.read(path)
                    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                    mime = _HWPX_IMG_MIME.get(ext)
                    if not mime or len(raw) > _HWPX_IMG_MAX:
                        return ""
                    if img_budget[0] + len(raw) > _HWPX_IMG_TOTAL_MAX:
                        return ""
                    img_budget[0] += len(raw)
                    b64 = base64.standard_b64encode(raw).decode("utf-8")
                    return f'<img src="data:{mime};base64,{b64}" style="max-width:100%;height:auto;"/>'
                except Exception:
                    return ""

            def render_text_el(t):
                buf = []
                if t.text:
                    buf.append(_html.escape(t.text))
                for ch in list(t):
                    ln = L(ch.tag)
                    if ln in ("lineBreak", "linebreak"):
                        buf.append("<br>")
                    elif ln == "tab":
                        buf.append("&emsp;")
                    if ch.tail:
                        buf.append(_html.escape(ch.tail))
                return "".join(buf)

            def render_run(run):
                bold = bold_map.get(run.get("charPrIDRef"))
                parts = []
                for ch in list(run):
                    ln = L(ch.tag)
                    if ln == "t":
                        s = render_text_el(ch)
                        if s and bold:
                            s = f"<strong>{s}</strong>"  # 굵게는 텍스트에만 적용
                        parts.append(s)
                    elif ln == "tbl":
                        parts.append(render_table(ch))
                    elif ln == "pic":
                        parts.append(render_pic(ch))
                    elif ln in ("lineBreak", "linebreak"):
                        parts.append("<br>")
                    elif ln == "tab":
                        parts.append("&emsp;")
                    else:
                        sub = render_children(ch)
                        if sub:
                            parts.append(sub)
                return "".join(parts)

            def render_para(p):
                inner = render_children(p)
                if not inner.strip():
                    return "<p>&nbsp;</p>"
                # 표 등 블록 요소는 <p>로 감싸지 않음 (잘못된 중첩 방지)
                if "<table" in inner:
                    return inner
                al = _HWPX_ALIGN.get(align_map.get(p.get("paraPrIDRef"), ""), "")
                style = f' style="text-align:{al}"' if al else ""
                return f"<p{style}>{inner}</p>"

            def render_table(tbl):
                out = ['<table class="hwpx-tbl">']
                for tr in _children(tbl, "tr"):
                    out.append("<tr>")
                    for tc in _children(tr, "tc"):
                        subs = _children(tc, "subList")
                        if subs:
                            cell = "".join(
                                render_para(p) for s in subs for p in _children(s, "p"))
                        else:
                            cell = render_children(tc)
                        out.append(f"<td>{cell or '&nbsp;'}</td>")
                    out.append("</tr>")
                out.append("</table>")
                return "".join(out)

            def render_children(node):
                out = []
                for ch in list(node):
                    ln = L(ch.tag)
                    if ln == "run":
                        out.append(render_run(ch))
                    elif ln == "p":
                        out.append(render_para(ch))
                    elif ln == "tbl":
                        out.append(render_table(ch))
                    elif ln == "t":
                        out.append(render_text_el(ch))
                    elif ln == "pic":
                        out.append(render_pic(ch))
                    else:
                        sub = render_children(ch)
                        if sub:
                            out.append(sub)
                return "".join(out)

            # 3) 섹션 순서대로 렌더
            sect_names = sorted(
                n for n in names
                if "section" in n.lower() and n.endswith(".xml") and "ontents" in n)
            html_parts = []
            for sn in sect_names:
                try:
                    root = ET.fromstring(z.read(sn))
                    html_parts.append(render_children(root))
                except Exception:
                    continue

        body = "".join(html_parts).strip()
        return body
    except Exception as e:
        return f"<p>[HWPX 변환 실패: {e}]</p>"


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
        if is_encrypted_office(data):
            return {"type": "text", "filename": filename,
                    "content": "[암호화된 Excel 파일이라 내용을 읽을 수 없습니다. 비밀번호를 해제한 뒤 다시 첨부해 주세요.]",
                    "media_type": None}
        try:
            sheets = extract_grid_from_excel(data)
            return {"type": "table", "filename": filename, "content": sheets, "media_type": None}
        except Exception as e:
            return {"type": "text", "filename": filename, "content": f"[Excel 추출 실패: {e}]", "media_type": None}

    elif ext in {"pptx", "ppt"}:
        text = extract_text_from_ppt(data)
        return {"type": "text", "filename": filename, "content": text, "media_type": None}

    elif ext == "docx":
        if is_encrypted_office(data):
            return {"type": "text", "filename": filename,
                    "content": "[암호화된 Word 파일이라 내용을 읽을 수 없습니다. 비밀번호를 해제한 뒤 다시 첨부해 주세요.]",
                    "media_type": None}
        html = extract_html_from_docx(data)
        return {"type": "html", "filename": filename, "content": html, "media_type": None}

    elif ext == "doc":
        text = extract_text_from_doc(data)
        return {"type": "text", "filename": filename, "content": text, "media_type": None}

    elif ext == "hwp":
        text = extract_text_from_hwp(data)
        return {"type": "text", "filename": filename, "content": text, "media_type": None}

    elif ext == "hwpx":
        html = extract_html_from_hwpx(data)
        # 변환 결과가 비거나 실패하면 텍스트로 폴백
        if not html or len(html.strip()) < 20 or html.startswith("<p>[HWPX 변환 실패"):
            text = extract_text_from_hwpx(data)
            return {"type": "text", "filename": filename, "content": text, "media_type": None}
        return {"type": "html", "filename": filename, "content": html, "media_type": None}

    else:
        return {"type": "unsupported", "filename": filename, "content": f"[지원하지 않는 파일 형식: {ext}]", "media_type": None}
