import os
import json
import anthropic
import pandas as pd
import io
from flask import Flask, render_template, request, send_file, Response
from dotenv import load_dotenv

from eml_parser import parse_eml
from attachment_handler import process_attachment
from claude_analyzer import build_messages

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB 제한

# ─── Utility Functions ───────────────────────────────────────────────────────

def export_excel(results):
    """분석 결과를 Excel로 변환"""
    rows = []
    for r in results:
        details = r["result"]
        rows.append({
            "파일명": r["filename"],
            "요청_업체": details.get("요청_업체", ""),
            "수신일": details.get("수신일", ""),
            "마감일": details.get("마감일", ""),
            "마감일_원문": details.get("마감일_원문", ""),
            "요청_유형": details.get("요청_유형", ""),
            "우선순위": details.get("우선순위", ""),
            "요구사항_요약": details.get("요구사항_요약", ""),
            "상세_요구사항": "\n".join(details.get("상세_요구사항", [])),
            "추가_참고사항": details.get("추가_참고사항", ""),
        })

    df = pd.DataFrame(rows)
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="요구사항분석")
        workbook = writer.book
        worksheet = writer.sheets["요구사항분석"]

        header_fmt = workbook.add_format({
            "bold": True, "bg_color": "#1d4ed8", "font_color": "white",
            "border": 1, "align": "center", "valign": "vcenter"
        })
        wrap_fmt = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1})

        col_widths = [20, 20, 18, 15, 25, 12, 8, 50, 50, 30]
        for col_num, (col_name, width) in enumerate(zip(df.columns, col_widths)):
            worksheet.write(0, col_num, col_name, header_fmt)
            worksheet.set_column(col_num, col_num, width, wrap_fmt)

        worksheet.set_row(0, 22)
        worksheet.freeze_panes(1, 0)

    output.seek(0)
    return output.getvalue()


def analyze_with_api_key(file_bytes, filename, api_key):
    """API Key를 직접 받아 분석 수행"""
    try:
        print(f"[분석 시작] {filename}")

        # 1. EML 파싱
        print(f"  1. EML 파싱 중...")
        eml_data = parse_eml(file_bytes)
        print(f"     - 제목: {eml_data.get('subject', 'N/A')}")
        print(f"     - 발신자: {eml_data.get('sender', 'N/A')}")

        # 2. 첨부파일 처리
        print(f"  2. 첨부파일 처리 중... ({len(eml_data.get('attachments', []))}개)")
        processed_attachments = []
        for att in eml_data["attachments"]:
            processed = process_attachment(att)
            processed_attachments.append(processed)
            print(f"     - {att['filename']}: {processed['type']}")

        # 3. Claude API 호출
        print(f"  3. Claude API 호출 중...")
        client = anthropic.Anthropic(api_key=api_key)
        messages = build_messages(eml_data, processed_attachments)
        print(f"     - 메시지 블록: {len(messages)}")

        response = client.messages.create(
            model="claude-opus-4-1-20250805",
            max_tokens=2048,
            system="""당신은 이메일과 첨부파일을 분석하여 업체 요구사항을 정리하는 전문가입니다.

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
- 반드시 유효한 JSON만 반환하세요 (마크다운 코드블록 없이)""",
            messages=messages,
        )

        # 4. JSON 파싱
        print(f"  4. 응답 파싱 중...")
        result_text = response.content[0].text
        result = json.loads(result_text)
        print(f"  ✓ 분석 완료: {result.get('요구사항_요약', 'N/A')[:50]}...")
        return result, None

    except json.JSONDecodeError as e:
        error_msg = f"JSON 파싱 실패: {str(e)}"
        print(f"  ✗ {error_msg}")
        return None, error_msg
    except anthropic.APIError as e:
        error_msg = f"Claude API 오류: {str(e)}"
        print(f"  ✗ {error_msg}")
        return None, error_msg
    except Exception as e:
        error_msg = f"분석 중 오류: {str(e)}"
        print(f"  ✗ {error_msg}")
        import traceback
        traceback.print_exc()
        return None, error_msg


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    """SSE 스트리밍으로 파일 분석 결과 전송"""
    api_key = request.form.get('api_key', '').strip()

    if not api_key:
        return Response(
            'data: ' + json.dumps({"type": "error", "message": "API Key가 필요합니다"}) + '\n\n',
            mimetype='text/event-stream'
        )

    uploaded_files = request.files.getlist('files[]')
    if not uploaded_files:
        return Response(
            'data: ' + json.dumps({"type": "error", "message": "파일이 없습니다"}) + '\n\n',
            mimetype='text/event-stream'
        )

    # 요청 컨텍스트 내에서 모든 파일을 미리 메모리에 읽기
    print(f"[/analyze] {len(uploaded_files)}개 파일 수신")
    files = []
    for f in uploaded_files:
        file_data = f.read()
        files.append((f.filename, file_data))
        print(f"  - {f.filename}: {len(file_data)} bytes")

    def generate():
        all_results = []

        for idx, (filename, file_bytes) in enumerate(files):
            try:
                # 진행률 전송
                yield f'data: {json.dumps({"type": "progress", "index": idx, "total": len(files), "file": filename})}\n\n'

                # 분석 수행
                result, error = analyze_with_api_key(file_bytes, filename, api_key)

                if error:
                    yield f'data: {json.dumps({"type": "error", "file": filename, "message": error})}\n\n'
                else:
                    all_results.append({"filename": filename, "result": result})
                    yield f'data: {json.dumps({"type": "result", "file": filename, "data": result})}\n\n'

            except Exception as e:
                print(f"  ✗ {filename}: {str(e)}")
                yield f'data: {json.dumps({"type": "error", "file": filename, "message": str(e)})}\n\n'

        yield f'data: {json.dumps({"type": "done", "results": all_results})}\n\n'

    return Response(generate(), mimetype='text/event-stream')


@app.route('/download', methods=['POST'])
def download():
    """분석 결과를 Excel로 다운로드"""
    try:
        data = request.get_json()
        results = data.get('results', [])

        if not results:
            return {'error': '다운로드할 결과가 없습니다'}, 400

        excel_bytes = export_excel(results)

        return send_file(
            io.BytesIO(excel_bytes),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='요구사항_분석결과.xlsx'
        )

    except Exception as e:
        return {'error': str(e)}, 500


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
