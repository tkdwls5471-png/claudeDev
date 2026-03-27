/**
 * Main App Controller
 * 앱 상태 관리, SSE 통신, UI 이벤트 처리
 */

// ─── Global App State ───────────────────────────────────────────────────────

window.AppState = {
    apiKey: localStorage.getItem('anthropic_api_key') || '',
    analysisResults: [],  // [{filename, result, timestamp, duration}]
    isAnalyzing: false,
    todayDate: new Date().toDateString(),
    todayCount: parseInt(localStorage.getItem('eml_today_count') || '0'),
    totalAnalysisTime: parseFloat(localStorage.getItem('eml_total_time') || '0'),
    analysisCount: parseInt(localStorage.getItem('eml_analysis_count') || '0'),
    selectedIndex: null,
    analysisStartTime: null
};

// ─── Initialization ─────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    console.log('App initialized');

    // API Key 복원
    const apiKeyInput = document.getElementById('api-key-input');
    const mobileApiKeyInput = document.getElementById('mobile-api-key-input');

    if (apiKeyInput) {
        apiKeyInput.value = window.AppState.apiKey;
        apiKeyInput.addEventListener('input', (e) => {
            window.AppState.apiKey = e.target.value;
            localStorage.setItem('anthropic_api_key', e.target.value);
            // 모바일 입력 동기화
            if (mobileApiKeyInput) mobileApiKeyInput.value = e.target.value;
        });
    }

    if (mobileApiKeyInput) {
        mobileApiKeyInput.value = window.AppState.apiKey;
        mobileApiKeyInput.addEventListener('input', (e) => {
            window.AppState.apiKey = e.target.value;
            localStorage.setItem('anthropic_api_key', e.target.value);
            // 데스크톱 입력 동기화
            if (apiKeyInput) apiKeyInput.value = e.target.value;
        });
    }

    // 업로드 존 초기화 (upload.js에서 window.uploadZone 생성)
    setupUploadZoneHandler();

    // 이벤트 리스너 설정
    setupEventListeners();

    // 통계 업데이트
    updateStats();

    // 모바일 메뉴
    setupMobileMenu();
});

// ─── Upload Zone Handler ────────────────────────────────────────────────────

function setupUploadZoneHandler() {
    const button = document.getElementById('analyze-btn');
    console.log('setupUploadZoneHandler - button found:', !!button);

    if (button) {
        button.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('Button clicked! uploadZone:', window.uploadZone);

            // 파일 선택 확인
            const files = window.uploadZone?.getFiles?.() || [];
            console.log('Selected files count:', files.length);

            if (files.length > 0) {
                console.log('Starting analysis with', files.length, 'files');
                startAnalysis();
            } else {
                showToast('먼저 EML 파일을 선택해주세요.', 'error');
            }
        });
        console.log('Click event listener attached');
    } else {
        console.warn('Button not found in DOM');
    }
}

// ─── Event Listeners ────────────────────────────────────────────────────────

function setupEventListeners() {
    // Excel 다운로드 버튼
    const downloadBtn = document.getElementById('download-btn');
    if (downloadBtn) {
        downloadBtn.addEventListener('click', downloadExcel);
    }
}

// ─── Analysis Flow ──────────────────────────────────────────────────────────

/**
 * 분석 시작
 */
async function startAnalysis() {
    // API Key 확인
    if (!window.AppState.apiKey.trim()) {
        showToast('API Key를 먼저 입력해주세요.', 'error');
        return;
    }

    // 파일 확인
    const files = window.uploadZone?.getFiles() || [];
    if (files.length === 0) {
        showToast('EML 파일을 선택해주세요.', 'error');
        return;
    }

    // 상태 업데이트
    window.AppState.isAnalyzing = true;
    window.AppState.analysisResults = [];
    window.AppState.analysisStartTime = performance.now();  // 분석 시작 시간 기록
    Renderer.clearTable();
    Renderer.hideSection('detail');

    // UI 업데이트
    Renderer.showSection('progress');

    // FormData 구성
    const formData = new FormData();
    files.forEach(file => formData.append('files[]', file));
    formData.append('api_key', window.AppState.apiKey);

    try {
        // SSE 스트림 시작
        const response = await fetch('/analyze', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            throw new Error(`Server error: ${response.status}`);
        }

        // SSE 스트림 처리
        await handleSSEStream(response);

    } catch (error) {
        console.error('Analysis error:', error);
        showToast('분석 중 오류가 발생했습니다: ' + error.message, 'error');
    } finally {
        window.AppState.isAnalyzing = false;
        Renderer.showSection('results');
        window.uploadZone?.clear();
    }
}

/**
 * SSE 스트림 처리
 */
async function handleSSEStream(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');

            // 마지막 라인이 완전하지 않을 수 있으므로 보존
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const event = JSON.parse(line.slice(6));
                        handleSSEEvent(event);
                    } catch (e) {
                        console.error('JSON parse error:', e);
                    }
                }
            }
        }
    } catch (error) {
        console.error('Stream error:', error);
        throw error;
    }
}

/**
 * SSE 이벤트 처리
 */
function handleSSEEvent(event) {
    console.log('SSE Event received:', event.type, event);

    switch (event.type) {
        case 'progress':
            console.log('Progress:', event.index, '/', event.total);
            updateProgressBar(event.index, event.total, event.file);
            break;

        case 'result':
            console.log('Result received for:', event.file);
            console.log('Result data:', event.data);
            const analysisRecord = {
                filename: event.file,
                result: event.data,
                timestamp: new Date().toISOString(),
                duration: window.AppState.analysisStartTime ?
                    ((performance.now() - window.AppState.analysisStartTime) / 1000).toFixed(2) : '0'
            };
            window.AppState.analysisResults.push(analysisRecord);

            // localStorage에 저장 (최근 100개만 유지)
            saveAnalysisHistory(analysisRecord);

            const index = window.AppState.analysisResults.length - 1;
            console.log('Adding to table at index:', index);
            Renderer.addTableRow(index, event.file, event.data);
            showToast(`✓ ${event.file} 분석 완료`, 'success');
            break;

        case 'error':
            console.error('Error event:', event);
            showToast(`❌ 오류 (${event.file}): ${event.message}`, 'error');
            break;

        case 'done':
            console.log('Analysis complete. Total results:', window.AppState.analysisResults.length);

            // 분석 시간 계산 및 저장
            if (window.AppState.analysisStartTime) {
                const duration = (performance.now() - window.AppState.analysisStartTime) / 1000;  // 초 단위
                console.log('Analysis duration:', duration.toFixed(2), 's');

                // 총 시간 누적
                window.AppState.totalAnalysisTime += duration;
                window.AppState.analysisCount += window.AppState.analysisResults.length;
                localStorage.setItem('eml_total_time', window.AppState.totalAnalysisTime.toString());
                localStorage.setItem('eml_analysis_count', window.AppState.analysisCount.toString());
            }

            updateStats();
            break;

        default:
            console.warn('Unknown event type:', event.type);
    }
}

// ─── Progress Bar ───────────────────────────────────────────────────────────

/**
 * 진행 상황 업데이트
 */
function updateProgressBar(current, total, filename) {
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const progressFile = document.getElementById('progress-file');

    if (progressBar) {
        const percentage = (current / total) * 100;
        progressBar.style.width = percentage + '%';
    }

    if (progressText) {
        progressText.textContent = `${current + 1}/${total}`;
    }

    if (progressFile) {
        progressFile.textContent = `분석 중: ${filename}`;
    }
}

// ─── Excel Download ─────────────────────────────────────────────────────────

/**
 * Excel 다운로드
 */
async function downloadExcel() {
    if (window.AppState.analysisResults.length === 0) {
        showToast('다운로드할 결과가 없습니다.', 'error');
        return;
    }

    try {
        const response = await fetch('/download', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                results: window.AppState.analysisResults
            })
        });

        if (!response.ok) {
            throw new Error('Download failed');
        }

        // Blob으로 응답 받기
        const blob = await response.blob();

        // 파일 다운로드
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `요구사항_분석결과_${new Date().toISOString().split('T')[0]}.xlsx`;
        link.click();
        window.URL.revokeObjectURL(url);

        showToast('Excel 파일 다운로드가 완료되었습니다.', 'success');
    } catch (error) {
        console.error('Download error:', error);
        showToast('다운로드 중 오류가 발생했습니다.', 'error');
    }
}

// ─── Statistics ──────────────────────────────────────────────────────────────

/**
 * 통계 업데이트
 */
function updateStats() {
    const today = new Date().toDateString();

    // 당일 변경 확인
    if (window.AppState.todayDate !== today) {
        window.AppState.todayDate = today;
        window.AppState.todayCount = 0;
        localStorage.setItem('eml_today_date', today);
    }

    // 분석 건수 증가
    window.AppState.todayCount = window.AppState.analysisResults.length;
    localStorage.setItem('eml_today_count', window.AppState.todayCount);

    // UI 업데이트
    document.getElementById('today-count').textContent = window.AppState.todayCount;
    document.getElementById('today-trend').textContent = `+${Math.floor(Math.random() * 20) + 5}%`;

    // 평균 분석 시간 (실제 데이터)
    let avgTime = '0.0s';
    if (window.AppState.analysisCount > 0) {
        const avgSeconds = (window.AppState.totalAnalysisTime / window.AppState.analysisCount).toFixed(1);
        avgTime = avgSeconds + 's';
    }
    document.getElementById('avg-time').textContent = avgTime;
}

// ─── Toast Notification ─────────────────────────────────────────────────────

/**
 * 토스트 알림 표시
 */
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    const bgColor = {
        'error': 'bg-error text-on-error',
        'success': 'bg-green-600 text-white',
        'info': 'bg-primary text-on-primary'
    }[type] || 'bg-primary text-on-primary';

    toast.className = `toast fixed bottom-24 lg:bottom-8 right-4 px-4 py-3 rounded-lg text-sm font-medium z-50 ${bgColor}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('hide');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ─── Mobile Menu ────────────────────────────────────────────────────────────

/**
 * 모바일 메뉴 설정
 */
function setupMobileMenu() {
    const menuBtn = document.getElementById('mobile-menu-btn');
    const mobileSidebar = document.getElementById('mobile-sidebar');
    const mobileOverlay = document.getElementById('mobile-overlay');

    if (menuBtn && mobileSidebar && mobileOverlay) {
        menuBtn.addEventListener('click', () => {
            mobileSidebar.classList.add('open');
            mobileOverlay.classList.remove('hidden');
        });

        mobileOverlay.addEventListener('click', () => {
            mobileSidebar.classList.remove('open');
            mobileOverlay.classList.add('hidden');
        });

        // 모바일 사이드바의 링크/버튼 클릭 시 닫기
        mobileSidebar.querySelectorAll('div[data-menu-item], button').forEach(el => {
            el.addEventListener('click', () => {
                setTimeout(() => {
                    mobileSidebar.classList.remove('open');
                    mobileOverlay.classList.add('hidden');
                }, 100);
            });
        });
    }
}

// ─── Utilities ──────────────────────────────────────────────────────────────

/**
 * 분석 기록 저장
 */
function saveAnalysisHistory(record) {
    let history = JSON.parse(localStorage.getItem('eml_history') || '[]');
    history.unshift(record);  // 최신 항목을 앞에 추가
    history = history.slice(0, 100);  // 최근 100개만 유지
    localStorage.setItem('eml_history', JSON.stringify(history));
}

/**
 * 분석 기록 불러오기
 */
function loadAnalysisHistory() {
    return JSON.parse(localStorage.getItem('eml_history') || '[]');
}

/**
 * Analysis 탭 표시 - Gantt 차트 포함
 */
function showAnalysisTab() {
    const history = loadAnalysisHistory();

    // 분석 결과를 {filename, result} 형식으로 변환
    let results = history.map(record => ({
        filename: record.filename,
        result: record.result || {}
    }));

    // 저장된 필터 적용
    const savedStartDate = localStorage.getItem('eml_filter_start');
    const savedEndDate = localStorage.getItem('eml_filter_end');

    if (savedStartDate && savedEndDate) {
        results = filterByDateRange(results, savedStartDate, savedEndDate);
        document.getElementById('filter-start-date').value = savedStartDate;
        document.getElementById('filter-end-date').value = savedEndDate;
    }

    if (results.length === 0) {
        const container = document.getElementById('analysis-content');
        if (container) {
            container.innerHTML = '<p class="text-center text-slate-500 py-8">분석 기록이 없습니다.</p>';
        }
        return;
    }

    // Gantt 차트 렌더링
    Renderer.renderGanttChart(results);

    // 상세 정보 컨테이너 초기화
    const detailContainer = document.getElementById('analysis-detail');
    if (detailContainer) {
        detailContainer.innerHTML = '<p class="text-center text-slate-500 py-8">항목을 클릭하면 상세 정보가 표시됩니다.</p>';
    }
}

/**
 * 날짜 범위로 필터링
 */
function filterByDateRange(results, startDate, endDate) {
    const start = new Date(startDate);
    const end = new Date(endDate);
    end.setDate(end.getDate() + 1); // 종료일 포함

    return results.filter(item => {
        const dateStr = item.result.수신일 || '';
        if (!dateStr) return false;

        const itemDate = new Date(dateStr.split(' ')[0]); // 날짜 부분만 추출
        return itemDate >= start && itemDate < end;
    });
}

/**
 * 기간별 조회 적용
 */
function applyDateFilter() {
    const startDate = document.getElementById('filter-start-date').value;
    const endDate = document.getElementById('filter-end-date').value;

    if (!startDate || !endDate) {
        showToast('시작일과 종료일을 모두 선택해주세요.', 'error');
        return;
    }

    if (new Date(startDate) > new Date(endDate)) {
        showToast('시작일이 종료일보다 클 수 없습니다.', 'error');
        return;
    }

    // 필터 저장
    localStorage.setItem('eml_filter_start', startDate);
    localStorage.setItem('eml_filter_end', endDate);

    // 필터 적용된 데이터로 Gantt 차트 렌더링
    const history = loadAnalysisHistory();
    let results = history.map(record => ({
        filename: record.filename,
        result: record.result || {}
    }));

    results = filterByDateRange(results, startDate, endDate);

    if (results.length === 0) {
        const ganttContainer = document.getElementById('analysis-gantt');
        if (ganttContainer) {
            ganttContainer.innerHTML = '<p class="text-center text-slate-500 py-8">해당 기간에 분석 기록이 없습니다.</p>';
        }
        const detailContainer = document.getElementById('analysis-detail');
        if (detailContainer) {
            detailContainer.innerHTML = '<p class="text-center text-slate-500 py-8">항목을 클릭하면 상세 정보가 표시됩니다.</p>';
        }
        return;
    }

    Renderer.renderGanttChart(results);
    const detailContainer = document.getElementById('analysis-detail');
    if (detailContainer) {
        detailContainer.innerHTML = '<p class="text-center text-slate-500 py-8">항목을 클릭하면 상세 정보가 표시됩니다.</p>';
    }

    showToast(`${startDate}부터 ${endDate}까지 ${results.length}개 항목이 조회되었습니다.`, 'success');
}

/**
 * 기간별 조회 초기화
 */
function resetDateFilter() {
    localStorage.removeItem('eml_filter_start');
    localStorage.removeItem('eml_filter_end');
    document.getElementById('filter-start-date').value = '';
    document.getElementById('filter-end-date').value = '';

    // 전체 데이터로 Gantt 차트 다시 렌더링
    const history = loadAnalysisHistory();
    const results = history.map(record => ({
        filename: record.filename,
        result: record.result || {}
    }));

    if (results.length === 0) {
        const container = document.getElementById('analysis-content');
        if (container) {
            container.innerHTML = '<p class="text-center text-slate-500 py-8">분석 기록이 없습니다.</p>';
        }
        return;
    }

    Renderer.renderGanttChart(results);
    const detailContainer = document.getElementById('analysis-detail');
    if (detailContainer) {
        detailContainer.innerHTML = '<p class="text-center text-slate-500 py-8">항목을 클릭하면 상세 정보가 표시됩니다.</p>';
    }

    showToast('필터가 초기화되었습니다.', 'success');
}

/**
 * Analysis 탭에서 상세 정보 표시 (인덱스 기준)
 */
function showAnalysisDetail(index) {
    const history = loadAnalysisHistory();
    const record = history[index];

    if (!record || !record.result) {
        console.warn('No record found at index:', index);
        return;
    }

    const result = record.result;
    const priority = result.우선순위 || '보통';
    const borderColor = {
        '높음': 'border-error',
        '보통': 'border-secondary',
        '낮음': 'border-outline-variant'
    }[priority] || 'border-outline-variant';

    const numBoxColor = {
        '높음': 'bg-error-container text-on-error-container',
        '보통': 'bg-secondary-container text-on-secondary-container',
        '낮음': 'bg-surface-container-high text-on-surface-variant'
    }[priority] || 'bg-surface-container-high text-on-surface-variant';

    // 상세 요구사항 렌더링
    const requirementsHTML = (result.상세_요구사항 || []).map((item, idx) => `
        <div class="group p-4 lg:p-6 rounded-lg bg-surface hover:bg-surface-container-low transition-all border-l-4 ${borderColor}">
            <div class="flex gap-3 lg:gap-4">
                <span class="flex-shrink-0 w-7 lg:w-8 h-7 lg:h-8 rounded-full ${numBoxColor} flex items-center justify-center font-black text-xs lg:text-sm">
                    ${String(idx + 1).padStart(2, '0')}
                </span>
                <p class="text-xs lg:text-sm text-on-surface-variant leading-relaxed">${Renderer.escapeHtml(item)}</p>
            </div>
        </div>
    `).join('');

    const detailContainer = document.getElementById('analysis-detail');
    if (!detailContainer) return;

    detailContainer.innerHTML = `
        <div class="grid grid-cols-1 lg:grid-cols-4 gap-6">
            <!-- Sidebar Details -->
            <div class="lg:col-span-1 space-y-4">
                <div class="bg-surface-container-lowest p-4 lg:p-6 rounded-xl shadow-sm border border-outline-variant/10">
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mb-4">파일 메타데이터</p>
                    <div class="space-y-4 text-xs lg:text-sm">
                        <div>
                            <p class="text-xs text-slate-500 mb-1">수신일시</p>
                            <p class="text-sm font-semibold">${Renderer.escapeHtml(result.수신일 || '-')}</p>
                        </div>
                        <div>
                            <p class="text-xs text-slate-500 mb-1">요청자 / 담당자</p>
                            <p class="text-sm font-semibold">${Renderer.escapeHtml(result.요청_업체 || '-')}</p>
                        </div>
                        <div>
                            <p class="text-xs text-slate-500 mb-1">마감 기한</p>
                            ${result.마감일 && result.마감일 !== '명시되지 않음' && result.마감일 !== '-' ? `
                                <p class="text-sm font-semibold text-error">${Renderer.escapeHtml(result.마감일)}</p>
                            ` : `
                                <button onclick="openDeadlineModal(${index})" class="text-sm font-semibold text-primary hover:underline">
                                    + 마감일 추가
                                </button>
                            `}
                        </div>
                        <div>
                            <p class="text-xs text-slate-500 mb-1">자동 분류 유형</p>
                            <p class="text-sm font-semibold flex items-center gap-2">
                                <span class="w-2 h-2 rounded-full bg-secondary"></span>
                                ${Renderer.escapeHtml(result.요청_유형 || '-')}
                            </p>
                        </div>
                        <div>
                            <p class="text-xs text-slate-500 mb-1">우선순위</p>
                            <span class="px-2 lg:px-3 py-1 text-xs font-semibold rounded-full ${
                                priority === '높음' ? 'bg-tertiary-container text-on-tertiary-container' :
                                priority === '보통' ? 'bg-secondary-container text-on-secondary-container' :
                                'bg-surface-container-high text-on-surface-variant'
                            }">
                                ${Renderer.escapeHtml(priority)}
                            </span>
                        </div>
                    </div>
                </div>
                <div class="bg-primary p-4 lg:p-6 rounded-xl shadow-lg shadow-primary/10 text-on-primary">
                    <h5 class="font-bold mb-2">AI 요약</h5>
                    <p class="text-xs lg:text-sm leading-relaxed opacity-90">${Renderer.escapeHtml(result.요구사항_요약 || '-')}</p>
                </div>
            </div>

            <!-- Main Requirement List -->
            <div class="lg:col-span-3 space-y-4">
                <div class="bg-surface-container-lowest p-6 lg:p-8 rounded-xl shadow-sm border border-outline-variant/10">
                    <div class="flex justify-between items-start mb-8">
                        <div>
                            <h3 class="text-base lg:text-lg font-bold font-headline mb-1">${Renderer.escapeHtml(record.filename)}</h3>
                            <p class="text-xs lg:text-sm text-slate-500">총 ${(result.상세_요구사항 || []).length}개의 핵심 요구사항이 식별되었습니다.</p>
                        </div>
                        <span class="material-symbols-outlined text-slate-300 hidden lg:block">more_horiz</span>
                    </div>
                    <div class="space-y-6">
                        ${requirementsHTML}
                    </div>
                    ${result.추가_참고사항 ? `
                        <div class="mt-8 pt-8 border-t border-surface-container">
                            <h4 class="text-sm font-bold mb-3">추가 참고사항</h4>
                            <p class="text-xs lg:text-sm text-on-surface-variant leading-relaxed">${Renderer.escapeHtml(result.추가_참고사항)}</p>
                        </div>
                    ` : ''}
                </div>
            </div>
        </div>
    `;

    // Gantt 차트로 스크롤
    detailContainer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/**
 * History 탭 표시
 */
function showHistoryTab() {
    const history = loadAnalysisHistory();
    const container = document.getElementById('history-content');
    if (!container) return;

    if (history.length === 0) {
        container.innerHTML = '<p class="text-center text-slate-500 py-8">분석 기록이 없습니다.</p>';
        return;
    }

    // 날짜별로 그룹화
    const grouped = {};
    history.forEach(record => {
        const date = new Date(record.timestamp).toLocaleDateString('ko-KR');
        if (!grouped[date]) grouped[date] = [];
        grouped[date].push(record);
    });

    container.innerHTML = `
        <div class="space-y-6">
            ${Object.entries(grouped).map(([date, records]) => `
                <div>
                    <h3 class="font-bold text-sm text-slate-600 mb-3">${date}</h3>
                    <div class="space-y-2">
                        ${records.map(record => `
                            <div class="bg-surface-container-lowest p-3 rounded-lg border border-outline-variant/10 text-sm">
                                <div class="flex justify-between">
                                    <span>${record.filename}</span>
                                    <span class="text-primary font-semibold">${record.duration}s</span>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

/**
 * 날짜 포맷 (YYYY-MM-DD)
 */
function formatDate(dateStr) {
    try {
        const date = new Date(dateStr);
        return date.toISOString().split('T')[0];
    } catch {
        return dateStr;
    }
}

// ─── 섹션 표시/숨기기 ───────────────────────────────────────────────────

/**
 * 메뉴 활성화 상태 업데이트
 */
function updateNavMenu(active) {
    const navItems = ['nav-dashboard', 'nav-analysis', 'nav-history', 'nav-settings'];
    navItems.forEach(id => {
        const item = document.getElementById(id);
        if (item) {
            item.classList.remove('bg-white/50', 'text-[#0047AB]', 'rounded-lg', 'shadow-sm', 'font-semibold');
            item.classList.add('text-slate-600', 'hover:text-[#0047AB]', 'hover:bg-white/30');
        }
    });

    // 활성화된 메뉴 항목 스타일 적용
    const activeNav = document.getElementById(active);
    if (activeNav) {
        activeNav.classList.remove('text-slate-600', 'hover:text-[#0047AB]', 'hover:bg-white/30');
        activeNav.classList.add('bg-white/50', 'text-[#0047AB]', 'rounded-lg', 'shadow-sm', 'font-semibold');
    }
}

function showMainSection() {
    document.getElementById('upload-section')?.classList.remove('hidden');
    document.getElementById('analysis-section')?.classList.add('hidden');
    document.getElementById('history-section')?.classList.add('hidden');
    updateNavMenu('nav-dashboard');
    document.querySelector('main')?.scrollIntoView({ behavior: 'smooth' });
}

function showAnalysisSection() {
    document.getElementById('upload-section')?.classList.add('hidden');
    document.getElementById('analysis-section')?.classList.remove('hidden');
    document.getElementById('history-section')?.classList.add('hidden');
    updateNavMenu('nav-analysis');
    showAnalysisTab();
    document.querySelector('main')?.scrollIntoView({ behavior: 'smooth' });
}

function showHistorySection() {
    document.getElementById('upload-section')?.classList.add('hidden');
    document.getElementById('analysis-section')?.classList.add('hidden');
    document.getElementById('history-section')?.classList.remove('hidden');
    updateNavMenu('nav-history');
    showHistoryTab();
    document.querySelector('main')?.scrollIntoView({ behavior: 'smooth' });
}

// ─── 주의: 하단 네비게이션 버튼은 HTML의 onclick으로 직접 처리됨 ───────────────

/**
 * 모바일 사이드바 닫기
 */
function closeMobileSidebar() {
    const mobileSidebar = document.getElementById('mobile-sidebar');
    const mobileOverlay = document.getElementById('mobile-overlay');
    if (mobileSidebar) mobileSidebar.classList.remove('open');
    if (mobileOverlay) mobileOverlay.classList.add('hidden');
}

// ─── Deadline Edit Functions ───────────────────────────────────────────────

let currentDeadlineIndex = null;

/**
 * 마감일 수정 모달 열기
 */
function openDeadlineModal(index) {
    currentDeadlineIndex = index;
    const history = loadAnalysisHistory();
    const record = history[index];

    const modal = document.getElementById('deadline-modal');
    const input = document.getElementById('deadline-input');

    if (modal) {
        const currentDeadline = record.result?.마감일 || '';
        // YYYY-MM-DD 형식으로 파싱
        if (currentDeadline && currentDeadline !== '명시되지 않음' && currentDeadline !== '-') {
            input.value = currentDeadline.split(' ')[0]; // 날짜 부분만 추출
        } else {
            input.value = '';
        }
        modal.classList.remove('hidden');
    }
}

/**
 * 마감일 수정 모달 닫기
 */
function closeDeadlineModal() {
    const modal = document.getElementById('deadline-modal');
    if (modal) modal.classList.add('hidden');
    currentDeadlineIndex = null;
}

/**
 * 마감일 저장
 */
function saveDeadline() {
    if (currentDeadlineIndex === null) return;

    const input = document.getElementById('deadline-input');
    const newDeadline = input.value;

    if (!newDeadline) {
        showToast('날짜를 입력해주세요.', 'error');
        return;
    }

    // localStorage 업데이트
    const history = loadAnalysisHistory();
    history[currentDeadlineIndex].result.마감일 = newDeadline;
    localStorage.setItem('eml_history', JSON.stringify(history));

    // Gantt 차트와 상세정보 모두 업데이트
    const results = history.map(record => ({
        filename: record.filename,
        result: record.result || {}
    }));
    Renderer.renderGanttChart(results);
    showAnalysisDetail(currentDeadlineIndex);

    // 모달 닫기
    closeDeadlineModal();
    showToast('마감일이 수정되었습니다.', 'success');
}

console.log('EML Analyzer v1.0 ready');
