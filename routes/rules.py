"""발신자·도메인 제외 규칙."""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func

import email_service as es
import timeutil
from db import Email, SenderRule
from routes.common import db_session, json_body

bp = Blueprint('rules', __name__)


def _rule_json(db, r):
    ids = es.user_account_ids(db, current_user.id, active_only=False) or [-1]
    excluded = db.query(func.count(Email.id)).filter(
        Email.account_id.in_(ids), Email.is_excluded == True,  # noqa: E712
        Email.exclude_reason == es.rule_label(r.kind, r.value)).scalar() or 0
    return {'id': r.id, 'kind': r.kind, 'value': r.value, 'excluded_count': excluded,
            'created_at': timeutil.to_kst_str(r.created_at)}


@bp.route('/api/sender-rules', methods=['GET'])
@login_required
def list_sender_rules():
    with db_session() as db:
        rules = db.query(SenderRule).filter_by(user_id=current_user.id).order_by(SenderRule.created_at.desc()).all()
        return jsonify(rules=[_rule_json(db, r) for r in rules],
                       suggestions=es.suggest_domains(db, current_user.id))


@bp.route('/api/sender-rules/preview', methods=['POST'])
@login_required
def preview_sender_rule():
    """규칙을 만들기 전에: 정리된 값과, 기존 메일 중 새로 제외될 건수."""
    data = json_body()
    try:
        value = es.normalize_rule(data.get('kind'), data.get('value'))
    except ValueError as ex:
        return jsonify(success=False, message=str(ex)), 400
    with db_session() as db:
        return jsonify(success=True, kind=data.get('kind'), value=value,
                       matches=es.count_rule_matches(db, current_user.id, data.get('kind'), value))


@bp.route('/api/sender-rules', methods=['POST'])
@login_required
def add_sender_rule():
    data = json_body()
    kind = data.get('kind')
    try:
        value = es.normalize_rule(kind, data.get('value'))
    except ValueError as ex:
        return jsonify(success=False, message=str(ex)), 400
    with db_session() as db:
        r = db.query(SenderRule).filter_by(user_id=current_user.id, kind=kind, value=value).first()
        if r is None:
            r = SenderRule(user_id=current_user.id, kind=kind, value=value)
            db.add(r)
            db.commit()
        affected = es.apply_rule_to_existing(db, current_user.id, kind, value) if data.get('apply_existing', True) else 0
        return jsonify(success=True, rule=_rule_json(db, r), affected=affected)


@bp.route('/api/sender-rules/<int:rule_id>', methods=['DELETE'])
@login_required
def delete_sender_rule(rule_id):
    """규칙 삭제. ?restore=1 이면 그 규칙으로 자동 제외됐던 메일도 되돌린다."""
    with db_session() as db:
        r = db.query(SenderRule).filter_by(id=rule_id, user_id=current_user.id).first()
        if r is None:
            return jsonify(success=False, message='규칙을 찾을 수 없습니다'), 404
        restored = es.restore_rule(db, current_user.id, r.kind, r.value) if request.args.get('restore') == '1' else 0
        db.delete(r)
        db.commit()
        return jsonify(success=True, restored=restored)
