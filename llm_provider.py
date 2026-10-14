"""멀티 프로바이더 LLM 분석 레이어.

지원 프로바이더:
- remote (기본): OpenAI 호환 로컬 서버(gemma). REMOTE_LLM_URL / REMOTE_LLM_MODEL
- anthropic: Claude (claude-haiku-4-5 / claude-sonnet-4-6 / claude-opus-4-8)
- openai:   OpenAI GPT (gpt-4o / gpt-4o-mini)

analyze() → (result_dict, usage) 반환. 실패하면 LLMError(또는 SDK 예외)를 던지고,
분석 워커는 classify()로 오류 종류를 판단해 재시도 여부를 정한다.
usage = {'provider','model','tokens_in','tokens_out'}
"""
import os
import re
import json

from claude_analyzer import SYSTEM_PROMPT, _prep_image, _html_to_text

MAX_TOKENS = int(os.environ.get('LLM_MAX_TOKENS', 4096))
LLM_TIMEOUT_SEC = float(os.environ.get('LLM_TIMEOUT_SEC', 180))
DEFAULT_PROVIDER = 'remote'

ANTHROPIC_MODELS = ['claude-haiku-4-5', 'claude-sonnet-4-6', 'claude-opus-4-8']
OPENAI_MODELS = ['gpt-4o', 'gpt-4o-mini']


# ─── 오류 분류 ────────────────────────────────────────────────────────────────

class LLMError(Exception):
    """kind: transient(잠시 후 재시도) | config(설정 문제) | invalid(이 메일 요청 자체가 거부) | parse(응답 해석 실패)"""
    def __init__(self, message, kind='transient'):
        super().__init__(message)
        self.kind = kind


class LLMParseError(LLMError):
    def __init__(self, message, raw='', truncated=False):
        super().__init__(message, kind='parse')
        self.raw = (raw or '')[:2000]
        self.truncated = truncated


def classify(e) -> str:
    if isinstance(e, LLMError):
        return e.kind
    if isinstance(e, (TimeoutError, ConnectionError)):
        return 'transient'
    try:
        import anthropic
        import openai
        if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError)):
            return 'transient'   # APITimeoutError 포함
    except ImportError:
        pass
    status = getattr(e, 'status_code', None)
    msg = str(e).lower()
    if status is None:
        return 'transient'
    if status == 429 and (getattr(e, 'code', None) == 'insufficient_quota' or 'insufficient_quota' in msg):
        return 'config'
    if status in (408, 409, 429) or status >= 500:      # Anthropic 529(overloaded) 포함
        return 'transient'
    if status in (401, 403, 404) or 'credit balance' in msg or \
            ('model' in msg and ('not found' in msg or 'does not exist' in msg)):
        return 'config'
    return 'invalid'                                     # 400 / 413 / 422


# ─── 모델 목록 ────────────────────────────────────────────────────────────────

def remote_model():
    return os.environ.get('REMOTE_LLM_MODEL', 'gemma-4-26b-a4b-it')


def remote_available_models():
    """원격 서버에 지금 실제로 로드된 모델 목록 조회 (조회 실패 시 빈 리스트).
    공유 추론 서버라 로드된 모델이 수시로 바뀔 수 있어 설정 화면을 열 때마다 조회한다."""
    import urllib.request
    base = _remote_base()
    if not base:
        return []
    try:
        req = urllib.request.Request(f'{base}/models')
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return [m.get('id') for m in (data.get('data') or []) if m.get('id')]
    except Exception:
        return []


def model_options():
    """설정 UI용 프로바이더별 선택 가능 모델 목록."""
    return {
        'remote':    remote_available_models() or [remote_model()],
        'anthropic': list(ANTHROPIC_MODELS),
        'openai':    list(OPENAI_MODELS),
    }


def _remote_base():
    base = os.environ.get('REMOTE_LLM_URL', '').rstrip('/')
    if base and not base.endswith('/v1'):
        base = base + '/v1'
    return base


# ─── 메시지 구성 (텍스트 파트 + 이미지 목록) ──────────────────────────────────

def _build_content(eml_data, attachments):
    parts = [
        "=== 이메일 정보 ===\n"
        f"제목: {eml_data.get('subject','')}\n"
        f"발신자: {eml_data.get('sender','')}\n"
        f"수신자: {eml_data.get('to','')}\n"
        f"수신일시: {eml_data.get('received_date','')}\n\n"
        "=== 이메일 본문 ===\n"
        f"{(eml_data.get('body') or '')[:8000]}"
    ]
    images = []   # [(b64, media_type), ...]
    for att in attachments:
        t = att.get('type')
        fn = att.get('filename', '')
        if t == 'text':
            parts.append(f"\n=== 첨부파일: {fn} ===\n{(att.get('content') or '')[:5000]}")
        elif t == 'image':
            parts.append(f"\n=== 첨부파일 (이미지): {fn} ===")
            p = _prep_image(att.get('content') or '')
            if p:
                images.append(p)
        elif t == 'images':
            parts.append(f"\n=== 첨부파일 (스캔PDF): {fn} ===")
            for b64 in (att.get('content') or [])[:3]:
                p = _prep_image(b64)
                if p:
                    images.append(p)
        elif t == 'html':
            txt = _html_to_text(att.get('content') or '')
            if txt.strip():
                parts.append(f"\n=== 첨부파일: {fn} ===\n{txt[:5000]}")
        elif t == 'table':
            lines = []
            for sh in (att.get('content') or []):
                lines.append(f"[시트: {sh.get('name','')}]")
                for row in (sh.get('rows') or []):
                    lines.append("\t".join(str(c) for c in row))
            if lines:
                parts.append(f"\n=== 첨부파일: {fn} ===\n" + "\n".join(lines)[:5000])
    parts.append("\n위 이메일과 첨부파일을 분석하여 요구사항을 JSON 형식으로 정리해주세요.")
    return "\n".join(parts), images


# ─── 프로바이더별 호출 ────────────────────────────────────────────────────────

def _call_openai_compatible(base_url, api_key, model, system_prompt, text, images, timeout):
    """OpenAI 호환 엔드포인트(gemma/OpenAI 공통). 반환: (text, tin, tout, truncated)"""
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key or 'EMPTY', timeout=timeout, max_retries=1)
    content = [{"type": "text", "text": text}]
    for b64, mt in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
    resp = client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    )
    choice = resp.choices[0]
    out = choice.message.content or ''
    u = resp.usage
    return (out, (getattr(u, 'prompt_tokens', 0) or 0), (getattr(u, 'completion_tokens', 0) or 0),
            choice.finish_reason == 'length')


def _call_anthropic(api_key, model, system_prompt, text, images, timeout):
    import anthropic
    if not api_key:
        raise LLMError("Anthropic API 키가 설정되지 않았습니다.", kind='config')
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
    content = [{"type": "text", "text": text}]
    for b64, mt in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    out = ''.join(getattr(b, 'text', '') for b in resp.content)
    return out, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason == 'max_tokens'


# ─── JSON 파싱 ────────────────────────────────────────────────────────────────

def _parse_result(raw_text, eml_data, truncated=False):
    raw = re.sub(r"```(?:json)?\s*", "", raw_text or "")
    raw = re.sub(r"```", "", raw).strip()

    result = None
    try:
        result = json.loads(raw)
    except Exception:
        # 대화체 텍스트에 섞인 JSON 오브젝트만 추출 (첫 '{' ~ 마지막 '}')
        s, e = raw.find('{'), raw.rfind('}')
        if s != -1 and e > s:
            try:
                result = json.loads(raw[s:e + 1])
            except Exception:
                result = None

    if not isinstance(result, dict) or not (result.get('요구사항_요약') or result.get('상세_요구사항')):
        why = '응답이 너무 길어 잘렸습니다' if truncated else '응답을 JSON으로 해석할 수 없습니다'
        raise LLMParseError(why, raw=raw, truncated=truncated)
    if eml_data.get("received_date"):
        result["수신일"] = eml_data["received_date"]
    return result


# ─── 공용 저수준 호출 (analyze / search agent / 연결 테스트가 공유) ──────────────

def _chat(system_prompt, text, images=None, provider=None, model=None,
          anthropic_key=None, openai_key=None, timeout=None):
    """반환: (raw_text, tokens_in, tokens_out, provider, model, truncated)"""
    provider = (provider or DEFAULT_PROVIDER).strip()
    images = images or []
    timeout = timeout or LLM_TIMEOUT_SEC

    if provider == 'anthropic':
        model = model or ANTHROPIC_MODELS[0]
        out, tin, tout, trunc = _call_anthropic(anthropic_key, model, system_prompt, text, images, timeout)
    elif provider == 'openai':
        model = model or OPENAI_MODELS[0]
        if not openai_key:
            raise LLMError("OpenAI API 키가 설정되지 않았습니다.", kind='config')
        out, tin, tout, trunc = _call_openai_compatible(
            'https://api.openai.com/v1', openai_key, model, system_prompt, text, images, timeout)
    else:  # remote (기본)
        provider = 'remote'
        model = model or remote_model()
        base = _remote_base()
        if not base:
            raise LLMError("REMOTE_LLM_URL 환경변수가 설정되지 않았습니다.", kind='config')
        out, tin, tout, trunc = _call_openai_compatible(
            base, os.environ.get('REMOTE_LLM_KEY', 'EMPTY'), model, system_prompt, text, images, timeout)
    return out, tin, tout, provider, model, trunc


def chat_once(system_prompt, text, images=None, provider=None, model=None,
              anthropic_key=None, openai_key=None, timeout=None):
    """단일 채팅 호출. 반환: (raw_text, tokens_in, tokens_out, resolved_provider, resolved_model)"""
    out, tin, tout, provider, model, _ = _chat(
        system_prompt, text, images, provider, model, anthropic_key, openai_key, timeout)
    return out, tin, tout, provider, model


# ─── 진입점 ──────────────────────────────────────────────────────────────────

def analyze(eml_data, attachments, system_prompt=None, provider=None, model=None,
            anthropic_key=None, openai_key=None):
    system_prompt = system_prompt or SYSTEM_PROMPT
    text, images = _build_content(eml_data, attachments)
    try:
        out, tin, tout, provider, model, trunc = _chat(
            system_prompt, text, images, provider, model, anthropic_key, openai_key)
    except Exception as e:
        # 이미지 때문에 요청이 거부되는 경우(텍스트 전용 모델, 이미지 형식 등) 텍스트만으로 1회 재시도
        if not images or classify(e) != 'invalid':
            raise
        out, tin, tout, provider, model, trunc = _chat(
            system_prompt, text + "\n\n(첨부 이미지는 처리할 수 없어 제외했습니다.)", [],
            provider, model, anthropic_key, openai_key)
    result = _parse_result(out, eml_data, trunc)
    return result, {'provider': provider, 'model': model, 'tokens_in': tin, 'tokens_out': tout}


def test_connection(provider=None, model=None, anthropic_key=None, openai_key=None):
    """설정 화면 '연결 테스트' 버튼용. 최소 왕복 호출로 프로바이더/모델/키가 실제로
    동작하는지 확인. 실패해도 예외를 던지지 않고 결과 dict로 반환한다."""
    import time
    t0 = time.monotonic()
    try:
        out, tin, tout, resolved_provider, resolved_model = chat_once(
            "당신은 연결 테스트용 어시스턴트입니다. 'ok'라고만 답하세요.",
            "연결 테스트입니다. 'ok'라고 답해주세요.",
            [], provider, model, anthropic_key, openai_key, timeout=30,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            'ok': True,
            'message': f'응답 확인됨 ({latency_ms}ms)',
            'latency_ms': latency_ms,
            'provider': resolved_provider,
            'model': resolved_model,
            'sample': (out or '')[:80],
        }
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            'ok': False,
            'message': str(e)[:300],
            'latency_ms': latency_ms,
            'provider': provider or DEFAULT_PROVIDER,
            'model': model,
        }
