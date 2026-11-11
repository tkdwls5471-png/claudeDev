/**
 * app.js — 앱 시작, 화면 전환(라우터), 다크모드, 대시보드
 * 화면: #dashboard | #mail?... | #settings?tab=...
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const SECTIONS = {
        dashboard: { id: 'dashboard-section', title: '대시보드' },
        mail: { id: 'mail-section', title: '메일' },
        settings: { id: 'settings-section', title: '설정' },
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
            if (!SECTIONS[route]) route = 'dashboard';
            const prev = Router.route;
            Router.route = route;
            if (prev !== route) {
                if (prev === 'mail') Mail.onLeave();
                Object.entries(SECTIONS).forEach(([r, s]) => document.getElementById(s.id)?.classList.toggle('hidden', r !== route));
                document.body.dataset.route = route;
                document.getElementById('page-title').textContent = SECTIONS[route].title;
                document.title = `${SECTIONS[route].title} · EML Analyzer`;
                activateNav(route);
                window.scrollTo(0, 0);
            }
            if (route === 'mail') Mail.apply(params);
            else if (route === 'settings') Settings.apply(params, prev !== route);
            else if (route === 'dashboard' && prev !== route) Dashboard.load();
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

    global.navTo = route => Router.go(route);

    // ─── 다크모드 / 로그아웃 ────────────────────────────────────────────────
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
    };
    global.doLogout = async function () {
        try { await api('/api/auth/logout', { method: 'POST', silent: true }); } catch (_) {}
        location.href = '/login';
    };

    // ─── 대시보드 ────────────────────────────────────────────────────────────
    const Dashboard = {
        load() {
            Dashboard.renderWork(Status.last);
            Dashboard.loadRecent();
        },

        renderWork(st) {
            if (!st) return;
            const c = st.counts || {};
            document.getElementById('work-todo').textContent = c.todo ?? 0;
            document.getElementById('work-due').textContent = c.due ?? 0;
            document.getElementById('work-failed').textContent = c.failed ?? 0;
            document.getElementById('today-count').textContent = st.analysis?.today ?? 0;
        },

        async loadRecent() {
            const box = document.getElementById('dashboard-recent-list');
            try {
                const d = await api('/api/emails', { query: { tab: 'all', per_page: 8 }, silent: true });
                Store.upsertMany(d.emails);
                if (!d.emails.length) { box.innerHTML = '<p class="text-center text-slate-400 text-sm py-8">수신된 메일이 없습니다.</p>'; return; }
                box.innerHTML = d.emails.map(r => `
                  <button type="button" data-id="${r.id}" class="w-full text-left flex items-center gap-3 px-5 py-3 hover:bg-surface-container-low/60 border-b border-outline-variant/10 last:border-0">
                    <span class="unread-dot ${r.is_read ? 'invisible' : ''}"></span>
                    <div class="flex-1 min-w-0">
                      <p class="text-sm truncate ${r.is_read ? 'text-on-surface' : 'font-bold text-on-surface'}">${esc(r.subject || '(제목 없음)')}</p>
                      <p class="text-xs text-slate-400 truncate mt-0.5">${esc(r.company || r.sender || '-')} · ${esc(DateUtil.shortDate(r.received_at))}</p>
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
    };

    // ─── 시작 ────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', async () => {
        syncDarkIcon();
        try {
            const me = await api('/api/me', { silent: true });
            document.querySelectorAll('[data-ref="username"]').forEach(e => { e.textContent = me.username; });
        } catch (_) { return; }   // 401이면 api()가 로그인 화면으로 보낸다

        Upload.init();
        Mail.init();
        Status.init();
        Bus.on('status', st => { if (Router.route === 'dashboard') Dashboard.renderWork(st); });
        Bus.on('mail-changed', () => { if (Router.route === 'dashboard') Dashboard.loadRecent(); });
        Router.apply();
    });

    global.Router = Router;
    global.Dashboard = Dashboard;
})(window);
