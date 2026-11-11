"""분석 프롬프트와 LLM 입력 전처리 도우미 (이미지 정규화, HTML → 평문)."""
import io
import base64


# Claude API가 허용하는 이미지 미디어 타입
_SUPPORTED_IMG = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'GIF': 'image/gif', 'WEBP': 'image/webp'}


_MAX_IMG_SIDE = 1568   # 비전 모델 권장 최대 변 길이 (이보다 크면 축소해서 전송)


def _prep_image(b64_data: str):
    """이미지 base64를 실제 포맷에 맞는 미디어타입으로 정규화.
    긴 변이 _MAX_IMG_SIDE를 넘거나 미지원 포맷(BMP/TIFF 등)이면 축소·변환한다.
    반환: (base64, media_type) 또는 None(실패)."""
    try:
        from PIL import Image
        raw = base64.standard_b64decode(b64_data)
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or '').upper()
        if fmt in _SUPPORTED_IMG and max(img.size) <= _MAX_IMG_SIDE:
            return b64_data, _SUPPORTED_IMG[fmt]
        img.thumbnail((_MAX_IMG_SIDE, _MAX_IMG_SIDE))
        buf = io.BytesIO()
        if img.mode in ('RGBA', 'LA', 'P'):
            img.convert('RGBA').save(buf, format='PNG')
            mt = 'image/png'
        else:
            img.convert('RGB').save(buf, format='JPEG', quality=85)
            mt = 'image/jpeg'
        return base64.standard_b64encode(buf.getvalue()).decode('ascii'), mt
    except Exception:
        return None



SYSTEM_PROMPT = """당신은 이메일과 첨부파일을 분석하여 업체 요구사항을 정리하는 전문가입니다.

주어진 이메일 내용과 첨부파일 내용을 분석하여 다음 항목을 JSON 형식으로 반환하세요:

{
  "요구사항_요약": "핵심 요구사항을 2~5줄로 요약",
  "상세_요구사항": ["요구사항 항목1", "요구사항 항목2", ...],
  "수신일": "YYYY-MM-DD HH:MM 형식 (이메일 수신 날짜)",
  "마감일": "YYYY-MM-DD 형식 또는 '명시되지 않음'",
  "마감일_원문": "이메일에서 마감일 관련 원문 텍스트",
  "요청_업체": "발신 업체명 또는 담당자명",
  "요청_유형": "아래 고정 7개 유형 중 정확히 하나만 그대로 사용 (다른 표현 금지)",
  "우선순위": "높음/보통/낮음 (마감일 긴박성 기준)",
  "추가_참고사항": "기타 중요한 내용"
}

요청_유형은 반드시 다음 7개 값 중 하나를 글자 그대로 사용하세요 (괄호·부연 설명 없이 이 문자열만):
- 검토요청
- 보고서/자료 요청
- 수정/작업요청
- 견적요청
- 작업완료보고
- 회의요청
- 기타
위 6개 유형에 명확히 부합하지 않으면 반드시 "기타"로 분류하세요.

분석 시 주의사항:
- 마감일은 이메일 본문, 첨부파일 모두에서 찾아보세요
- '이번 주', '다음 주' 같은 상대적 표현도 원문에 그대로 기록하세요
- 요구사항이 명확하지 않으면 추측하지 말고 '불명확'으로 표시하세요
- 반드시 유효한 JSON만 반환하세요 (마크다운 코드블록 없이)"""


def _html_to_text(html: str) -> str:
    """HTML을 분석용 평문으로 변환 (태그 제거 → base64 이미지 등도 함께 제거).
    표/문단 구분은 줄바꿈으로 대체."""
    import re
    import html as ihtml
    s = re.sub(r"(?i)<\s*(br|/p|/tr|/div|/h[1-6])\s*/?>", "\n", html)
    s = re.sub(r"(?i)</td>\s*<td[^>]*>", "\t", s)
    s = re.sub(r"<[^>]+>", "", s)        # 잔여 태그(이미지 data URI 포함) 제거
    s = ihtml.unescape(s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
