/**
 * Result Renderer
 * 분석 결과를 DOM으로 변환하여 표시
 */
const Renderer = {

    /**
     * 요약 테이블에 행 추가
     */
    addTableRow(index, filename, result) {
        console.log('addTableRow called:', {index, filename, result});

        const tbody = document.getElementById('results-table-body');
        console.log('tbody element:', tbody);
        if (!tbody) {
            console.error('results-table-body를 찾을 수 없습니다!');
            return;
        }

        const priorityClass = this.getPriorityClass(result.우선순위);
        const typeClass = this.getTypeClass(result.요청_유형);
        console.log('CSS classes:', {priorityClass, typeClass});

        const row = document.createElement('tr');
        row.className = 'hover:bg-surface-container-low transition-colors group cursor-pointer table-row-hover';
        row.dataset.index = index;
        row.innerHTML = `
            <td class="px-4 lg:px-6 py-3 lg:py-4 text-xs lg:text-sm font-medium text-primary">${this.escapeHtml(filename)}</td>
            <td class="px-4 lg:px-6 py-3 lg:py-4 text-xs lg:text-sm">${this.escapeHtml(result.요청_업체 || '-')}</td>
            <td class="px-4 lg:px-6 py-3 lg:py-4 text-xs lg:text-sm text-slate-500">${this.escapeHtml(result.수신일 || '-')}</td>
            <td class="px-4 lg:px-6 py-3 lg:py-4 text-xs lg:text-sm text-slate-500">${this.escapeHtml(result.마감일 || '-')}</td>
            <td class="px-4 lg:px-6 py-3 lg:py-4">
                <span class="px-2 lg:px-3 py-1 text-xs font-semibold rounded-full ${typeClass}">
                    ${this.escapeHtml(result.요청_유형 || '기타')}
                </span>
            </td>
            <td class="px-4 lg:px-6 py-3 lg:py-4">
                <span class="px-2 lg:px-3 py-1 text-xs font-semibold rounded-full ${priorityClass}">
                    ${this.escapeHtml(result.우선순위 || '보통')}
                </span>
            </td>
        `;

        row.addEventListener('click', () => {
            this.showDetail(index);
        });

        tbody.appendChild(row);
    },

    /**
     * 우선순위별 배지 클래스 반환
     */
    getPriorityClass(priority) {
        switch (priority) {
            case '높음':
                return 'bg-tertiary-container text-on-tertiary-container';
            case '보통':
                return 'bg-secondary-container text-on-secondary-container';
            case '낮음':
                return 'bg-surface-container-high text-on-surface-variant';
            default:
                return 'bg-surface-container-high text-on-surface-variant';
        }
    },

    /**
     * 요청 유형별 배지 클래스 반환
     */
    getTypeClass(type) {
        return 'bg-secondary-container text-on-secondary-container';
    },

    /**
     * 우선순위별 border 색상 반환
     */
    getBorderColor(priority) {
        switch (priority) {
            case '높음':
                return 'border-error';
            case '보통':
                return 'border-secondary';
            case '낮음':
                return 'border-outline-variant';
            default:
                return 'border-outline-variant';
        }
    },

    /**
     * 우선순위별 번호 박스 색상 반환
     */
    getNumberBoxClass(priority) {
        switch (priority) {
            case '높음':
                return 'bg-error-container text-on-error-container';
            case '보통':
                return 'bg-secondary-container text-on-secondary-container';
            case '낮음':
                return 'bg-surface-container-high text-on-surface-variant';
            default:
                return 'bg-surface-container-high text-on-surface-variant';
        }
    },

    /**
     * 상세 내용 표시
     */
    showDetail(index) {
        const result = window.AppState.analysisResults[index];
        if (!result) return;

        window.AppState.selectedIndex = index;
        const { filename, result: data } = result;

        // 메타데이터 업데이트
        document.getElementById('detail-received-date').textContent = data.수신일 || '-';
        document.getElementById('detail-requester').textContent = data.요청_업체 || '-';
        document.getElementById('detail-deadline').textContent = data.마감일 || '-';
        document.getElementById('detail-type').textContent = data.요청_유형 || '-';

        // AI 요약
        document.getElementById('detail-ai-summary').textContent = data.요구사항_요약 || '-';

        // 파일명 및 요구사항 개수
        document.getElementById('detail-filename').textContent = filename;
        const reqCount = data.상세_요구사항 ? data.상세_요구사항.length : 0;
        document.getElementById('detail-req-count').textContent = `총 ${reqCount}개의 핵심 요구사항이 식별되었습니다.`;

        // 상세 요구사항 카드 렌더링
        this.renderRequirementCards(data.상세_요구사항 || [], data.우선순위);

        // 테이블 행 선택 표시
        document.querySelectorAll('tbody tr').forEach((row, i) => {
            row.classList.remove('table-row-selected', 'bg-surface-container-low/20');
            if (i === index) {
                row.classList.add('table-row-selected', 'bg-surface-container-low/20');
            }
        });

        // 섹션 표시
        document.getElementById('detail-section').classList.remove('hidden');
        document.getElementById('detail-section').scrollIntoView({ behavior: 'smooth' });
    },

    /**
     * 상세 요구사항 카드 렌더링
     */
    renderRequirementCards(requirements, priority) {
        const container = document.getElementById('detail-requirements');
        if (!container) return;

        container.innerHTML = requirements.map((item, idx) => {
            const borderColor = this.getBorderColor(priority);
            const numBoxColor = this.getNumberBoxClass(priority);

            return `
                <div class="group p-4 lg:p-6 rounded-lg bg-surface hover:bg-surface-container-low transition-all border-l-4 ${borderColor}">
                    <div class="flex gap-3 lg:gap-4">
                        <span class="flex-shrink-0 w-7 lg:w-8 h-7 lg:h-8 rounded-full ${numBoxColor} flex items-center justify-center font-black text-xs lg:text-sm">
                            ${String(idx + 1).padStart(2, '0')}
                        </span>
                        <p class="text-xs lg:text-sm text-on-surface-variant leading-relaxed">${this.escapeHtml(item)}</p>
                    </div>
                </div>
            `;
        }).join('');
    },

    /**
     * HTML 이스케이프 (XSS 방지)
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    /**
     * 테이블 초기화
     */
    clearTable() {
        const tbody = document.getElementById('results-table-body');
        if (tbody) tbody.innerHTML = '';
    },

    /**
     * 섹션 보이기/숨기기
     */
    showSection(sectionName) {
        const sections = {
            'upload': document.getElementById('upload-section'),
            'progress': document.getElementById('progress-section'),
            'results': document.getElementById('results-section'),
            'detail': document.getElementById('detail-section')
        };

        // upload, progress, results는 동시에 보일 수 있음
        if (sectionName === 'results' || sectionName === 'progress') {
            if (sectionName === 'results') {
                sections['progress']?.classList.add('hidden');
            } else if (sectionName === 'progress') {
                sections['progress']?.classList.remove('hidden');
            }
            sections['results']?.classList.remove('hidden');
        }

        // detail은 results 아래에 표시
        if (sectionName === 'detail') {
            sections['detail']?.classList.remove('hidden');
        }
    },

    hideSection(sectionName) {
        const sections = {
            'upload': document.getElementById('upload-section'),
            'progress': document.getElementById('progress-section'),
            'results': document.getElementById('results-section'),
            'detail': document.getElementById('detail-section')
        };

        if (sections[sectionName]) {
            sections[sectionName].classList.add('hidden');
        }
    },

    /**
     * Gantt 차트로 분석 일정 표시
     */
    renderGanttChart(results) {
        // 데이터 정렬 (수신일 기준)
        const sorted = [...results].sort((a, b) => {
            const dateA = new Date(a.result.수신일 || '2099-12-31');
            const dateB = new Date(b.result.수신일 || '2099-12-31');
            return dateA - dateB;
        });

        if (sorted.length === 0) return;

        // 최소/최대 날짜 계산
        const dates = sorted.map(r => new Date(r.result.수신일 || '2099-12-31'))
            .concat(sorted.map(r => this.parseDate(r.result.마감일) || new Date('2099-12-31')));
        const minDate = new Date(Math.min(...dates));
        const maxDate = new Date(Math.max(...dates));

        // 날짜 범위 (최소 60일)
        const daysRange = Math.max(60, Math.ceil((maxDate - minDate) / (1000 * 60 * 60 * 24)));

        let ganttHTML = `
            <div class="space-y-4">
                <div class="overflow-x-auto">
                    <div class="min-w-max">
                        <!-- 타임라인 헤더 -->
                        <div class="flex mb-4">
                            <div class="w-48 flex-shrink-0 font-bold text-sm px-4">제목</div>
                            <div class="flex-1 ml-4 flex text-xs text-slate-500 border-l border-slate-300 pl-4">`;

        // 날짜 헤더 (7일 단위)
        for (let i = 0; i <= daysRange; i += 7) {
            const date = new Date(minDate);
            date.setDate(date.getDate() + i);
            ganttHTML += `<div class="w-16 text-center text-[10px]">${date.toLocaleDateString('ko-KR', {month: 'short', day: 'numeric'})}</div>`;
        }

        ganttHTML += `</div></div><!-- 각 항목 -->`;

        // 각 항목 렌더링
        sorted.forEach((record, idx) => {
            const startDate = new Date(record.result.수신일 || minDate);
            const endDateStr = record.result.마감일;
            const endDate = endDateStr && endDateStr !== '명시되지 않음' ? new Date(endDateStr) : null;

            const startOffset = Math.max(0, (startDate - minDate) / (1000 * 60 * 60 * 24));
            const duration = endDate ? Math.max(1, (endDate - startDate) / (1000 * 60 * 60 * 24)) : 7;
            const barWidth = (duration / daysRange) * 100;
            const barLeft = (startOffset / daysRange) * 100;

            const priorityColor = {
                '높음': 'bg-red-500',
                '보통': 'bg-yellow-500',
                '낮음': 'bg-green-500'
            }[record.result.우선순위] || 'bg-blue-500';

            ganttHTML += `
                <div class="flex items-center mb-2 cursor-pointer hover:bg-blue-50 p-2 rounded transition-all group" onclick="showAnalysisDetail(${idx})">
                    <div class="w-48 flex-shrink-0 text-xs font-semibold text-slate-700 truncate px-4" title="${record.filename}">
                        ${record.filename.substring(0, 30)}
                    </div>
                    <div class="flex-1 ml-4 relative h-6 bg-slate-100 rounded border border-slate-200" style="min-width: ${daysRange * 16}px;">
                        <div class="${priorityColor} h-full rounded opacity-75 group-hover:opacity-100 transition-all"
                             style="width: ${barWidth}%; margin-left: ${barLeft}%; min-width: 40px;">
                            <div class="text-[10px] text-white font-semibold h-full flex items-center px-1 truncate">
                                ${endDateStr ? endDateStr : '진행중'}
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });

        ganttHTML += `</div></div>`;

        const container = document.getElementById('analysis-gantt');
        if (container) {
            container.innerHTML = ganttHTML;
        }
    },

    /**
     * 날짜 파싱
     */
    parseDate(dateStr) {
        if (!dateStr || dateStr === '명시되지 않음') return null;
        const parsed = new Date(dateStr);
        return isNaN(parsed.getTime()) ? null : parsed;
    }
};
