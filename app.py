import streamlit as st
import pandas as pd
import io
import os
from dotenv import load_dotenv

from eml_parser import parse_eml
from attachment_handler import process_attachment, SUPPORTED_EXTENSIONS
from claude_analyzer import analyze_email

load_dotenv()

st.set_page_config(
    page_title="EML 요구사항 분석기",
    page_icon="📧",
    layout="wide",
)

# CSS 스타일
st.markdown("""
<style>
    .main-title { font-size: 2rem; font-weight: 700; color: #1f2937; margin-bottom: 0.25rem; }
    .sub-title { color: #6b7280; margin-bottom: 2rem; }
    .result-card {
        background: #f9fafb;
        border: 1px solid #e5e7eb;
        border-radius: 0.75rem;
        padding: 1.25rem;
        margin-bottom: 1rem;
    }
    .priority-high { color: #dc2626; font-weight: 700; }
    .priority-medium { color: #d97706; font-weight: 700; }
    .priority-low { color: #16a34a; font-weight: 700; }
    .badge {
        display: inline-block;
        padding: 0.15rem 0.6rem;
        border-radius: 9999px;
        font-size: 0.8rem;
        font-weight: 600;
        margin-right: 0.25rem;
    }
</style>
""", unsafe_allow_html=True)


def check_api_key() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return bool(key and key != "your_api_key_here")


def render_sidebar():
    with st.sidebar:
        st.markdown("### ⚙️ 설정")

        api_key_input = st.text_input(
            "Anthropic API Key",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            type="password",
            placeholder="sk-ant-...",
        )
        if api_key_input:
            os.environ["ANTHROPIC_API_KEY"] = api_key_input

        st.markdown("---")
        st.markdown("### 📋 지원 파일 형식")
        st.markdown("""
- 📧 **EML** - 이메일 파일
- 📎 **첨부파일 자동 분석**
  - 📄 PDF (텍스트/스캔)
  - 🖼️ 이미지 (PNG/JPG 등)
  - 📊 Excel (xlsx/xls)
  - 📑 PPT (pptx/ppt)
""")
        st.markdown("---")
        st.markdown("### ℹ️ 사용 방법")
        st.markdown("""
1. API Key 입력
2. EML 파일 업로드 (복수 선택 가능)
3. **분석 시작** 버튼 클릭
4. 결과 확인 및 Excel 다운로드
""")

    return api_key_input


def priority_badge(priority: str) -> str:
    colors = {"높음": "#fee2e2", "보통": "#fef3c7", "낮음": "#dcfce7"}
    text_colors = {"높음": "#dc2626", "보통": "#d97706", "낮음": "#16a34a"}
    bg = colors.get(priority, "#f3f4f6")
    tc = text_colors.get(priority, "#374151")
    return f'<span class="badge" style="background:{bg};color:{tc};">{priority}</span>'


def type_badge(req_type: str) -> str:
    return f'<span class="badge" style="background:#dbeafe;color:#1d4ed8;">{req_type}</span>'


def render_result_card(idx: int, result: dict, filename: str):
    priority = result.get("우선순위", "보통")
    req_type = result.get("요청_유형", "기타")

    with st.container():
        st.markdown(f"""
<div class="result-card">
<h3 style="margin:0 0 0.5rem 0;">📧 {filename}</h3>
{priority_badge(priority)} {type_badge(req_type)}
</div>
""", unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**📅 수신일시**")
            st.info(result.get("수신일", "-"))

            st.markdown("**⏰ 마감일**")
            deadline = result.get("마감일", "명시되지 않음")
            deadline_raw = result.get("마감일_원문", "")
            if deadline and deadline != "명시되지 않음":
                st.error(f"{deadline}")
            else:
                st.warning("명시되지 않음")
            if deadline_raw:
                st.caption(f"원문: {deadline_raw}")

        with col2:
            st.markdown("**🏢 요청 업체/담당자**")
            st.info(result.get("요청_업체", "-"))

            st.markdown("**📝 요청 유형**")
            st.info(result.get("요청_유형", "-"))

        st.markdown("**💡 요구사항 요약**")
        st.success(result.get("요구사항_요약", "-"))

        detail_items = result.get("상세_요구사항", [])
        if detail_items:
            st.markdown("**📋 상세 요구사항**")
            for i, item in enumerate(detail_items, 1):
                st.markdown(f"{i}. {item}")

        extra = result.get("추가_참고사항", "")
        if extra and extra not in ("없음", "-", ""):
            with st.expander("추가 참고사항"):
                st.write(extra)

        st.markdown("---")


def results_to_dataframe(results: list) -> pd.DataFrame:
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
    return pd.DataFrame(rows)


def export_excel(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="요구사항분석")
        workbook = writer.book
        worksheet = writer.sheets["요구사항분석"]

        # 헤더 서식
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

    return output.getvalue()


# ─── 메인 ───────────────────────────────────────────────────────────────────

def main():
    render_sidebar()

    st.markdown('<div class="main-title">📧 EML 요구사항 분석기</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">업체 이메일에서 요구사항·마감일을 자동으로 추출합니다</div>', unsafe_allow_html=True)

    if not check_api_key():
        st.warning("⚠️ 왼쪽 사이드바에서 Anthropic API Key를 먼저 입력해주세요.")

    uploaded_files = st.file_uploader(
        "EML 파일을 업로드하세요 (여러 개 선택 가능)",
        type=["eml"],
        accept_multiple_files=True,
    )

    if not uploaded_files:
        st.markdown("""
        <div style="text-align:center; padding: 3rem; color: #9ca3af;">
            <div style="font-size:4rem;">📨</div>
            <div style="font-size:1.1rem;">EML 파일을 드래그하거나 클릭하여 업로드하세요</div>
        </div>
        """, unsafe_allow_html=True)
        return

    st.markdown(f"**{len(uploaded_files)}개 파일 선택됨**")

    col_btn, col_space = st.columns([1, 4])
    with col_btn:
        start = st.button("🔍 분석 시작", type="primary", use_container_width=True)

    if not start:
        return

    if not check_api_key():
        st.error("API Key를 입력해주세요.")
        return

    st.markdown("---")
    all_results = []
    progress = st.progress(0)
    status_text = st.empty()

    for i, uploaded_file in enumerate(uploaded_files):
        status_text.text(f"분석 중... ({i+1}/{len(uploaded_files)}) {uploaded_file.name}")

        try:
            file_bytes = uploaded_file.read()
            eml_data = parse_eml(file_bytes)

            # 첨부파일 처리
            processed_attachments = []
            for att in eml_data["attachments"]:
                processed = process_attachment(att)
                processed_attachments.append(processed)

            # Claude 분석
            result = analyze_email(eml_data, processed_attachments)
            all_results.append({"filename": uploaded_file.name, "result": result})

        except Exception as e:
            st.error(f"오류 발생 ({uploaded_file.name}): {e}")
            all_results.append({
                "filename": uploaded_file.name,
                "result": {
                    "요구사항_요약": f"분석 실패: {e}",
                    "상세_요구사항": [],
                    "수신일": "",
                    "마감일": "",
                    "마감일_원문": "",
                    "요청_업체": "",
                    "요청_유형": "기타",
                    "우선순위": "보통",
                    "추가_참고사항": "",
                }
            })

        progress.progress((i + 1) / len(uploaded_files))

    status_text.text("✅ 분석 완료!")
    progress.empty()

    # 결과 표시
    st.markdown(f"## 분석 결과 ({len(all_results)}건)")

    # 요약 테이블
    df = results_to_dataframe(all_results)
    with st.expander("📊 요약 테이블 보기", expanded=False):
        st.dataframe(
            df[["파일명", "요청_업체", "수신일", "마감일", "요청_유형", "우선순위"]],
            use_container_width=True,
        )

    # Excel 다운로드
    excel_bytes = export_excel(df)
    st.download_button(
        label="📥 Excel로 다운로드",
        data=excel_bytes,
        file_name="요구사항_분석결과.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("---")

    # 개별 카드 결과
    for idx, r in enumerate(all_results):
        render_result_card(idx, r["result"], r["filename"])


if __name__ == "__main__":
    main()
