/**
 * status.js — 헤더의 동기화·분석 현황 표시와 펼침 패널
 *  - 평소: "2분 전 동기화 · 대기 3 · 실패 1"
 *  - 펼치면: 계정별 동기화 결과/오류/신규, 분석 대기열(지금 분석 중인 메일), 실패 목록 + 재시도, [지금 동기화]
 *  - /api/status를 평소 10초, 동기화·분석 중 2초 간격으로 확인 (탭이 숨겨지면 멈춤)
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;
    const FAST = 2000, IDLE = 10000;

    let timer = null;
    let last = null;
    let fastUntil = 0;
    let el = {};

    function init() {
        el = {
            btn: document.getElementById('status-btn'),
            icon: document.getElementById('status-icon'),
            text: document.getElementById('status-text'),
            dot: document.getElementById('status-dot'),
            pop: document.getElementById('status-popover'),
            navBadge: document.querySelectorAll('[data-ref="nav-mail-count"]'),
        };
        el.btn.addEventListener('click', e => { e.stopPropagation(); togglePopover(); });
        el.pop.addEventListener('click', onPopoverClick);
        document.addEventListener('click', e => {
            if (!el.pop.classList.contains('hidden') && !e.target.closest('#status-popover')) togglePopover(false);
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape' && !el.pop.classList.contains('hidden')) { togglePopover(false); e.preventDefault(); e.stopImmediatePropagation(); }
        }, true);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
        Bus.on('queue-changed', kick);
        tick();
    }

    /** 방금 작업을 시작했으니 잠시 빠르게 확인 */
    function kick() {
        fastUntil = Date.now() + 20000;
        tick();
    }

    async function tick() {
        clearTimeout(timer);
        if (document.hidden) return;
        try {
            const st = await api('/api/status', { silent: true });
            last = st;
            render(st);
            Bus.emit('status', st);
            if (global.Mail) Mail.onStatus(st);
        } catch (_) {
            el.text.textContent = '연결 끊김';
            el.dot.className = 'status-dot status-dot-error';
        }
        const busy = last && (last.sync.running || last.analysis.queued > 0 || last.analysis.running_count > 0);
        timer = setTimeout(tick, busy || Date.now() < fastUntil ? FAST : IDLE);
    }

    function lastSyncAt(st) {
        return st.sync.accounts.map(a => a.last_sync_at).filter(Boolean).sort().pop() || null;
    }

    function render(st) {
        const a = st.analysis;
        const errors = st.sync.accounts.filter(x => x.status === 'error').length;
        const parts = [];
        if (st.sync.running) parts.push('동기화 중');
        else if (st.sync.accounts.length) {
            const at = lastSyncAt(st);
            parts.push(at ? `${DateUtil.relativeTime(at)} 동기화` : '동기화 전');
        } else parts.push('메일 계정 없음');
        if (a.running) parts.push('분석 중');
        if (a.queued) parts.push(`대기 ${a.queued}`);
        if (a.failed) parts.push(`실패 ${a.failed}`);
        el.text.textContent = parts.join(' · ');
        el.icon.classList.toggle('animate-spin', !!st.sync.running);
        const bad = errors || a.paused;
        el.dot.className = 'status-dot ' + (bad ? 'status-dot-error' : (a.running || a.queued || st.sync.running) ? 'status-dot-busy' : 'status-dot-ok');
        el.btn.title = bad ? '동기화 또는 분석에 문제가 있습니다. 눌러서 확인하세요.' : '동기화·분석 현황';
        el.navBadge.forEach(b => {
            const n = st.counts?.todo || 0;
            b.textContent = n > 99 ? '99+' : String(n);
            b.classList.toggle('hidden', !n);
        });
        if (!el.pop.classList.contains('hidden')) renderPopover(st);
    }

    function togglePopover(force) {
        const open = force ?? el.pop.classList.contains('hidden');
        el.pop.classList.toggle('hidden', !open);
        if (open && last) { renderPopover(last); loadFailed(); }
    }

    function renderPopover(st) {
        const a = st.analysis;
        const accounts = st.sync.accounts.length ? st.sync.accounts.map(acc => `
            <li class="flex items-start gap-2 py-1.5">
              <span class="status-dot mt-1.5 ${acc.status === 'error' ? 'status-dot-error' : acc.status === 'ok' ? 'status-dot-ok' : 'status-dot-idle'}"></span>
              <div class="min-w-0 flex-1">
                <p class="text-sm font-medium text-on-surface truncate">${esc(acc.label || acc.email_address)}</p>
                <p class="text-xs text-slate-500">${acc.last_sync_at ? `${esc(DateUtil.relativeTime(acc.last_sync_at))} (${esc(acc.last_sync_at.slice(5))})` : '아직 동기화 전'}
                  ${acc.status === 'ok' && acc.new_count ? ` · 신규 ${acc.new_count}건` : ''}</p>
                ${acc.status === 'error' && acc.error ? `<p class="text-xs text-red-600 mt-0.5 break-words">${esc(acc.error)}</p>` : ''}
              </div>
            </li>`).join('')
            : '<li class="text-xs text-slate-500 py-1.5">연결된 메일 계정이 없습니다. <button type="button" data-action="settings" class="underline">설정에서 추가</button></li>';
        el.pop.innerHTML = `
          <div class="p-4 space-y-4">
            <section>
              <div class="flex items-center justify-between mb-1">
                <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider">메일 동기화 <span class="normal-case font-normal">(${st.sync.interval_min}분마다)</span></h4>
                <button type="button" data-action="sync" ${st.sync.running ? 'disabled' : ''}
                  class="flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-lg bg-primary text-on-primary disabled:opacity-50">
                  <span class="material-symbols-outlined text-sm ${st.sync.running ? 'animate-spin' : ''}">sync</span>${st.sync.running ? '동기화 중' : '지금 동기화'}</button>
              </div>
              <ul>${accounts}</ul>
            </section>
            <section class="border-t border-outline-variant/10 pt-3">
              <div class="flex items-center justify-between mb-1">
                <h4 class="text-xs font-bold text-slate-400 uppercase tracking-wider">AI 분석</h4>
                <span class="text-xs ${a.auto_enabled ? 'text-green-600' : 'text-slate-400'}">자동 분석 ${a.auto_enabled ? '켜짐' : '꺼짐'}</span>
              </div>
              ${a.paused ? `<p class="text-xs text-red-600 bg-red-50 rounded-lg px-2.5 py-1.5 mb-2 break-words">
                  분석을 잠시 멈췄습니다 (${esc(a.paused.until?.slice(11) || '')}까지): ${esc(a.paused.reason)}
                  ${String(a.paused.reason).startsWith('[설정 오류]') ? '<button type="button" data-action="settings" class="underline font-semibold">AI 설정 확인</button>' : ''}</p>` : ''}
              ${a.running ? `<button type="button" data-action="open" data-id="${a.running.id}" class="w-full text-left flex items-center gap-2 text-sm py-1 hover:text-primary">
                  <span class="analyze-spinner"></span><span class="truncate">${esc(a.running.subject || '(제목 없음)')}</span></button>` : ''}
              <div class="flex items-center gap-3 text-xs text-slate-500 mt-1">
                <span>대기 <b class="text-on-surface">${a.queued}</b>건</span>
                <span>실패 <b class="${a.failed ? 'text-red-600' : 'text-on-surface'}">${a.failed}</b>건</span>
                ${a.queued ? '<button type="button" data-action="goto-queue" class="ml-auto underline">대기 목록</button>' : ''}
              </div>
              <ul data-ref="failed" class="mt-2 space-y-1"></ul>
            </section>
          </div>`;
        if (lastFailed) renderFailed(lastFailed);
    }

    let lastFailed = null;
    async function loadFailed() {
        if (!last?.analysis.failed) { lastFailed = []; renderFailed([]); return; }
        try {
            const d = await api('/api/emails', { query: { tab: 'failed', per_page: 5, sort: 'received' }, silent: true });
            lastFailed = d.emails || [];
            Store.upsertMany(lastFailed);
            renderFailed(lastFailed);
        } catch (_) {}
    }

    function renderFailed(rows) {
        const ul = el.pop.querySelector('[data-ref="failed"]');
        if (!ul) return;
        ul.innerHTML = rows.map(r => `
            <li class="flex items-start gap-2 rounded-lg bg-red-50/60 px-2.5 py-1.5">
              <button type="button" data-action="open" data-id="${r.id}" class="min-w-0 flex-1 text-left">
                <p class="text-xs font-medium text-on-surface truncate">${esc(r.subject || '(제목 없음)')}</p>
                <p class="text-[11px] text-red-600 truncate" title="${esc(r.analysis_error)}">${esc(r.analysis_error || '')}</p>
              </button>
              <button type="button" data-action="retry" data-id="${r.id}" class="shrink-0 text-xs font-semibold text-primary underline">재시도</button>
            </li>`).join('')
            + (last?.analysis.failed > rows.length ? `<li><button type="button" data-action="goto-failed" class="text-xs underline text-slate-500">실패 ${last.analysis.failed}건 모두 보기</button></li>` : '');
    }

    async function onPopoverClick(e) {
        const t = e.target.closest('[data-action]');
        if (!t) return;
        const a = t.dataset.action;
        if (a === 'sync') {
            try { await api('/api/sync', { method: 'POST' }); kick(); toast('동기화를 시작했습니다.', 'success'); } catch (_) {}
        } else if (a === 'open') {
            togglePopover(false);
            Router.go(`mail?tab=all&id=${Number(t.dataset.id)}`);
        } else if (a === 'retry') {
            await MailActions.analyze(Number(t.dataset.id));
            loadFailed();
        } else if (a === 'goto-failed') {
            togglePopover(false);
            Router.go('mail?tab=failed');
        } else if (a === 'goto-queue') {
            togglePopover(false);
            Router.go('mail?tab=all&sort=received');
        } else if (a === 'settings') {
            togglePopover(false);
            Router.go('settings');
        }
    }

    global.Status = { init, kick, tick, get last() { return last; } };
})(window);
