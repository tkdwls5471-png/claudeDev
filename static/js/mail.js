/**
 * mail.js — '메일' 화면
 *  - 상태 탭: 할 일 / 마감임박 / 완료 / 전체 (+ 제외·분석실패는 작게)
 *  - 검색 + 필터 패널(계정·안읽음·우선순위·유형·정렬) 하나로 정리
 *  - 행에서 바로 완료, 여러 건 선택 후 일괄 처리
 *  - 오른쪽 상세 패널 + 키보드(j/k ↑/↓ 이동, Esc, e 완료, x 제외, d 마감, u 읽음, / 검색)
 *  - 화면 상태는 주소(#mail?...)에 담겨 새로고침·뒤로가기에도 유지
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const MAIN_TABS = [['todo', '할 일'], ['due', '마감임박'], ['done', '완료'], ['all', '전체']];
    const SUB_TABS = [['excluded', '제외'], ['failed', '분석실패']];
    const TABS = [...MAIN_TABS, ...SUB_TABS];
    const SORTS = [['received', '수신일순'], ['deadline', '마감일순'], ['priority', '우선순위순'], ['analyzed', '분석일순']];
    const PRIORITIES = ['높음', '보통', '낮음'];
    const TYPES = ['검토요청', '보고서/자료 요청', '수정/작업요청', '견적요청', '작업완료보고', '회의요청', '기타'];
    const PER_PAGE = 50;
    const PREF_KEY = 'eml_mail_prefs';
    const PREF_FIELDS = ['tab', 'sort', 'acct', 'prio', 'type', 'unread'];
    const PRIO_DOT = { '높음': 'prio-dot-high', '보통': 'prio-dot-mid', '낮음': 'prio-dot-low' };

    const S = {                 // 화면 상태 (주소에 반영되는 값)
        tab: 'todo', sort: '', q: '', acct: '', prio: [], type: [], unread: false, page: 1, id: null,
    };
    let ids = [];               // 지금 보이는 순서
    let total = 0;
    let counts = {};
    let listKey = null;
    let loadCtl = null;
    const selected = new Set();
    let cursorId = null;
    let base = null;            // 새 메일 배너 기준 { latest, all }
    let lastQueueSig = '';
    let accounts = null;        // [{id, label}]
    let el = {};

    // ─── 초기화 ──────────────────────────────────────────────────────────────
    function init() {
        const $ = id => document.getElementById(id);
        el = {
            tabs: $('mail-tabs'), search: $('mail-search'), searchClear: $('mail-search-clear'),
            filterBtn: $('mail-filter-btn'), filterPanel: $('mail-filter-panel'), filterSummary: $('mail-filter-summary'),
            banner: $('mail-banner'), bulk: $('mail-bulk'), listWrap: $('mail-list-wrap'), table: $('mail-table'),
            total: $('mail-total'), pager: $('mail-pager'), panel: $('mail-panel'),
        };

        el.tabs.addEventListener('click', e => {
            const b = e.target.closest('[data-tab]');
            if (b) navigate({ tab: b.dataset.tab, page: 1, sort: '' });
        });
        let qTimer = null;
        el.search.addEventListener('input', () => {
            el.searchClear.classList.toggle('hidden', !el.search.value);
            clearTimeout(qTimer);
            qTimer = setTimeout(() => navigate({ q: el.search.value.trim(), page: 1 }, { replace: true }), 350);
        });
        el.search.addEventListener('keydown', e => {
            if (e.key === 'Enter') { clearTimeout(qTimer); navigate({ q: el.search.value.trim(), page: 1 }, { replace: true }); }
        });
        el.searchClear.addEventListener('click', () => { el.search.value = ''; navigate({ q: '', page: 1 }, { replace: true }); el.search.focus(); });

        el.filterBtn.addEventListener('click', e => { e.stopPropagation(); toggleFilterPanel(); });
        el.filterPanel.addEventListener('click', onFilterClick);
        el.filterPanel.addEventListener('change', onFilterChange);
        el.filterSummary.addEventListener('click', e => {
            const c = e.target.closest('[data-clear]');
            if (!c) return;
            const [dim, val] = [c.dataset.clear, c.dataset.val];
            const patch = dim === 'prio' || dim === 'type' ? { [dim]: S[dim].filter(x => x !== val) }
                : dim === 'acct' ? { acct: '' } : dim === 'unread' ? { unread: false }
                : { prio: [], type: [], acct: '', unread: false };
            navigate({ ...patch, page: 1 }, { replace: true });
        });
        document.addEventListener('click', e => {
            if (!el.filterPanel.classList.contains('hidden') && !e.target.closest('#mail-filter-control')) toggleFilterPanel(false);
            if (!e.target.closest('[data-bulk="more"],[data-ref="bulk-menu"]')) el.bulk.querySelector('[data-ref="bulk-menu"]')?.classList.add('hidden');
        });
        el.banner.addEventListener('click', e => {
            if (e.target.closest('[data-action="reload"]')) { hideBanner(); load(); }
        });

        el.listWrap.addEventListener('click', onListClick);
        el.listWrap.addEventListener('change', onListChange);
        el.bulk.addEventListener('click', onBulkClick);

        Panel.mount(el.panel, {
            onClose: () => navigate({ id: null }, { replace: !isMobile(), back: isMobile() }),
            onPrev: () => move(-1),
            onNext: () => move(1),
            navInfo: id => { const i = ids.indexOf(id); return { hasPrev: i > 0, hasNext: i >= 0 && i < ids.length - 1 }; },
        });

        Bus.on('row', onRowChanged);
        Bus.on('row-removed', id => {
            selected.delete(id);
            el.table.querySelector(`tr[data-id="${id}"]`)?.remove();
            ids = ids.filter(x => x !== id);
            if (S.id === id) navigate({ id: null }, { replace: true });
            renderBulk();
        });
        Bus.on('mail-changed', d => {
            if (d && d.bulk) { selected.clear(); load({ keepScroll: true }); }
            else refreshCounts();
        });
        document.addEventListener('keydown', onKey);
    }

    function isMobile() { return window.matchMedia('(max-width: 1023px)').matches; }

    // ─── 주소 ↔ 상태 ─────────────────────────────────────────────────────────
    function loadPrefs() {
        try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (_) { return {}; }
    }
    function savePrefs() {
        const p = {};
        PREF_FIELDS.forEach(k => { p[k] = S[k]; });
        try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch (_) {}
    }

    function fromParams(params) {
        const has = [...params.keys()].some(k => k !== 'id');
        const src = has ? null : loadPrefs();       // 주소에 조건이 없으면 마지막으로 보던 조건
        const get = k => (src ? src[k] : params.get(k));
        const getAll = k => (src ? (src[k] || []) : params.getAll(k));
        const tab = get('tab');
        return {
            tab: TABS.some(([v]) => v === tab) ? tab : 'todo',
            sort: SORTS.some(([v]) => v === get('sort')) ? get('sort') : '',
            q: src ? '' : (params.get('q') || ''),
            acct: String(get('acct') || ''),
            prio: getAll('prio').filter(p => PRIORITIES.includes(p)),
            type: getAll('type').filter(t => TYPES.includes(t)),
            unread: src ? !!src.unread : params.get('unread') === '1',
            page: src ? 1 : Math.max(1, parseInt(params.get('page') || '1', 10) || 1),
            id: params.get('id') ? Number(params.get('id')) : null,
        };
    }

    function toHash(st) {
        const qs = new URLSearchParams();
        if (st.tab !== 'todo') qs.set('tab', st.tab);
        if (st.sort) qs.set('sort', st.sort);
        if (st.q) qs.set('q', st.q);
        if (st.acct) qs.set('acct', st.acct);
        st.prio.forEach(p => qs.append('prio', p));
        st.type.forEach(t => qs.append('type', t));
        if (st.unread) qs.set('unread', '1');
        if (st.page > 1) qs.set('page', String(st.page));
        if (st.id) qs.set('id', String(st.id));
        const s = qs.toString();
        return 'mail' + (s ? '?' + s : '');
    }

    /** 상태 일부를 바꿔 주소를 갱신 (Router가 apply를 다시 부른다) */
    function navigate(patch, { replace = false, back = false } = {}) {
        if (back && S.id && history.state?.mailPanel) { history.back(); return; }
        const next = { ...S, ...patch };
        const opening = patch.id && !S.id;
        Router.go(toHash(next), { replace: replace && !opening, state: opening ? { mailPanel: true } : null });
    }

    /** Router → 화면 반영 */
    function apply(params) {
        Object.assign(S, fromParams(params));
        savePrefs();
        if (!accounts) loadAccounts();
        syncToolbar();
        const key = JSON.stringify([S.tab, S.sort, S.q, S.acct, S.prio, S.type, S.unread, S.page]);
        if (key !== listKey) {
            listKey = key;
            load();
        }
        if (S.id) {
            cursorId = S.id;
            if (!Store.get(S.id)) fetchRow(S.id).then(() => S.id && Panel.open(S.id));
            else Panel.open(S.id);
            markCursor();
        } else if (Panel.isOpen()) {
            Panel.close();
            markCursor();
        }
    }

    async function fetchRow(id) {
        try { Store.upsert(await api(`/api/emails/${id}`, { silent: true })); }
        catch (_) { toast('메일을 찾을 수 없습니다.', 'error'); navigate({ id: null }, { replace: true }); }
    }

    function queryParams(extra = {}) {
        const q = {
            tab: S.tab, sort: S.sort || undefined, q: S.q || undefined,
            priority: S.prio, type: S.type, unread: S.unread || undefined,
        };
        if (S.acct === 'upload') q.source = 'upload';
        else if (S.acct) q.account_id = S.acct;
        return { ...q, ...extra };
    }

    // ─── 툴바 ────────────────────────────────────────────────────────────────
    function syncToolbar() {
        renderTabs();
        if (document.activeElement !== el.search) el.search.value = S.q;
        el.searchClear.classList.toggle('hidden', !S.q);
        renderFilterPanel();
        renderFilterSummary();
    }

    function renderTabs() {
        const btn = ([v, label], sub) => {
            const n = counts[v];
            const warn = (v === 'due' || v === 'failed') && n > 0;
            return `<button type="button" data-tab="${v}" class="mail-tab ${sub ? 'mail-tab-sub' : ''} ${S.tab === v ? 'active' : ''}">${label}`
                + (n != null && (n > 0 || !sub) ? `<span class="mail-tab-count ${warn ? 'warn' : ''}">${n}</span>` : '') + '</button>';
        };
        // 분석실패는 있을 때(또는 보고 있을 때)만 보인다
        const subs = SUB_TABS.filter(([v]) => v !== 'failed' || counts.failed > 0 || S.tab === 'failed');
        el.tabs.innerHTML = `<div class="mail-tabs-main">${MAIN_TABS.map(t => btn(t, false)).join('')}</div>`
            + subs.map(t => btn(t, true)).join('');
    }

    function activeFilters() {
        const out = [];
        if (S.acct) {
            const a = S.acct === 'upload' ? { label: '업로드한 메일' } : (accounts || []).find(x => String(x.id) === S.acct);
            out.push(['acct', S.acct, a ? a.label : '계정']);
        }
        if (S.unread) out.push(['unread', '1', '안읽음만']);
        S.prio.forEach(p => out.push(['prio', p, `우선순위 ${p}`]));
        S.type.forEach(t => out.push(['type', t, t]));
        return out;
    }

    function renderFilterSummary() {
        const f = activeFilters();
        const cnt = el.filterBtn.querySelector('[data-ref="count"]');
        cnt.textContent = f.length || '';
        cnt.classList.toggle('hidden', !f.length);
        el.filterBtn.classList.toggle('active', f.length > 0);
        el.filterSummary.classList.toggle('hidden', !f.length);
        el.filterSummary.innerHTML = f.map(([d, v, label]) =>
            `<button type="button" class="filter-chip" data-clear="${d}" data-val="${esc(v)}" title="필터 해제">${esc(label)}<span class="material-symbols-outlined text-[13px]">close</span></button>`).join('')
            + (f.length > 1 ? '<button type="button" data-clear="all" class="text-xs text-slate-400 hover:text-red-500 ml-1">모두 해제</button>' : '');
    }

    function renderFilterPanel() {
        const sortVal = S.sort || (S.tab === 'due' ? 'deadline' : 'received');
        const acctOpts = [['', '전체 계정'], ['upload', '업로드한 메일'], ...(accounts || []).map(a => [String(a.id), a.label])];
        const chip = (dim, v, extra = '') =>
            `<button type="button" class="facet-chip ${extra} ${S[dim].includes(v) ? 'active' : ''}" data-dim="${dim}" data-val="${esc(v)}">${esc(v)}</button>`;
        el.filterPanel.innerHTML = `
          <div class="grid grid-cols-2 gap-3">
            <label class="block"><span class="field-label">계정</span>
              <select data-filter="acct" class="field-input !py-1.5">${acctOpts.map(([v, l]) =>
                `<option value="${esc(v)}" ${S.acct === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
            <label class="block"><span class="field-label">정렬</span>
              <select data-filter="sort" class="field-input !py-1.5">${SORTS.map(([v, l]) =>
                `<option value="${v}" ${sortVal === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
          </div>
          <label class="flex items-center gap-2 text-sm text-slate-600 cursor-pointer select-none">
            <input data-filter="unread" type="checkbox" ${S.unread ? 'checked' : ''} class="w-4 h-4 rounded accent-[#0047AB]"/>안 읽은 메일만</label>
          <div><span class="field-label">우선순위</span>
            <div class="flex flex-wrap gap-1.5">${PRIORITIES.map((p, i) => chip('prio', p, ['facet-chip-high', 'facet-chip-mid', 'facet-chip-low'][i])).join('')}</div></div>
          <div><span class="field-label">유형</span>
            <div class="flex flex-wrap gap-1.5">${TYPES.map(t => chip('type', t)).join('')}</div></div>
          <div class="flex justify-end pt-2 border-t border-outline-variant/10">
            <button type="button" data-action="clear-filters" class="text-xs text-slate-400 hover:text-red-500">필터 모두 해제</button>
          </div>`;
    }

    function onFilterClick(e) {
        e.stopPropagation();
        const chip = e.target.closest('.facet-chip');
        if (chip) {
            const dim = chip.dataset.dim, val = chip.dataset.val;
            const list = S[dim].includes(val) ? S[dim].filter(x => x !== val) : [...S[dim], val];
            navigate({ [dim]: list, page: 1 }, { replace: true });
        } else if (e.target.closest('[data-action="clear-filters"]')) {
            navigate({ prio: [], type: [], acct: '', unread: false, page: 1 }, { replace: true });
        }
    }

    function onFilterChange(e) {
        const f = e.target.dataset.filter;
        if (f === 'acct') navigate({ acct: e.target.value, page: 1 }, { replace: true });
        else if (f === 'sort') navigate({ sort: e.target.value, page: 1 }, { replace: true });
        else if (f === 'unread') navigate({ unread: e.target.checked, page: 1 }, { replace: true });
    }

    function toggleFilterPanel(force) {
        const open = force ?? el.filterPanel.classList.contains('hidden');
        el.filterPanel.classList.toggle('hidden', !open);
    }

    async function loadAccounts() {
        accounts = [];
        try {
            accounts = (await api('/api/accounts', { silent: true })).map(a => ({ id: a.id, label: a.label || a.email_address }));
            renderFilterPanel();
            renderFilterSummary();
        } catch (_) { accounts = null; }
    }

    // ─── 불러오기 ────────────────────────────────────────────────────────────
    async function load({ keepScroll = false, silent = false } = {}) {
        loadCtl?.abort();
        loadCtl = new AbortController();
        if (!silent) { base = null; el.banner.classList.add('hidden'); }   // 새 메일 배너 기준을 다시 잡는다
        if (!silent && !keepScroll) {
            (el.table.tBodies[0] || el.table).innerHTML = '<tr><td colspan="6" class="text-center text-slate-400 py-16">불러오는 중...</td></tr>';
        }
        try {
            const data = await api('/api/emails', { query: queryParams({ page: S.page, per_page: PER_PAGE }), signal: loadCtl.signal, silent });
            Store.upsertMany(data.emails);
            counts = data.counts || {};
            total = data.total || 0;
            renderTabs();
            ids = data.emails.map(r => r.id);
            for (const id of [...selected]) if (!ids.includes(id)) selected.delete(id);
            renderTable(keepScroll);
            el.total.textContent = `총 ${total}건`;
            renderPager(el.pager, S.page, PER_PAGE, total, p => navigate({ page: p }));
            renderBulk();
            if (S.id) Panel.renderHeader();
        } catch (e) {
            if (e.name === 'AbortError') return;
            if (!silent) el.table.tBodies[0].innerHTML = `<tr><td colspan="6" class="text-center text-red-500 py-16">${esc(e.message)}</td></tr>`;
        }
    }

    let countTimer = null;
    function refreshCounts() {
        clearTimeout(countTimer);
        countTimer = setTimeout(async () => {
            try {
                const d = await api('/api/emails', { query: queryParams({ per_page: 1 }), silent: true });
                counts = d.counts || counts;
                renderTabs();
            } catch (_) {}
        }, 300);
    }

    // ─── 목록 렌더 ───────────────────────────────────────────────────────────
    function renderTable(keepScroll) {
        const top = el.listWrap.scrollTop;
        const allChecked = ids.length && ids.every(id => selected.has(id));
        el.table.innerHTML = `
          <thead class="sticky top-0 z-10">
            <tr class="mail-head">
              <th class="px-3" style="width:2.75rem"><input type="checkbox" data-action="select-all" ${allChecked ? 'checked' : ''} class="w-4 h-4 accent-[#0047AB] cursor-pointer" title="이 페이지 전체 선택"/></th>
              <th>제목</th><th class="col-sender" style="width:9.5rem">보낸 곳</th><th style="width:5.25rem">마감</th>
              <th class="col-received" style="width:4.5rem">수신</th><th class="text-center" style="width:3.25rem">완료</th>
            </tr>
          </thead>
          <tbody>${ids.length ? ids.map(id => rowHtml(Store.get(id))).join('')
                : `<tr><td colspan="6" class="text-center text-slate-400 py-16">${emptyText()}</td></tr>`}</tbody>`;
        el.listWrap.scrollTop = keepScroll ? top : 0;
    }

    function emptyText() {
        if (S.q || activeFilters().length) return '조건에 맞는 메일이 없습니다.';
        return { todo: '할 일이 없습니다. 👍', due: '마감이 임박한 메일이 없습니다.', done: '완료한 메일이 없습니다.',
            excluded: '제외한 메일이 없습니다.', failed: '분석에 실패한 메일이 없습니다.', all: '메일이 없습니다.' }[S.tab];
    }

    function matchesTab(r) {
        const todo = !r.is_completed && !r.is_excluded;
        switch (S.tab) {
            case 'todo': return todo;
            case 'due': return todo && r.deadline && DateUtil.daysUntil(r.deadline) <= 3;
            case 'done': return r.is_completed && !r.is_excluded;
            case 'excluded': return r.is_excluded;
            case 'failed': return r.analysis_status === 'failed';
            default: return true;
        }
    }

    /** 한 줄: 제목(+우선순위 점·첨부) / 요약(+유형·분석 상태). 나머지 정보는 상세 패널에서 */
    function rowHtml(r) {
        if (!r) return '';
        const stale = !matchesTab(r) || (S.unread && r.is_read && r.id !== S.id);
        const cls = ['mail-row', r.is_read ? '' : 'unread', r.id === cursorId ? 'is-cursor' : '',
            selected.has(r.id) ? 'is-selected' : '', stale ? 'is-stale' : '', r.is_completed ? 'is-done' : ''].join(' ');
        const status = Badges.analysis(r, { retry: true });
        const sub = [
            r.req_type ? `<span class="text-slate-500 shrink-0">${esc(r.req_type)}</span>` : '',
            r.summary ? `<span class="truncate">${esc(r.summary)}</span>` : '',
        ].filter(Boolean).join('<span class="text-slate-300 shrink-0">·</span>');
        return `<tr data-id="${r.id}" class="${cls}">
            <td class="px-3" data-stop><input type="checkbox" class="mail-check w-4 h-4 accent-[#0047AB] cursor-pointer" data-id="${r.id}" ${selected.has(r.id) ? 'checked' : ''}/></td>
            <td class="py-2 pr-2">
              <div class="flex items-center gap-1.5 min-w-0">
                <span class="prio-dot ${PRIO_DOT[r.priority] || ''}" title="${r.priority ? `우선순위 ${esc(r.priority)}` : ''}"></span>
                <span class="mail-subject truncate">${esc(r.subject || '(제목 없음)')}</span>
                ${r.has_attachments ? '<span class="material-symbols-outlined text-[15px] text-slate-400 shrink-0" title="첨부파일">attach_file</span>' : ''}
                ${r.source === 'upload' ? '<span class="material-symbols-outlined text-[15px] text-violet-500 shrink-0" title="업로드한 EML">upload_file</span>' : ''}
              </div>
              <div class="flex items-center gap-1.5 mt-0.5 pl-3.5 text-xs text-slate-400 min-w-0 empty:hidden">${status}${
                  r.exclude_reason ? `<span class="shrink-0 flex items-center gap-0.5" title="${esc(r.exclude_reason)}"><span class="material-symbols-outlined text-[13px]">filter_alt</span>자동 제외</span>` : ''}${sub}<span class="col-sender-inline truncate">${esc(r.company || r.sender || '')}</span></div>
            </td>
            <td class="col-sender text-xs text-slate-500 pr-2"><p class="truncate" title="${esc(r.sender)}">${esc(r.company || r.sender || '-')}</p></td>
            <td class="whitespace-nowrap">${Badges.deadline(r)}</td>
            <td class="col-received text-xs text-slate-500 whitespace-nowrap" title="${esc(r.received_at)}">${esc(DateUtil.shortDate(r.received_at))}</td>
            <td class="text-center" data-stop>
              <button type="button" data-action="toggle-complete" data-id="${r.id}" class="complete-toggle ${r.is_completed ? 'on' : ''}" title="${r.is_completed ? '완료 취소' : '완료'} (e)">
                <span class="material-symbols-outlined">${r.is_completed ? 'task_alt' : 'radio_button_unchecked'}</span></button>
            </td>
          </tr>`;
    }

    function onRowChanged(r) {
        if (!ids.includes(r.id)) return;
        const tr = el.table.querySelector(`tr[data-id="${r.id}"]`);
        if (tr) tr.outerHTML = rowHtml(r);
    }

    function markCursor() {
        el.table.querySelectorAll('tr.is-cursor').forEach(tr => tr.classList.remove('is-cursor'));
        if (cursorId == null) return;
        const target = el.table.querySelector(`tr[data-id="${cursorId}"]`);
        if (target) {
            target.classList.add('is-cursor');
            target.scrollIntoView({ block: 'nearest' });
        }
    }

    // ─── 목록 이벤트 ─────────────────────────────────────────────────────────
    function onListClick(e) {
        const action = e.target.closest('[data-action]');
        if (action) {
            const a = action.dataset.action;
            const id = Number(action.dataset.id);
            if (a === 'toggle-complete') { MailActions.toggleComplete(id); return; }
            if (a === 'analyze') { e.stopPropagation(); MailActions.analyze(id); return; }
            if (a === 'select-all') return;   // change 이벤트에서 처리
        }
        if (e.target.closest('[data-stop]')) return;
        const tr = e.target.closest('tr[data-id]');
        if (tr) openMail(Number(tr.dataset.id));
    }

    function onListChange(e) {
        const t = e.target;
        if (t.matches('[data-action="select-all"]')) {
            ids.forEach(id => (t.checked ? selected.add(id) : selected.delete(id)));
            el.table.querySelectorAll('.mail-check').forEach(c => { c.checked = t.checked; c.closest('tr')?.classList.toggle('is-selected', t.checked); });
            renderBulk();
        } else if (t.matches('.mail-check')) {
            const id = Number(t.dataset.id);
            t.checked ? selected.add(id) : selected.delete(id);
            t.closest('tr')?.classList.toggle('is-selected', t.checked);
            renderBulk();
        }
    }

    function openMail(id) {
        cursorId = id;
        navigate({ id }, { replace: Panel.isOpen() });
    }

    function move(delta) {
        if (!ids.length) return;
        const cur = S.id ?? cursorId;
        let i = ids.indexOf(cur);
        i = i < 0 ? (delta > 0 ? 0 : ids.length - 1) : i + delta;
        if (i < 0 || i >= ids.length) return;
        openMail(ids[i]);
    }

    // ─── 일괄 처리 ───────────────────────────────────────────────────────────
    const BULK = {
        analyze: ['auto_awesome', '분석'], complete: ['check_circle', '완료'], uncomplete: ['undo', '완료 취소'],
        exclude: ['visibility_off', '제외'], unexclude: ['restore_from_trash', '제외 해제'],
        read: ['drafts', '읽음으로'], unread: ['mark_email_unread', '안읽음으로'],
        excel: ['download', 'Excel'], delete: ['delete', '삭제'],
    };

    /** 지금 탭에서 자주 쓰는 동작만 앞에, 나머지는 ⋯ 메뉴로 */
    function bulkLayout() {
        const main = S.tab === 'done' ? ['uncomplete', 'exclude', 'excel']
            : S.tab === 'excluded' ? ['unexclude', 'delete']
            : S.tab === 'failed' ? ['analyze', 'exclude']
            : ['complete', 'exclude', 'analyze'];
        const rest = Object.keys(BULK).filter(k => !main.includes(k));
        return { main, rest };
    }

    function renderBulk() {
        const n = selected.size;
        el.bulk.classList.toggle('hidden', n === 0);
        if (!n) return;
        const { main, rest } = bulkLayout();
        const btn = (k, cls = 'bulk-btn') => `<button type="button" data-bulk="${k}" class="${cls} ${k === 'delete' ? 'text-red-600' : ''}">
            <span class="material-symbols-outlined text-sm">${BULK[k][0]}</span>${BULK[k][1]}</button>`;
        el.bulk.innerHTML = `
          <span class="text-sm font-semibold text-primary mr-1">${n}개 선택</span>
          ${main.map(k => btn(k)).join('')}
          <div class="relative">
            <button type="button" data-bulk="more" class="bulk-btn" title="더보기"><span class="material-symbols-outlined text-sm">more_horiz</span></button>
            <div data-ref="bulk-menu" class="hidden absolute left-0 top-full mt-1 z-30 w-44 py-1 bg-surface-container-lowest rounded-xl shadow-xl border border-outline-variant/20 text-sm">
              ${rest.map(k => btn(k, 'menu-item')).join('')}
            </div>
          </div>
          <button type="button" data-bulk="clear" class="bulk-btn ml-auto">선택 해제</button>`;
    }

    async function onBulkClick(e) {
        const b = e.target.closest('[data-bulk]');
        if (!b) return;
        const action = b.dataset.bulk;
        if (action === 'more') { el.bulk.querySelector('[data-ref="bulk-menu"]')?.classList.toggle('hidden'); return; }
        const list = [...selected];
        if (action === 'clear') { selected.clear(); renderTable(true); renderBulk(); return; }
        if (action === 'excel') { MailActions.exportExcel(list); return; }
        if (action === 'delete' && !confirm(`${list.length}개 메일을 앱에서 삭제할까요?\n(메일 서버의 원본은 지워지지 않습니다)`)) return;
        if (action === 'analyze' && list.length > 20 && !confirm(`${list.length}개 메일을 분석 대기열에 올릴까요?`)) return;
        try {
            const n = await MailActions.bulk(list, action);
            if (action === 'delete') list.forEach(id => Store.remove(id));
            const label = { analyze: '분석 대기열에 올렸습니다', complete: '완료 처리했습니다', uncomplete: '완료를 취소했습니다',
                exclude: '제외했습니다', unexclude: '제외를 해제했습니다', read: '읽음으로 표시했습니다',
                unread: '안읽음으로 표시했습니다', delete: '삭제했습니다' }[action];
            toast(`${n}건 ${label}.`, 'success');
        } catch (_) {}
    }

    // ─── 키보드 ──────────────────────────────────────────────────────────────
    function onKey(e) {
        if (Router.route !== 'mail' || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
        if (Attachments.isLightboxOpen() || document.querySelector('.app-modal:not(.hidden)')) return;
        const t = e.target;
        if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) {
            if (e.key === 'Escape') t.blur();
            return;
        }
        const id = S.id;
        switch (e.key) {
            case 'j': case 'ArrowDown': move(1); break;
            case 'k': case 'ArrowUp': move(-1); break;
            case 'Enter': if (!id && cursorId) openMail(cursorId); else return; break;
            case 'Escape':
                if (!el.filterPanel.classList.contains('hidden')) toggleFilterPanel(false);
                else if (id) navigate({ id: null }, { replace: true });
                else return;
                break;
            case 'e': if (id) MailActions.toggleComplete(id); else return; break;
            case 'x': if (id) MailActions.toggleExclude(id); else return; break;
            case 'u': if (id) MailActions.toggleRead(id); else return; break;
            case 'd': if (id) Panel.focusDeadline(); else return; break;
            case '/': el.search.focus(); el.search.select(); break;
            default: return;
        }
        e.preventDefault();
    }

    // ─── 처리 현황(Status) 연동 ──────────────────────────────────────────────
    function onStatus(st) {
        const latest = st.latest_email_id || 0;
        const all = st.counts?.all || 0;
        if (!base) base = { latest, all };
        if (Router.route === 'mail' && latest > base.latest && all > base.all) showBanner(all - base.all);
        // 대기/분석 중인 메일이 화면에 있거나 대기열이 바뀌면 조용히 상태만 갱신
        const sig = `${st.analysis.queued}|${st.analysis.running?.id || ''}|${st.analysis.failed}`;
        const visibleBusy = ids.some(id => ['queued', 'running'].includes(Store.get(id)?.analysis_status));
        if (Router.route === 'mail' && (sig !== lastQueueSig || visibleBusy)) silentRefresh();
        lastQueueSig = sig;
    }

    async function silentRefresh() {
        try {
            const data = await api('/api/emails', { query: queryParams({ page: S.page, per_page: PER_PAGE }), silent: true });
            counts = data.counts || counts;
            renderTabs();
            data.emails.forEach(r => { if (ids.includes(r.id)) Store.upsert(r); });
        } catch (_) {}
    }

    function showBanner(n) {
        el.banner.innerHTML = `<span class="material-symbols-outlined text-base">mark_email_unread</span>
            새 메일 ${n}건이 도착했습니다.
            <button type="button" data-action="reload" class="font-semibold underline underline-offset-2 ml-1">새로고침</button>`;
        el.banner.classList.remove('hidden');
    }
    function hideBanner() {
        el.banner.classList.add('hidden');
        base = null;
    }

    function onLeave() { Panel.close(); }

    global.Mail = { init, apply, onStatus, onLeave, open: openMail, get state() { return S; } };
})(window);
