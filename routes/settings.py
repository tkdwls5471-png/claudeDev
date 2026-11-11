"""사용자 설정: API 키, LLM 프로바이더·모델, 자동 분석, 분석 지침."""
from flask import Blueprint, jsonify
from flask_login import current_user, login_required

import analysis_worker
import email_service as es
import llm_provider
from db import Setting, encrypt
from routes.common import db_session, json_body

bp = Blueprint('settings', __name__)

KEY_PROVIDERS = {
    'anthropic': 'anthropic_api_key',
    'openai': 'openai_api_key',
}


def _mask_key(key):
    """키 원문은 프론트에 내려보내지 않고 확인용 마스킹만 보낸다."""
    if not key:
        return None
    if len(key) <= 8:
        return key[:2] + '...'
    return key[:5] + '...' + key[-4:]


def _put(db, key, value):
    s = db.query(Setting).filter_by(user_id=current_user.id, key=key).first()
    enc = encrypt(value) if value else ''
    if s:
        s.value_enc = enc
    else:
        db.add(Setting(user_id=current_user.id, key=key, value_enc=enc))


def _get(db, key, default=None):
    return es.get_setting(db, current_user.id, key, default)


@bp.route('/api/settings/keys', methods=['GET'])
@login_required
def list_api_keys():
    with db_session() as db:
        out = {}
        for provider, setting_key in KEY_PROVIDERS.items():
            val = _get(db, setting_key)
            out[provider] = {'set': bool(val), 'preview': _mask_key(val)}
        return jsonify(out)


@bp.route('/api/settings/keys/<provider>', methods=['PUT'])
@login_required
def set_api_key(provider):
    if provider not in KEY_PROVIDERS:
        return jsonify(success=False, message='알 수 없는 프로바이더입니다'), 400
    key = (json_body().get('api_key') or '').strip()
    if not key:
        return jsonify(success=False, message='API 키를 입력하세요'), 400
    with db_session() as db:
        _put(db, KEY_PROVIDERS[provider], key)
        db.commit()
    analysis_worker.clear_pause(current_user.id)
    return jsonify(success=True, preview=_mask_key(key))


@bp.route('/api/settings/keys/<provider>', methods=['DELETE'])
@login_required
def delete_api_key(provider):
    if provider not in KEY_PROVIDERS:
        return jsonify(success=False, message='알 수 없는 프로바이더입니다'), 400
    with db_session() as db:
        s = db.query(Setting).filter_by(user_id=current_user.id, key=KEY_PROVIDERS[provider]).first()
        if s:
            db.delete(s)
            db.commit()
        return jsonify(success=True)


@bp.route('/api/settings/llm', methods=['GET'])
@login_required
def get_llm_settings():
    with db_session() as db:
        return jsonify(
            provider=_get(db, 'llm_provider') or 'remote',
            model=_get(db, 'llm_model') or '',
            options=llm_provider.model_options(),
        )


@bp.route('/api/settings/llm', methods=['PUT'])
@login_required
def set_llm_settings():
    data = json_body()
    with db_session() as db:
        _put(db, 'llm_provider', (data.get('provider') or 'remote').strip())
        _put(db, 'llm_model', (data.get('model') or '').strip())
        db.commit()
    analysis_worker.clear_pause(current_user.id)
    return jsonify(success=True)


@bp.route('/api/settings/llm/test', methods=['POST'])
@login_required
def test_llm_connection():
    """'연결 테스트' 버튼. api_key가 함께 오면(저장 전 임시 테스트) 그 값을 우선 사용."""
    data = json_body()
    provider = (data.get('provider') or 'remote').strip()
    model = (data.get('model') or '').strip() or None
    typed_key = (data.get('api_key') or '').strip() or None
    with db_session() as db:
        anthropic_key = (typed_key if provider == 'anthropic' else None) or _get(db, 'anthropic_api_key')
        openai_key = (typed_key if provider == 'openai' else None) or _get(db, 'openai_api_key')
    return jsonify(llm_provider.test_connection(provider, model, anthropic_key, openai_key))


@bp.route('/api/settings/auto-analyze', methods=['GET'])
@login_required
def get_auto_analyze_setting():
    with db_session() as db:
        return jsonify(enabled=es.auto_analyze_enabled(db, current_user.id))


@bp.route('/api/settings/auto-analyze', methods=['PUT'])
@login_required
def set_auto_analyze_setting():
    enabled = bool(json_body().get('enabled'))
    with db_session() as db:
        _put(db, 'auto_analyze_enabled', 'true' if enabled else 'false')
        db.commit()
        changed = es.queue_recent_unanalyzed(db, current_user.id) if enabled else es.unqueue_auto(db, current_user.id)
    if enabled:
        analysis_worker.wake()
    return jsonify(success=True, enabled=enabled, changed=changed)


@bp.route('/api/settings/prompt', methods=['GET'])
@login_required
def get_analysis_prompt():
    with db_session() as db:
        return jsonify(prompt=es.get_analysis_prompt(db, current_user.id))


@bp.route('/api/settings/prompt', methods=['PUT'])
@login_required
def set_analysis_prompt():
    with db_session() as db:
        _put(db, 'analysis_prompt', (json_body().get('prompt') or '').strip())
        db.commit()
        return jsonify(success=True)
