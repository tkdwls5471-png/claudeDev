/**
 * gantt.js — 메일 화면의 '일정' 보기
 * 막대 = 수신일 → 마감일 (마감 없으면 7일짜리 빗금 막대 '마감 미정'). 오늘선 표시,
 * 마감 지난 미완료는 빨간 테두리, 완료는 회색. 클릭하면 같은 상세 패널이 열린다.
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;
    const DAY = 86400000;
    const COLOR = { '높음': 'gantt-bar-high', '보통': 'gantt-bar-mid', '낮음': 'gantt-bar-low' };

    function render(container, rows, { from, to, cursorId } = {}) {
        if (!rows.length) {
            container.innerHTML = '<p class="text-center text-slate-400 text-sm py-16">이 기간에 표시할 메일이 없습니다.</p>';
            return;
        }
        const t0 = DateUtil.ymdToMs(from);
        const t1 = DateUtil.ymdToMs(to) + DAY;
        const span = Math.max(DAY, t1 - t0);
        const days = Math.round(span / DAY);
        const pct = ms => Math.min(100, Math.max(0, ((ms - t0) / span) * 100));
        const step = Math.max(1, Math.round(days / 12));
        const today = DateUtil.ymdToMs(DateUtil.todayStr());

        let ticks = '';
        for (let i = 0; i <= days; i += step) {
            const ms = t0 + i * DAY;
            const d = new Date(ms);
            const p = (i / days) * 100;
            const pos = p <= 1 ? 'left:0' : p >= 99 ? 'right:0' : `left:${p.toFixed(2)}%;transform:translateX(-50%)`;
            ticks += `<span class="absolute top-0 text-[10px] text-slate-500 whitespace-nowrap" style="${pos}">${d.getUTCMonth() + 1}/${d.getUTCDate()}</span>`;
        }
        const todayLine = today >= t0 && today < t1
            ? `<div class="gantt-today" style="left:${pct(today + DAY / 2).toFixed(2)}%"></div>` : '';
        const grid = `background-image:repeating-linear-gradient(to right, rgba(100,116,139,.12) 0 1px, transparent 1px ${((step / days) * 100).toFixed(3)}%)`;

        const body = rows.map(r => {
            const start = DateUtil.ymdToMs((r.received_at || '').slice(0, 10)) || t0;
            const hasDl = !!r.deadline;
            const end = hasDl ? DateUtil.ymdToMs(r.deadline) + DAY : start + 7 * DAY;
            const left = pct(start);
            const width = Math.max(1.5, pct(Math.max(end, start + DAY)) - left);
            const closed = r.is_completed || r.is_excluded;
            const overdue = hasDl && !closed && DateUtil.daysUntil(r.deadline) < 0;
            const cls = closed ? 'gantt-bar-done' : (COLOR[r.priority] || 'gantt-bar-none');
            const label = r.is_completed ? '완료' : r.is_excluded ? '제외' : hasDl ? `${r.deadline.slice(5)} ${DateUtil.ddayLabel(r.deadline)}` : '마감 미정';
            return `
              <div class="gantt-row ${r.id === cursorId ? 'is-cursor' : ''} ${closed ? 'opacity-60' : ''}" data-id="${r.id}">
                <div class="gantt-label ${r.is_read ? '' : 'font-bold'}" title="${esc(r.subject)}">
                  ${r.is_completed ? '<span class="material-symbols-outlined text-sm text-green-500">check_circle</span>' : ''}
                  <span class="truncate ${closed ? 'line-through' : ''}">${esc(r.subject || '(제목 없음)')}</span>
                </div>
                <div class="gantt-track" style="${grid}">
                  ${todayLine}
                  <div class="gantt-bar ${cls} ${hasDl ? '' : 'gantt-bar-open'} ${overdue ? 'gantt-bar-overdue' : ''}"
                       style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%" title="${esc(`${r.received_at} → ${r.deadline || '마감 미정'}`)}">
                    <span class="truncate">${esc(label)}</span>
                  </div>
                </div>
              </div>`;
        }).join('');

        container.innerHTML = `
          <div class="gantt">
            <div class="gantt-row gantt-head bg-surface-container-lowest">
              <div class="gantt-label text-xs font-bold text-slate-500">제목</div>
              <div class="gantt-track !bg-transparent !border-0 h-5">${ticks}</div>
            </div>
            ${body}
          </div>`;
    }

    global.Gantt = { render };
})(window);
