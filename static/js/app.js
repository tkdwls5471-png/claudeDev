/**
 * app.js — 앱 시작, 화면 전환(라우터), 다크모드, 대시보드, AI 검색
 * 화면: #dashboard | #mail?... | #search | #settings
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const SECTIONS = { dashboard: 'dashboard-section', mail: 'mail-section', search: 'ai-search-section', settings: 'settings-section' };
    // 예전 주소 → 새 메일 화면
    const LEGACY = {
        inbox: 'mail?tab=all',
        analysis: 'mail?view=gantt',
        history: 'mail?tab=all&sort=analyzed',
        'ai-search': 'search',
    };

    // ─── 라우터 ──────────────────────────────────────────────────────────────
    const Router = {
        route: null,
        parse() {
            const h = decodeURIComponent(location.hash.replace(/^#/, ''));
            const i = h.indexOf('?');
            return { route: (i < 0 ? h : h.slice(0, i)) || 'dashboard', params: new URLSearchParams(i < 0 ? '' : h.slice(i + 1)) };
        },
        /** hash: 'mail?tab=due' 등. replace면 히스토리를 쌓지 않는다 */
        go(hash, { replace = false, state = null } = {}) {
            const target = '#' + hash.replace(/^#/, '');
            if (target === location.hash) { Router.apply(); return; }
            if (replace) history.replaceState(state, '', target);
            else history.pushState(state, '', target);
            Router.apply();
        },
        apply() {
            let { route, params } = Router.parse();
            if (LEGACY[route]) { Router.go(LEGACY[route], { replace: true }); return; }
            if (!SECTIONS[route]) route = 'dashboard';
            const prev = Router.route;
            Router.route = route;
            if (prev !== route) {
                if (prev === 'mail') Mail.onLeave();
                Object.entries(SECTIONS).forEach(([r, id]) => document.getElementById(id)?.classList.toggle('hidden', r !== route));
                document.body.dataset.route = route;
                activateNav(route);
                window.scrollTo(0, 0);
            }
            if (route === 'mail') Mail.apply(params);
            else if (route === 'dashboard' && prev !== route) Dashboard.load();
            else if (route === 'settings' && prev !== route) Settings.load();
            else if (route === 'search' && prev !== route) document.getElementById('ai-search-input')?.focus();
        },
    };
    window.addEventListener('hashchange', () => Router.apply());

    function activateNav(route) {
        document.querySelectorAll('[data-nav]').forEach(n => {
            const on = n.dataset.nav === route;
            n.classList.toggle('nav-active', on);
            n.setAttribute('aria-current', on ? 'page' : 'false');
        });
    }

    global.navTo = function (route) {
        closeMobileSidebar();
        Router.go(route);
    };

    // ─── 다크모드 / 모바일 메뉴 / 로그아웃 ──────────────────────────────────
    function syncDarkIcon() {
        const icon = document.getElementById('darkmode-icon');
        if (icon) icon.textContent = document.documentElement.classList.contains('dark') ? 'light_mode' : 'dark_mode';
    }
    global.toggleDarkMode = function () {
        const dark = !document.documentElement.classList.contains('dark');
        document.documentElement.classList.toggle('dark', dark);
        document.documentElement.classList.toggle('light', !dark);
        try { localStorage.setItem('eml_dark', dark ? '1' : '0'); } catch (_) {}
        syncDarkIcon();
        if (Router.route === 'dashboard') Dashboard.drawChart();
    };
    function closeMobileSidebar() {
        document.getElementById('mobile-sidebar')?.classList.remove('open');
        document.getElementById('mobile-overlay')?.classList.add('hidden');
    }
    global.closeMobileSidebar = closeMobileSidebar;
    global.doLogout = async function () {
        try { await api('/api/auth/logout', { method: 'POST', silent: true }); } catch (_) {}
        location.href = '/login';
    };

    // ─── 대시보드 ────────────────────────────────────────────────────────────
    const Dashboard = {
        chartData: [],
        async load() {
            try {
                const s = await api('/api/stats', { silent: true });
                document.getElementById('today-count').textContent = s.today_count || 0;
                document.getElementById('today-trend').textContent = `누적 ${s.total_count || 0}건`;
            } catch (_) {}
            Dashboard.renderWork(Status.last);
            Dashboard.loadRecent();
            try { Dashboard.chartData = await api('/api/stats/chart', { silent: true }); } catch (_) { Dashboard.chartData = []; }
            Dashboard.drawChart();
        },

        renderWork(st) {
            if (!st) return;
            const c = st.counts || {};
            document.getElementById('work-todo').textContent = c.todo ?? 0;
            document.getElementById('work-due').textContent = c.due ?? 0;
            document.getElementById('work-failed').textContent = c.failed ?? 0;
        },

        async loadRecent() {
            const box = document.getElementById('dashboard-recent-list');
            try {
                const d = await api('/api/emails', { query: { tab: 'all', per_page: 6 }, silent: true });
                Store.upsertMany(d.emails);
                if (!d.emails.length) { box.innerHTML = '<p class="text-center text-slate-400 text-sm py-8">수신된 메일이 없습니다.</p>'; return; }
                box.innerHTML = d.emails.map(r => `
                  <button type="button" data-id="${r.id}" class="w-full text-left flex items-center gap-3 px-5 py-3 hover:bg-surface-container-low/60 border-b border-outline-variant/10 last:border-0">
                    <span class="unread-dot ${r.is_read ? 'invisible' : ''}"></span>
                    <div class="flex-1 min-w-0">
                      <p class="text-sm truncate ${r.is_read ? 'text-on-surface' : 'font-bold text-on-surface'}">${esc(r.subject || '(제목 없음)')}</p>
                      <p class="text-xs text-slate-400 truncate mt-0.5">${esc(r.company || r.sender || '-')} · ${esc(r.received_at || '')}</p>
                    </div>
                    <div class="shrink-0 flex items-center gap-1">${Badges.deadline(r)}${Badges.analysis(r, { retry: false })}</div>
                  </button>`).join('');
                box.onclick = e => {
                    const b = e.target.closest('[data-id]');
                    if (b) Router.go(`mail?tab=all&id=${Number(b.dataset.id)}`);
                };
            } catch (_) {
                box.innerHTML = '<p class="text-center text-slate-400 text-sm py-8">불러오지 못했습니다.</p>';
            }
        },

        drawChart() {
            const canvas = document.getElementById('dashboard-chart');
            const data = Dashboard.chartData || [];
            if (!canvas || data.length < 2) return;
            const dark = document.documentElement.classList.contains('dark');
            const dpr = window.devicePixelRatio || 1;
            const W = Math.max(200, canvas.parentElement.clientWidth);
            const H = 180;
            canvas.width = W * dpr; canvas.height = H * dpr;
            canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const PAD = { top: 16, right: 16, bottom: 30, left: 36 };
            const cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
            const maxVal = Math.max(...data.map(d => Math.max(d.received, d.analyzed)), 1);
            const yMax = Math.ceil(maxVal * 1.2) || 5;
            const n = data.length;
            const x = i => PAD.left + (i / (n - 1)) * cW;
            const y = v => PAD.top + cH - (v / yMax) * cH;
            const ink = dark ? 'rgba(255,255,255,' : 'rgba(0,0,0,';
            ctx.clearRect(0, 0, W, H);
            ctx.font = '10px Inter,sans-serif';
            for (let i = 0; i <= 4; i++) {
                const gy = PAD.top + (cH / 4) * i;
                ctx.strokeStyle = ink + '0.07)';
                ctx.beginPath(); ctx.moveTo(PAD.left, gy); ctx.lineTo(W - PAD.right, gy); ctx.stroke();
                ctx.fillStyle = ink + '0.4)'; ctx.textAlign = 'right';
                ctx.fillText(String(Math.round(yMax - (yMax / 4) * i)), PAD.left - 6, gy + 3.5);
            }
            ctx.textAlign = 'center';
            const step = Math.max(1, Math.floor(n / 6));
            data.forEach((d, i) => { if (i % step === 0 || i === n - 1) ctx.fillText(d.date.slice(5), x(i), H - PAD.bottom + 16); });
            const line = (series, rgb) => {
                const grad = ctx.createLinearGradient(0, PAD.top, 0, PAD.top + cH);
                grad.addColorStop(0, `rgba(${rgb},0.16)`); grad.addColorStop(1, `rgba(${rgb},0)`);
                ctx.beginPath();
                series.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
                ctx.lineTo(x(n - 1), PAD.top + cH); ctx.lineTo(x(0), PAD.top + cH); ctx.closePath();
                ctx.fillStyle = grad; ctx.fill();
                ctx.beginPath();
                series.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
                ctx.strokeStyle = `rgb(${rgb})`; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
                series.forEach((v, i) => {
                    ctx.beginPath(); ctx.arc(x(i), y(v), 3, 0, Math.PI * 2);
                    ctx.fillStyle = dark ? '#262a30' : '#fff'; ctx.fill(); ctx.stroke();
                });
            };
            line(data.map(d => d.received), '79,134,247');
            line(data.map(d => d.analyzed), '52,201,126');

            const tip = document.getElementById('chart-tooltip');
            const guide = document.getElementById('chart-guide');
            canvas.onmousemove = e => {
                const mx = e.clientX - canvas.getBoundingClientRect().left;
                let best = 0;
                data.forEach((_, i) => { if (Math.abs(x(i) - mx) < Math.abs(x(best) - mx)) best = i; });
                const d = data[best];
                tip.innerHTML = `<p class="font-semibold mb-1 text-slate-200">${esc(d.date)}</p>
                    <p class="flex items-center gap-1.5"><span class="inline-block w-2 h-2 rounded-full bg-[#4f86f7]"></span>수신 <b>${d.received}</b>건</p>
                    <p class="flex items-center gap-1.5 mt-0.5"><span class="inline-block w-2 h-2 rounded-full bg-[#34c97e]"></span>분석 <b>${d.analyzed}</b>건</p>`;
                guide.style.left = `${x(best)}px`;
                guide.classList.remove('hidden');
                tip.classList.remove('hidden');
                let left = x(best) + 12;
                if (left + tip.offsetWidth > W) left = x(best) - tip.offsetWidth - 12;
                tip.style.left = `${Math.max(0, left)}px`;
                tip.style.top = `${Math.max(4, Math.min(y(d.received), y(d.analyzed)) - tip.offsetHeight - 8)}px`;
            };
            canvas.onmouseleave = () => { tip.classList.add('hidden'); guide.classList.add('hidden'); };
        },
    };

    // ─── AI 검색 ─────────────────────────────────────────────────────────────
    global.runAiSearch = async function () {
        const input = document.getElementById('ai-search-input');
        const out = document.getElementById('ai-search-result');
        const btn = document.getElementById('ai-search-btn');
        const query = input.value.trim();
        if (!query) return;
        btn.disabled = true;
        out.innerHTML = '<div class="flex items-center gap-2 text-sm text-slate-400 py-6"><span class="material-symbols-outlined animate-spin text-base">progress_activity</span>검색 중...</div>';
        try {
            const d = await api('/api/search/ask', { method: 'POST', json: { query }, silent: true });
            Store.upsertMany(d.matches);
            const cards = (d.matches || []).map(r => `
              <button type="button" data-id="${r.id}" class="w-full text-left bg-surface-container-lowest p-4 rounded-xl border border-outline-variant/10 hover:bg-surface-container-low">
                <div class="flex items-center justify-between gap-2 mb-1">
                  <span class="text-sm font-semibold text-on-surface truncate">${esc(r.subject || '(제목 없음)')}</span>
                  <span class="text-xs text-slate-400 shrink-0">${esc(r.received_at || '')}</span>
                </div>
                <p class="text-xs text-slate-500 mb-1.5">${esc(r.company || r.sender || '')}</p>
                ${r.summary ? `<p class="text-sm text-slate-600 line-clamp-2">${esc(r.summary)}</p>` : ''}
                <div class="flex flex-wrap items-center gap-1.5 mt-2">
                  ${r.is_completed ? '<span class="badge badge-green">완료</span>' : ''}
                  ${Badges.deadline(r)}${Badges.priority(r.priority)}${Badges.type(r.req_type)}
                </div>
              </button>`).join('');
            out.innerHTML = `
              <div class="bg-primary/5 border border-primary/20 rounded-xl p-4 mb-4 text-sm text-on-surface leading-relaxed whitespace-pre-line">${esc(d.answer || '')}</div>
              ${cards ? `<div class="space-y-2">${cards}</div>` : ''}`;
            out.onclick = e => {
                const b = e.target.closest('[data-id]');
                if (b) Router.go(`mail?tab=all&id=${Number(b.dataset.id)}`);
            };
        } catch (e) {
            out.innerHTML = `<p class="text-sm text-red-500 py-4">검색 실패: ${esc(e.message)}</p>`;
        } finally {
            btn.disabled = false;
        }
    };

    // ─── 예전 브라우저 데이터 정리 (1회) ─────────────────────────────────────
    const LEGACY_KEYS = ['eml_history', 'eml_analysis_history', 'anthropic_api_key', 'eml_today_count',
        'eml_total_time', 'eml_analysis_count', 'eml_filter_start', 'eml_filter_end', 'eml_section', 'inbox_col_order'];

    async function migrateLegacy() {
        let raw = null;
        try {
            if (localStorage.getItem('eml_migrated_v2')) return;
            raw = localStorage.getItem('eml_history');
        } catch (_) { return; }
        try {
            const hist = JSON.parse(raw || '[]');
            const items = (Array.isArray(hist) ? hist : [])
                .filter(r => r && r.result && !r.emailId)
                .map(r => ({
                    filename: r.filename,
                    timestamp: r.timestamp ? DateUtil.toKstString(new Date(r.timestamp)) : '',
                    result: r.result,
                }));
            if (items.length) {
                const d = await api('/api/upload/legacy', { method: 'POST', json: { items }, silent: true });
                if (d.imported) toast(`예전에 업로드해 분석한 기록 ${d.imported}건을 메일함으로 옮겼습니다.`, 'success');
            }
            LEGACY_KEYS.forEach(k => localStorage.removeItem(k));
            localStorage.setItem('eml_migrated_v2', '1');
        } catch (_) { /* 다음 접속 때 다시 시도 */ }
    }

    // ─── 시작 ────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', async () => {
        syncDarkIcon();
        document.getElementById('mobile-menu-btn')?.addEventListener('click', () => {
            document.getElementById('mobile-sidebar')?.classList.add('open');
            document.getElementById('mobile-overlay')?.classList.remove('hidden');
        });
        document.getElementById('mobile-overlay')?.addEventListener('click', closeMobileSidebar);
        try {
            const me = await api('/api/me', { silent: true });
            document.querySelectorAll('[data-ref="username"]').forEach(e => { e.textContent = me.username; });
        } catch (_) { return; }   // 401이면 api()가 로그인 화면으로 보낸다

        Upload.init();
        Mail.init();
        Status.init();
        Bus.on('status', st => { if (Router.route === 'dashboard') Dashboard.renderWork(st); });
        Bus.on('mail-changed', () => { if (Router.route === 'dashboard') Dashboard.loadRecent(); });

        // 예전 버전의 '마지막으로 보던 화면'을 새 주소로 연결
        if (!location.hash) {
            let last = '';
            try { last = localStorage.getItem('eml_section') || ''; } catch (_) {}
            if (LEGACY[last]) history.replaceState(null, '', '#' + LEGACY[last]);
            else if (SECTIONS[last]) history.replaceState(null, '', '#' + last);
        }
        Router.apply();
        migrateLegacy();
        let t = null;
        window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => Router.route === 'dashboard' && Dashboard.drawChart(), 150); });
    });

    global.Router = Router;
    global.Dashboard = Dashboard;
})(window);
