"""자연어 메일 검색 에이전트.

흐름:
1) 현재 설정된 LLM으로 자연어 질의 → 검색 필터(JSON) 파싱
2) 필터 + 키워드로 분석완료 메일 후보 선별 (Python 레벨 매칭 — list_emails의
   우선순위/유형 필터와 동일한 방식. 이 앱 규모에선 전체 스캔이 충분히 빠름)
3) 후보를 LLM에 다시 보여주고 근거를 든 자연어 답변 생성

LLM 호출이 실패해도(원격 서버 불안정 등) 원문 질의를 키워드로 쓰는 순수 검색으로
폴백해 항상 결과를 반환한다.
"""
import json
import re

from sqlalchemy.orm import defer as sa_defer

import timeutil
from db import Email
import llm_provider

MAX_SCAN = 2000   # 최근 분석 메일 중 이 건수까지만 훑는다

REQUEST_TYPES = ['검토요청', '보고서/자료 요청', '수정/작업요청', '견적요청', '작업완료보고', '회의요청', '기타']

QUERY_PARSE_PROMPT = """당신은 이메일 검색 질의를 분석하는 도우미입니다.
사용자의 자연어 질문을 검색 조건 JSON으로 변환하세요. 오늘 날짜: {today}

{{
  "keywords": ["질의에서 추출한 핵심 검색어(발신자/업체명/주제 등)", ...],
  "date_from": "YYYY-MM-DD 또는 null (질의에 기간 언급 있을 때만)",
  "date_to": "YYYY-MM-DD 또는 null",
  "priority": ["높음"|"보통"|"낮음", ...] 또는 [] (언급 없으면 빈 배열),
  "request_type": [다음 7개 중 언급된 것만: {types}] 또는 [],
  "completed": true 또는 false 또는 null (완료/미완료 언급 있을 때만, 없으면 null)
}}

키워드는 검색에 실제 도움될 단어만 담으세요(조사·불필요한 말 제외). 반드시 JSON만 반환하세요."""

ANSWER_PROMPT = """당신은 사용자가 받은 이메일들을 검색해 답해주는 어시스턴트입니다.
아래는 사용자 질문과, 검색으로 찾은 후보 메일 목록(번호가 붙어 있음)입니다.
후보 중 질문과 실제로 관련 있는 것만 근거로 자연스럽게 한국어로 답하세요.
관련 있는 메일이 없다고 판단되면 솔직히 없다고 답하세요. 목록에 없는 내용을 지어내지 마세요."""


def _parse_query(query, provider, model, anthropic_key, openai_key):
    """자연어 질의 → 필터 dict. 실패 시 원문을 키워드로 쓰는 폴백."""
    today = timeutil.kst_today().isoformat()
    try:
        out, tin, tout, _, _ = llm_provider.chat_once(
            QUERY_PARSE_PROMPT.format(today=today, types=', '.join(REQUEST_TYPES)),
            query, [], provider, model, anthropic_key, openai_key, timeout=60,
        )
        raw = re.sub(r"```(?:json)?\s*", "", out or "")
        raw = re.sub(r"```", "", raw).strip()
        s, e = raw.find('{'), raw.rfind('}')
        if s != -1 and e > s:
            raw = raw[s:e + 1]
        parsed = json.loads(raw)
        filt = {
            'keywords': [str(k) for k in (parsed.get('keywords') or []) if str(k).strip()][:8],
            'date_from': parsed.get('date_from') or None,
            'date_to': parsed.get('date_to') or None,
            'priority': [p for p in (parsed.get('priority') or []) if p in ('높음', '보통', '낮음')],
            'request_type': [t for t in (parsed.get('request_type') or []) if t in REQUEST_TYPES],
            'completed': parsed.get('completed') if isinstance(parsed.get('completed'), bool) else None,
        }
        return filt, (tin, tout)
    except Exception:
        words = [w for w in re.split(r'\s+', query.strip()) if len(w) > 1]
        filt = {'keywords': words[:8], 'date_from': None, 'date_to': None,
                'priority': [], 'request_type': [], 'completed': None}
        return filt, (0, 0)


def _score(row, filt):
    """조건에 맞으면 관련도 점수(정수), 안 맞으면 None."""
    received = timeutil.to_kst_str(row.received_at, '%Y-%m-%d') or ''
    if filt['date_from'] and received < filt['date_from']:
        return None
    if filt['date_to'] and received > filt['date_to']:
        return None
    r = row.analysis_result or {}
    if filt['priority'] and row.priority not in filt['priority']:
        return None
    if filt['request_type'] and row.req_type not in filt['request_type']:
        return None
    if filt['completed'] is not None and bool(row.is_completed) != filt['completed']:
        return None

    haystack = ' '.join(str(v) for v in [
        row.subject, row.sender, row.summary, row.company,
        r.get('추가_참고사항'), ' '.join(str(x) for x in (r.get('상세_요구사항') or [])),
    ] if v).lower()

    score = sum(1 for kw in filt['keywords'] if kw.lower() in haystack)
    if not filt['keywords']:
        return 1  # 키워드 없이 필터만으로도 매칭 허용 (예: "이번 주 견적요청 뭐있어")
    return score if score > 0 else None


def _write_answer(query, candidates, provider, model, anthropic_key, openai_key):
    lines = [f"질문: {query}", "", "후보 메일 목록:"]
    for i, c in enumerate(candidates, 1):
        r = c['result']
        lines.append(
            f"{i}. [{c['received_date']}] {c['sender']} - {c['subject']}\n"
            f"   요약: {r.get('요구사항_요약', '')}\n"
            f"   우선순위: {r.get('우선순위', '')} / 유형: {r.get('요청_유형', '')} / "
            f"마감일: {c['deadline'] or r.get('마감일', '')} / 처리: {'완료' if c['completed'] else '미완료'}"
        )
    try:
        out, tin, tout, _, _ = llm_provider.chat_once(
            ANSWER_PROMPT, "\n".join(lines), [], provider, model, anthropic_key, openai_key, timeout=60,
        )
        return (out or '').strip(), (tin, tout)
    except Exception as e:
        return f"(AI 답변 생성 실패: {e}) 아래 관련 가능성이 있는 메일 목록을 확인해주세요.", (0, 0)


def search(query, db, account_ids, provider=None, model=None,
           anthropic_key=None, openai_key=None, limit=8):
    """자연어 질의로 분석완료 메일을 검색. (answer, matches, usage) 반환."""
    if not account_ids:
        return '연결된 메일 계정이 없습니다.', [], {'tokens_in': 0, 'tokens_out': 0}

    filt, parse_usage = _parse_query(query, provider, model, anthropic_key, openai_key)

    rows = db.query(Email).options(
        sa_defer(Email.body_text), sa_defer(Email.attachments_json)
    ).filter(
        Email.account_id.in_(account_ids), Email.is_analyzed == True,  # noqa: E712
        Email.is_excluded == False,  # noqa: E712
    ).order_by(Email.received_at.desc()).limit(MAX_SCAN).all()

    scored = []
    for row in rows:
        s = _score(row, filt)
        if s is not None:
            scored.append((s, row))
    scored.sort(key=lambda x: (x[0], x[1].received_at or x[1].created_at), reverse=True)
    top = scored[:limit]

    if not top:
        return '조건에 맞는 분석완료 메일을 찾지 못했습니다.', [], {'tokens_in': parse_usage[0], 'tokens_out': parse_usage[1]}

    candidates = [{
        'id': row.id, 'subject': row.subject or '(제목 없음)', 'sender': row.sender or '',
        'received_date': timeutil.to_kst_str(row.received_at) or row.received_date or '',
        'result': row.analysis_result or {},
        'deadline': row.deadline.isoformat() if row.deadline else None,
        'completed': bool(row.is_completed),
    } for _, row in top]

    answer, answer_usage = _write_answer(query, candidates, provider, model, anthropic_key, openai_key)

    matches = [{'id': c['id']} for c in candidates]

    usage = {'tokens_in': parse_usage[0] + answer_usage[0], 'tokens_out': parse_usage[1] + answer_usage[1]}
    return answer, matches, usage
