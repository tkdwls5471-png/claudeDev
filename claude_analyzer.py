import os
import anthropic
from typing import Optional


def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    return anthropic.Anthropic(api_key=api_key)


SYSTEM_PROMPT = """당신은 이메일과 첨부파일을 분석하여 업체 요구사항을 정리하는 전문가입니다.

주어진 이메일 내용과 첨부파일 내용을 분석하여 다음 항목을 JSON 형식으로 반환하세요:

{
  "요구사항_요약": "핵심 요구사항을 2~5줄로 요약",
  "상세_요구사항": ["요구사항 항목1", "요구사항 항목2", ...],
  "수신일": "YYYY-MM-DD HH:MM 형식 (이메일 수신 날짜)",
  "마감일": "YYYY-MM-DD 형식 또는 '명시되지 않음'",
  "마감일_원문": "이메일에서 마감일 관련 원문 텍스트",
  "요청_업체": "발신 업체명 또는 담당자명",
  "요청_유형": "견적요청/자료제출/검토요청/회의요청/기타 중 하나",
  "우선순위": "높음/보통/낮음 (마감일 긴박성 기준)",
  "추가_참고사항": "기타 중요한 내용"
}

분석 시 주의사항:
- 마감일은 이메일 본문, 첨부파일 모두에서 찾아보세요
- '이번 주', '다음 주' 같은 상대적 표현도 원문에 그대로 기록하세요
- 요구사항이 명확하지 않으면 추측하지 말고 '불명확'으로 표시하세요
- 반드시 유효한 JSON만 반환하세요 (마크다운 코드블록 없이)"""


def build_messages(eml_data: dict, processed_attachments: list) -> list:
    """Claude API에 전송할 메시지 구성"""
    content = []

    # 이메일 기본 정보 텍스트
    email_info = f"""=== 이메일 정보 ===
제목: {eml_data['subject']}
발신자: {eml_data['sender']}
수신자: {eml_data['to']}
수신일시: {eml_data['received_date']}

=== 이메일 본문 ===
{eml_data['body'][:8000]}"""  # 본문 너무 길면 앞 8000자만

    content.append({"type": "text", "text": email_info})

    # 첨부파일 내용 추가
    for att in processed_attachments:
        if att["type"] == "text":
            att_text = f"\n=== 첨부파일: {att['filename']} ===\n{att['content'][:5000]}"
            content.append({"type": "text", "text": att_text})

        elif att["type"] == "image":
            content.append({"type": "text", "text": f"\n=== 첨부파일 (이미지): {att['filename']} ==="})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att["media_type"],
                    "data": att["content"],
                }
            })

        elif att["type"] == "images":
            content.append({"type": "text", "text": f"\n=== 첨부파일 (스캔PDF): {att['filename']} ==="})
            for i, img_b64 in enumerate(att["content"][:5]):  # 최대 5페이지
                content.append({"type": "text", "text": f"[페이지 {i+1}]"})
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    }
                })

        elif att["type"] == "unsupported":
            content.append({"type": "text", "text": f"\n[{att['filename']}]: 지원하지 않는 파일 형식"})

    content.append({"type": "text", "text": "\n위 이메일과 첨부파일을 분석하여 요구사항을 JSON 형식으로 정리해주세요."})

    return [{"role": "user", "content": content}]


def analyze_email(eml_data: dict, processed_attachments: list) -> dict:
    """Claude API로 이메일 분석 후 구조화된 결과 반환"""
    client = get_client()
    messages = build_messages(eml_data, processed_attachments)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    raw_text = response.content[0].text.strip()

    # JSON 파싱
    import json
    import re

    # 마크다운 코드블록 제거 (혹시 있을 경우)
    raw_text = re.sub(r"```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"```", "", raw_text)
    raw_text = raw_text.strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        # JSON 파싱 실패 시 원문 반환
        result = {
            "요구사항_요약": "파싱 오류 - 원문 참조",
            "상세_요구사항": [],
            "수신일": eml_data.get("received_date", ""),
            "마감일": "불명확",
            "마감일_원문": "",
            "요청_업체": eml_data.get("sender", ""),
            "요청_유형": "기타",
            "우선순위": "보통",
            "추가_참고사항": raw_text,
        }

    # 수신일은 EML 헤더 값으로 보정
    if eml_data.get("received_date"):
        result["수신일"] = eml_data["received_date"]

    return result
