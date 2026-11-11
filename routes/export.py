"""선택한 메일의 분석 결과 Excel 다운로드."""
import io

import pandas as pd
from flask import Blueprint, jsonify, send_file
from flask_login import current_user, login_required

import email_service as es
import timeutil
from db import Email
from routes.common import db_session, json_body

bp = Blueprint('export', __name__)

_STATUS_KO = {'none': '미분석', 'queued': '분석 대기', 'running': '분석 중', 'done': '분석 완료', 'failed': '분석 실패'}

# (열 이름, 너비, 값 꺼내기)
_COLUMNS = [
    ('제목', 30, lambda e, d: e.subject or ''),
    ('요청_업체', 20, lambda e, d: d.get('요청_업체', '') or (e.sender or '')),
    ('수신일', 18, lambda e, d: timeutil.to_kst_str(e.received_at) or (e.received_date or '')),
    ('마감일', 12, lambda e, d: e.deadline.isoformat() if e.deadline else ''),
    ('마감일_원문', 25, lambda e, d: d.get('마감일_원문', '')),
    ('요청_유형', 14, lambda e, d: d.get('요청_유형', '')),
    ('우선순위', 8, lambda e, d: d.get('우선순위', '')),
    ('처리상태', 10, lambda e, d: '제외' if e.is_excluded else ('완료' if e.is_completed else '미완료')),
    ('분석상태', 10, lambda e, d: _STATUS_KO.get(e.analysis_status, e.analysis_status or '')),
    ('요구사항_요약', 50, lambda e, d: d.get('요구사항_요약', '')),
    ('상세_요구사항', 50, lambda e, d: '\n'.join(str(x) for x in (d.get('상세_요구사항') or []))),
    ('추가_참고사항', 30, lambda e, d: d.get('추가_참고사항', '')),
]


def to_excel(emails) -> bytes:
    df = pd.DataFrame([{name: get(e, e.analysis_result or {}) for name, _, get in _COLUMNS} for e in emails],
                      columns=[c[0] for c in _COLUMNS])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='요구사항분석')
        wb, ws = writer.book, writer.sheets['요구사항분석']
        hfmt = wb.add_format({'bold': True, 'bg_color': '#1d4ed8', 'font_color': 'white',
                              'border': 1, 'align': 'center', 'valign': 'vcenter'})
        wfmt = wb.add_format({'text_wrap': True, 'valign': 'top', 'border': 1})
        for i, (name, width, _) in enumerate(_COLUMNS):
            ws.write(0, i, name, hfmt)
            ws.set_column(i, i, width, wfmt)
        ws.set_row(0, 22)
        ws.freeze_panes(1, 0)
    return buf.getvalue()


@bp.route('/download/emails', methods=['POST'])
@login_required
def download_emails():
    email_ids = json_body().get('email_ids') or [-1]
    with db_session() as db:
        ids = es.user_account_ids(db, current_user.id, active_only=False) or [-1]
        emails = db.query(Email).filter(Email.id.in_(email_ids), Email.account_id.in_(ids)) \
            .order_by(Email.received_at.desc()).all()
        if not emails:
            return jsonify(success=False, message='다운로드할 메일이 없습니다'), 400
        excel = to_excel(emails)
    return send_file(io.BytesIO(excel), as_attachment=True, download_name='요구사항_분석결과.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
