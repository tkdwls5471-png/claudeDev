import httpx
import pytest
import anthropic
import openai

import llm_provider as lp

REQ = httpx.Request('POST', 'http://llm.local/v1/chat/completions')


def _resp(status):
    return httpx.Response(status, request=REQ)


@pytest.mark.parametrize('err, kind', [
    (openai.APITimeoutError(request=REQ), 'transient'),
    (openai.APIConnectionError(request=REQ), 'transient'),
    (openai.RateLimitError('slow down', response=_resp(429), body=None), 'transient'),
    (openai.RateLimitError('insufficient_quota', response=_resp(429), body=None), 'config'),
    (openai.InternalServerError('boom', response=_resp(500), body=None), 'transient'),
    (openai.AuthenticationError('bad key', response=_resp(401), body=None), 'config'),
    (openai.NotFoundError('The model `x` does not exist', response=_resp(404), body=None), 'config'),
    (openai.BadRequestError('image too large', response=_resp(400), body=None), 'invalid'),
    (anthropic.InternalServerError('overloaded', response=_resp(529), body=None), 'transient'),
    (anthropic.BadRequestError('Your credit balance is too low', response=_resp(400), body=None), 'config'),
    (TimeoutError(), 'transient'),
    (lp.LLMError('키 없음', kind='config'), 'config'),
    (lp.LLMParseError('x'), 'parse'),
])
def test_classify(err, kind):
    assert lp.classify(err) == kind


def test_parse_result_raises_instead_of_fake_result():
    with pytest.raises(lp.LLMParseError):
        lp._parse_result('죄송합니다, 분석할 수 없습니다.', {})
    with pytest.raises(lp.LLMParseError) as ei:
        lp._parse_result('{"요구사항_요약": "잘린', {}, truncated=True)
    assert ei.value.truncated
    r = lp._parse_result('```json\n{"요구사항_요약": "ok"}\n```', {'received_date': '2026-10-13 09:00'})
    assert r == {'요구사항_요약': 'ok', '수신일': '2026-10-13 09:00'}


def test_analyze_retries_without_images_when_rejected(monkeypatch):
    calls = []

    def fake_chat(system_prompt, text, images, *a, **kw):
        calls.append(len(images))
        if images:
            raise openai.BadRequestError('image not supported', response=_resp(400), body=None)
        return '{"요구사항_요약": "텍스트만"}', 1, 1, 'remote', 'm', False
    monkeypatch.setattr(lp, '_chat', fake_chat)
    monkeypatch.setattr(lp, '_prep_image', lambda b64: (b64, 'image/png'))

    result, usage = lp.analyze({'subject': 's'}, [{'type': 'image', 'filename': 'a.png', 'content': 'AAAA'}])
    assert calls == [1, 0] and result['요구사항_요약'] == '텍스트만'


def test_missing_remote_url_is_config_error(monkeypatch):
    monkeypatch.delenv('REMOTE_LLM_URL', raising=False)
    with pytest.raises(lp.LLMError) as ei:
        lp.chat_once('s', 't', provider='remote')
    assert ei.value.kind == 'config'
