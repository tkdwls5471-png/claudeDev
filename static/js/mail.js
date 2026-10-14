/**
 * mail.js — 통합 '메일' 화면 (받은메일함 + 분석 일정 + 분석 기록을 하나로)
 *  - 상태 탭(할 일/마감임박/완료/제외/분석실패/전체)과 건수
 *  - 목록 ↔ 일정(Gantt) 보기, 정렬, 검색, 우선순위·유형 칩, 계정(업로드 포함), 안읽음만
 *  - 행에서 바로 완료, 분석 실패 재시도 / 여러 건 선택 후 일괄 처리
 *  - 오른쪽 상세 패널 + 키보드(j/k ↑/↓ 이동, Esc, e 완료, x 제외, d 마감, u 읽음, / 검색)
 *  - 화면 상태는 주소(#mail?...)에 담겨 새로고침·뒤로가기에도 유지
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const TABS = [
        ['todo', '할 일'], ['due', '마감임박'], ['done', '완료'],
        ['excluded', '제외'], ['failed', '분석실패'], ['all', '전체'],
    ];
    const SORTS = [['received', '수신일순'], ['deadline', '마감일순'], ['priority', '우선순위순'], ['analyzed', '분석일순']];
    const PRIORITIES = ['높음', '보통', '낮음'];
    const TYPES = ['검토요청', '보고서/자료 요청', '수정/작업요청', '견적요청', '작업완료보고', '회의요청', '기타'];
    const PER_PAGE = 50;
    const PREF_KEY = 'eml_mail_prefs';
    const PREF_FIELDS = ['tab', 'view', 'sort', 'acct', 'prio', 'type', 'unread', 'from', 'to'];

    // 표는 table-layout:fixed → 제목 열만 남는 폭을 쓰고 나머지는 고정 폭 (긴 제목이 표를 밀어내지 않게)
    const COLS = {
        subject:  { label: '제목', w: '', th: '', td: '' },
        sender:   { label: '보낸 곳', w: '9.5rem', th: 'hidden @[640px]:table-cell', td: 'hidden @[640px]:table-cell' },
        received: { label: '수신', w: '4rem', th: 'hidden @[520px]:table-cell', td: 'hidden @[520px]:table-cell whitespace-nowrap' },
        deadline: { label: '마감', w: '4.75rem', th: '', td: 'whitespace-nowrap' },
        priority: { label: '우선순위', w: '4.5rem', th: 'hidden @[900px]:table-cell', td: 'hidden @[900px]:table-cell whitespace-nowrap' },
        type:     { label: '유형', w: '7.5rem', th: 'hidden @[1040px]:table-cell', td: 'hidden @[1040px]:table-cell whitespace-nowrap overflow-hidden' },
        analysis: { label: '분석', w: '8.5rem', th: 'hidden @[720px]:table-cell', td: 'hidden @[720px]:table-cell whitespace-nowrap' },
    };
    const COL_DEFAULT = ['subject', 'sender', 'received', 'deadline', 'priority', 'type', 'analysis'];

    const S = {                 // 화면 상태 (주소에 반영되는 값)
        tab: 'todo', view: 'list', sort: '', q: '', acct: '', prio: [], type: [], unread: false,
        page: 1, from: '', to: '', id: null,
    };
    let ids = [];               // 지금 보이는 순서 (목록 또는 일정)
    let total = 0;
    let counts = {};
    let listKey = null;
    let loadCtl = null;
    const selected = new Set();
    let cursorId = null;
    let base = null;            // 새 메일 배너 기준 { latest, all }
    let lastQueueSig = '';
    let accountsLoaded = false;
    let el = {};

    // ─── 초기화 ──────────────────────────────────────────────────────────────
    function init() {
        const $ = id => document.getElementById(id);
        el = {
            section: $('mail-section'), tabs: $('mail-tabs'), search: $('mail-search'), searchClear: $('mail-search-clear'),
            sort: $('mail-sort'), acct: $('mail-account'), unread: $('mail-unread'), facetBtn: $('mail-facet-btn'),
            facetPanel: $('mail-facet-panel'), facetSummary: $('mail-facet-summary'), viewList: $('mail-view-list'),
            viewGantt: $('mail-view-gantt'), range: $('mail-range'), from: $('mail-from'), to: $('mail-to'),
            banner: $('mail-banner'), bulk: $('mail-bulk'), listWrap: $('mail-list-wrap'), table: $('mail-table'),
            gantt: $('mail-gantt'), footer: $('mail-footer'), total: $('mail-total'), pager: $('mail-pager'),
            panel: $('mail-panel'), notice: $('mail-notice'),
        };

        el.sort.innerHTML = SORTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
        el.facetPanel.innerHTML = `
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-[11px] font-bold text-slate-400 uppercase tracking-widest w-14 shrink-0">우선순위</span>
            ${PRIORITIES.map((p, i) => `<button type="button" class="facet-chip ${['facet-chip-high', 'facet-chip-mid', 'facet-chip-low'][i]}" data-dim="prio" data-val="${p}">${p}</button>`).join('')}
          </div>
          <div class="flex flex-wrap items-start gap-2">
            <span class="text-[11px] font-bold text-slate-400 uppercase tracking-widest w-14 shrink-0 pt-1.5">유형</span>
            <div class="flex flex-wrap items-center gap-2 flex-1">
              ${TYPES.map(t => `<button type="button" class="facet-chip" data-dim="type" data-val="${esc(t)}">${esc(t)}</button>`).join('')}
            </div>
          </div>
          <div class="flex justify-end pt-1 border-t border-outline-variant/10">
            <button type="button" data-action="clear-facets" class="text-xs text-slate-400 hover:text-red-500">전체 해제</button>
          </div>`;

        // 툴바
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
        el.sort.addEventListener('change', () => navigate({ sort: el.sort.value, page: 1 }, { replace: true }));
        el.acct.addEventListener('change', () => navigate({ acct: el.acct.value, page: 1 }, { replace: true }));
        el.unread.addEventListener('change', () => navigate({ unread: el.unread.checked, page: 1 }, { replace: true }));
        el.viewList.addEventListener('click', () => navigate({ view: 'list' }));
        el.viewGantt.addEventListener('click', () => navigate({ view: 'gantt' }));
        el.from.addEventListener('change', () => navigate({ from: el.from.value }, { replace: true }));
        el.to.addEventListener('change', () => navigate({ to: el.to.value }, { replace: true }));
        el.range.addEventListener('click', e => {
            const b = e.target.closest('[data-range]');
            if (!b) return;
            const [back, fwd] = b.dataset.range.split(',').map(Number);
            const t = DateUtil.todayStr();
            navigate({ from: DateUtil.addDays(t, -back), to: DateUtil.addDays(t, fwd) }, { replace: true });
        });
        el.facetBtn.addEventListener('click', e => { e.stopPropagation(); toggleFacetPanel(); });
        el.facetPanel.addEventListener('click', e => {
            e.stopPropagation();
            const chip = e.target.closest('.facet-chip');
            if (chip) {
                const dim = chip.dataset.dim, val = chip.dataset.val;
                const list = S[dim].includes(val) ? S[dim].filter(x => x !== val) : [...S[dim], val];
                navigate({ [dim]: list, page: 1 }, { replace: true });
            } else if (e.target.closest('[data-action="clear-facets"]')) {
                navigate({ prio: [], type: [], page: 1 }, { replace: true });
            }
        });
        el.facetSummary.addEventListener('click', e => {
            const c = e.target.closest('[data-dim]');
            if (c) navigate({ [c.dataset.dim]: S[c.dataset.dim].filter(x => x !== c.dataset.val), page: 1 }, { replace: true });
        });
        document.addEventListener('click', e => {
            if (!el.facetPanel.classList.contains('hidden') && !e.target.closest('#mail-facet-control')) toggleFacetPanel(false);
        });
        el.banner.addEventListener('click', e => {
            if (e.target.closest('[data-action="reload"]')) { hideBanner(); load(); }
        });

        // 목록 (위임)
        el.listWrap.addEventListener('click', onListClick);
        el.listWrap.addEventListener('change', onListChange);
        el.gantt.addEventListener('click', e => {
            const row = e.target.closest('.gantt-row[data-id]');
            if (row) openMail(Number(row.dataset.id));
        });
        el.bulk.addEventListener('click', onBulkClick);
        el.table.addEventListener('dragstart', onColDrag);
        el.table.addEventListener('dragover', onColDrag);
        el.table.addEventListener('dragleave', onColDrag);
        el.table.addEventListener('drop', onColDrag);
        el.table.addEventListener('dragend', onColDrag);

        Panel.mount(el.panel, {
            onClose: () => navigate({ id: null }, { replace: isMobile() ? false : true, back: isMobile() }),
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
        const src = has ? null : loadPrefs();
        const get = k => (src ? src[k] : params.get(k));
        const getAll = k => (src ? (src[k] || []) : params.getAll(k));
        const tab = get('tab');
        const next = {
            tab: TABS.some(([v]) => v === tab) ? tab : 'todo',
            view: get('view') === 'gantt' ? 'gantt' : 'list',
            sort: SORTS.some(([v]) => v === get('sort')) ? get('sort') : '',
            q: src ? '' : (params.get('q') || ''),
            acct: String(get('acct') || ''),
            prio: getAll('prio').filter(p => PRIORITIES.includes(p)),
            type: getAll('type').filter(t => TYPES.includes(t)),
            unread: src ? !!src.unread : params.get('unread') === '1',
            page: src ? 1 : Math.max(1, parseInt(params.get('page') || '1', 10) || 1),
            from: get('from') || '', to: get('to') || '',
            id: params.get('id') ? Number(params.get('id')) : null,
        };
        if (!next.from || !next.to) {
            const t = DateUtil.todayStr();
            next.from = next.from || DateUtil.addDays(t, -14);
            next.to = next.to || DateUtil.addDays(t, 30);
        }
        return next;
    }

    function toHash(st) {
        const qs = new URLSearchParams();
        if (st.tab !== 'todo') qs.set('tab', st.tab);
        if (st.view !== 'list') qs.set('view', st.view);
        if (st.sort) qs.set('sort', st.sort);
        if (st.q) qs.set('q', st.q);
        if (st.acct) qs.set('acct', st.acct);
        st.prio.forEach(p => qs.append('prio', p));
        st.type.forEach(t => qs.append('type', t));
        if (st.unread) qs.set('unread', '1');
        if (st.page > 1) qs.set('page', String(st.page));
        if (st.view === 'gantt') { qs.set('from', st.from); qs.set('to', st.to); }
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
        const next = fromParams(params);
        Object.assign(S, next);
        savePrefs();
        syncToolbar();
        const key = JSON.stringify([S.tab, S.view, S.sort, S.q, S.acct, S.prio, S.type, S.unread, S.page,
            S.view === 'gantt' ? [S.from, S.to] : null]);
        if (!accountsLoaded) loadAccounts();
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
        el.sort.value = S.sort || (S.tab === 'due' ? 'deadline' : 'received');
        if ([...el.acct.options].some(o => o.value === S.acct)) el.acct.value = S.acct;
        el.unread.checked = S.unread;
        const gantt = S.view === 'gantt';
        el.viewList.classList.toggle('active', !gantt);
        el.viewGantt.classList.toggle('active', gantt);
        el.range.classList.toggle('hidden', !gantt);
        el.sort.classList.toggle('hidden', gantt);
        el.from.value = S.from;
        el.to.value = S.to;
        el.facetPanel.querySelectorAll('.facet-chip').forEach(c => c.classList.toggle('active', S[c.dataset.dim].includes(c.dataset.val)));
        const chips = [...S.prio.map(v => ['prio', v]), ...S.type.map(v => ['type', v])];
        el.facetSummary.innerHTML = chips.map(([d, v]) =>
            `<span class="facet-summary-chip" data-dim="${d}" data-val="${esc(v)}">${esc(v)}<span class="material-symbols-outlined text-[13px]">close</span></span>`).join('');
        el.facetBtn.querySelector('[data-ref="count"]').textContent = chips.length ? String(chips.length) : '';
    }

    function renderTabs() {
        el.tabs.innerHTML = TABS.map(([v, label]) => {
            const n = counts[v];
            const warn = (v === 'due' || v === 'failed') && n > 0;
            return `<button type="button" data-tab="${v}" class="mail-tab ${S.tab === v ? 'active' : ''}">${label}`
                + (n != null ? `<span class="mail-tab-count ${warn ? 'warn' : ''}">${n}</span>` : '') + '</button>';
        }).join('');
    }

    function toggleFacetPanel(force) {
        const open = force ?? el.facetPanel.classList.contains('hidden');
        el.facetPanel.classList.toggle('hidden', !open);
        el.facetBtn.querySelector('[data-ref="icon"]').textContent = open ? 'expand_less' : 'expand_more';
    }

    async function loadAccounts() {
        accountsLoaded = true;
        try {
            const accs = await api('/api/accounts', { silent: true });
            el.acct.innerHTML = '<option value="">전체 계정</option><option value="upload">업로드한 메일</option>'
                + accs.map(a => `<option value="${a.id}">${esc(a.label || a.email_address)}</option>`).join('');
            el.acct.value = S.acct;
        } catch (_) { accountsLoaded = false; }
    }

    // ─── 불러오기 ────────────────────────────────────────────────────────────
    async function load({ keepScroll = false, silent = false } = {}) {
        loadCtl?.abort();
        loadCtl = new AbortController();
        const gantt = S.view === 'gantt';
        el.table.classList.toggle('hidden', gantt);
        el.gantt.classList.toggle('hidden', !gantt);
        el.footer.classList.toggle('hidden', gantt);
        el.notice.classList.add('hidden');
        if (!silent) { base = null; el.banner.classList.add('hidden'); }   // 새 메일 배너 기준을 다시 잡는다
        if (!silent && !keepScroll) {
            (gantt ? el.gantt : el.table.tBodies[0] || el.table).innerHTML = gantt
                ? '<p class="text-center text-slate-400 text-sm py-16">불러오는 중...</p>'
                : '<tr><td colspan="9" class="text-center text-slate-400 py-16">불러오는 중...</td></tr>';
        }
        const query = gantt
            ? queryParams({ range_from: S.from, range_to: S.to, per_page: 1000, sort: 'received' })
            : queryParams({ page: S.page, per_page: PER_PAGE });
        try {
            const data = await api('/api/emails', { query, signal: loadCtl.signal, silent });
            Store.upsertMany(data.emails);
            counts = data.counts || {};
            total = data.total || 0;
            renderTabs();
            if (gantt) {
                const rows = [...data.emails].sort((a, b) => (a.received_at || '').localeCompare(b.received_at || ''));
                ids = rows.map(r => r.id);
                Gantt.render(el.gantt, rows, { from: S.from, to: S.to, cursorId });
                if (total > rows.length) {
                    el.notice.textContent = `${total}건 중 ${rows.length}건만 표시합니다. 기간을 좁혀 주세요.`;
                    el.notice.classList.remove('hidden');
                }
            } else {
                ids = data.emails.map(r => r.id);
                for (const id of [...selected]) if (!ids.includes(id)) selected.delete(id);
                renderTable(keepScroll);
                el.total.textContent = `총 ${total}건`;
                renderPager(el.pager, S.page, PER_PAGE, total, p => navigate({ page: p }));
            }
            renderBulk();
            if (S.id) Panel.renderHeader();
        } catch (e) {
            if (e.name === 'AbortError') return;
            if (!silent) {
                (gantt ? el.gantt : el.table.tBodies[0]).innerHTML = gantt
                    ? `<p class="text-center text-red-500 text-sm py-16">${esc(e.message)}</p>`
                    : `<tr><td colspan="9" class="text-center text-red-500 py-16">${esc(e.message)}</td></tr>`;
            }
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
    function colOrder() {
        try {
            const saved = JSON.parse(localStorage.getItem('mail_col_order') || 'null');
            if (Array.isArray(saved) && saved.length === COL_DEFAULT.length && COL_DEFAULT.every(k => saved.includes(k))) return saved;
        } catch (_) {}
        return COL_DEFAULT.slice();
    }

    function renderTable(keepScroll) {
        const scroller = el.listWrap;
        const top = scroller.scrollTop;
        const order = colOrder();
        const allChecked = ids.length && ids.every(id => selected.has(id));
        el.table.innerHTML = `
          <thead class="sticky top-0 z-10">
            <tr class="mail-head">
              <th class="w-10 px-3 py-2.5"><input type="checkbox" data-action="select-all" ${allChecked ? 'checked' : ''} class="w-4 h-4 accent-[#0047AB] cursor-pointer" title="이 페이지 전체 선택"/></th>
              ${order.map(k => `<th draggable="true" data-col="${k}" class="mail-th ${COLS[k].th}" ${COLS[k].w ? `style="width:${COLS[k].w}"` : ''} title="드래그해서 열 순서 변경">${COLS[k].label}</th>`).join('')}
              <th class="w-14 px-2 py-2.5 text-center">완료</th>
            </tr>
          </thead>
          <tbody>${ids.length ? ids.map(id => rowHtml(Store.get(id), order)).join('')
                : `<tr><td colspan="9" class="text-center text-slate-400 py-16">${emptyText()}</td></tr>`}</tbody>`;
        if (keepScroll) scroller.scrollTop = top; else scroller.scrollTop = 0;
    }

    function emptyText() {
        if (S.q || S.prio.length || S.type.length || S.unread || S.acct) return '조건에 맞는 메일이 없습니다.';
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

    function rowHtml(r, order = colOrder()) {
        if (!r) return '';
        const stale = !matchesTab(r) || (S.unread && r.is_read && r.id !== S.id);
        const cls = ['mail-row', r.is_read ? '' : 'unread', r.id === cursorId ? 'is-cursor' : '',
            selected.has(r.id) ? 'is-selected' : '', stale ? 'is-stale' : '', r.is_completed ? 'is-done' : ''].join(' ');
        const mini = [
            r.exclude_reason ? `<span class="text-[11px] text-slate-400 flex items-center gap-0.5 shrink-0" title="${esc(r.exclude_reason)}"><span class="material-symbols-outlined text-[13px]">filter_alt</span>자동 제외</span>` : '',
            `<span class="@[900px]:hidden">${Badges.priority(r.priority)}</span>`,
            `<span class="@[720px]:hidden">${Badges.analysis(r, { retry: false })}</span>`,
            `<span class="@[640px]:hidden text-[11px] text-slate-400 truncate">${esc(r.company || r.sender || '')}</span>`,
        ].join('');
        const cell = {
            subject: `<td class="px-2 py-2 ${COLS.subject.td}">
                <div class="flex items-center gap-1.5 min-w-0">
                  <span class="unread-dot ${r.is_read ? 'invisible' : ''}"></span>
                  ${r.has_attachments ? '<span class="material-symbols-outlined text-[15px] text-slate-400 shrink-0" title="첨부파일">attach_file</span>' : ''}
                  ${r.source === 'upload' ? '<span class="material-symbols-outlined text-[15px] text-violet-500 shrink-0" title="업로드한 EML">upload_file</span>' : ''}
                  <span class="mail-subject truncate">${esc(r.subject || '(제목 없음)')}</span>
                </div>
                ${r.summary ? `<p class="text-xs text-slate-400 truncate mt-0.5 pl-3.5">${esc(r.summary)}</p>` : ''}
                <div class="flex items-center gap-1 mt-0.5 pl-3.5 empty:hidden">${mini.replace(/<span class="[^"]*"><\/span>/g, '')}</div>
              </td>`,
            sender: `<td class="px-2 py-2 text-xs text-slate-500 ${COLS.sender.td}"><p class="truncate" title="${esc(r.sender)}">${esc(r.company || r.sender || '-')}</p></td>`,
            received: `<td class="px-2 py-2 text-xs text-slate-500 ${COLS.received.td}" title="${esc(r.received_at)}">${esc(DateUtil.shortDate(r.received_at))}</td>`,
            deadline: `<td class="px-2 py-2 ${COLS.deadline.td}">${Badges.deadline(r)}</td>`,
            priority: `<td class="px-2 py-2 ${COLS.priority.td}">${Badges.priority(r.priority)}</td>`,
            type: `<td class="px-2 py-2 ${COLS.type.td}">${Badges.type(r.req_type)}</td>`,
            analysis: `<td class="px-2 py-2 ${COLS.analysis.td}">${Badges.analysis(r)}</td>`,
        };
        return `<tr data-id="${r.id}" class="${cls}">
            <td class="px-3 py-2 w-10" data-stop><input type="checkbox" class="mail-check w-4 h-4 accent-[#0047AB] cursor-pointer" data-id="${r.id}" ${selected.has(r.id) ? 'checked' : ''}/></td>
            ${order.map(k => cell[k]).join('')}
            <td class="px-2 py-2 text-center" data-stop>
              <button type="button" data-action="toggle-complete" data-id="${r.id}" class="complete-toggle ${r.is_completed ? 'on' : ''}" title="${r.is_completed ? '완료 취소' : '완료'}">
                <span class="material-symbols-outlined">${r.is_completed ? 'task_alt' : 'radio_button_unchecked'}</span></button>
            </td>
          </tr>`;
    }

    function onRowChanged(r) {
        if (!ids.includes(r.id)) return;
        if (S.view === 'gantt') {
            clearTimeout(onRowChanged._t);
            onRowChanged._t = setTimeout(() => {
                const rows = ids.map(id => Store.get(id)).filter(Boolean);
                Gantt.render(el.gantt, rows, { from: S.from, to: S.to, cursorId });
            }, 50);
            return;
        }
        const tr = el.table.querySelector(`tr[data-id="${r.id}"]`);
        if (tr) tr.outerHTML = rowHtml(r);
    }

    function markCursor() {
        el.table.querySelectorAll('tr.is-cursor').forEach(tr => tr.classList.remove('is-cursor'));
        el.gantt.querySelectorAll('.gantt-row.is-cursor').forEach(tr => tr.classList.remove('is-cursor'));
        if (cursorId == null) return;
        const target = el.table.querySelector(`tr[data-id="${cursorId}"]`) || el.gantt.querySelector(`.gantt-row[data-id="${cursorId}"]`);
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
            if (a === 'analyze') { MailActions.analyze(id); return; }
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
    function renderBulk() {
        const n = selected.size;
        el.bulk.classList.toggle('hidden', n === 0);
        if (!n) return;
        el.bulk.innerHTML = `
          <span class="text-sm font-semibold text-primary mr-1">${n}개 선택</span>
          <button type="button" data-bulk="analyze" class="bulk-btn"><span class="material-symbols-outlined text-sm">auto_awesome</span>분석</button>
          <button type="button" data-bulk="complete" class="bulk-btn"><span class="material-symbols-outlined text-sm">check_circle</span>완료</button>
          <button type="button" data-bulk="uncomplete" class="bulk-btn">완료 취소</button>
          <button type="button" data-bulk="exclude" class="bulk-btn"><span class="material-symbols-outlined text-sm">visibility_off</span>제외</button>
          <button type="button" data-bulk="unexclude" class="bulk-btn">제외 해제</button>
          <button type="button" data-bulk="read" class="bulk-btn">읽음</button>
          <button type="button" data-bulk="unread" class="bulk-btn">안읽음</button>
          <button type="button" data-bulk="excel" class="bulk-btn"><span class="material-symbols-outlined text-sm">download</span>Excel</button>
          <button type="button" data-bulk="delete" class="bulk-btn text-red-600"><span class="material-symbols-outlined text-sm">delete</span>삭제</button>
          <button type="button" data-bulk="clear" class="bulk-btn ml-auto">선택 해제</button>`;
    }

    async function onBulkClick(e) {
        const b = e.target.closest('[data-bulk]');
        if (!b) return;
        const action = b.dataset.bulk;
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

    // ─── 열 순서 드래그 ──────────────────────────────────────────────────────
    let dragCol = null;
    function onColDrag(e) {
        const th = e.target.closest?.('th[data-col]');
        if (e.type === 'dragstart' && th) {
            dragCol = th.dataset.col;
            e.dataTransfer.effectAllowed = 'move';
            try { e.dataTransfer.setData('text/plain', dragCol); } catch (_) {}
            th.classList.add('col-dragging');
        } else if (e.type === 'dragover' && th && dragCol) {
            e.preventDefault();
            th.classList.add('col-drop');
        } else if (e.type === 'dragleave' && th) {
            th.classList.remove('col-drop');
        } else if (e.type === 'drop' && th && dragCol) {
            e.preventDefault();
            const order = colOrder().filter(k => k !== dragCol);
            const ti = order.indexOf(th.dataset.col);
            order.splice(ti < 0 ? order.length : ti, 0, dragCol);
            try { localStorage.setItem('mail_col_order', JSON.stringify(order)); } catch (_) {}
            dragCol = null;
            renderTable(true);
        } else if (e.type === 'dragend') {
            dragCol = null;
            el.table.querySelectorAll('.col-drop,.col-dragging').forEach(x => x.classList.remove('col-drop', 'col-dragging'));
        }
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
                if (!el.facetPanel.classList.contains('hidden')) toggleFacetPanel(false);
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
        if (Router.route === 'mail' && latest > base.latest && all > base.all) {
            showBanner(all - base.all);
        }
        // 대기/분석 중인 메일이 화면에 있거나 대기열이 바뀌면 조용히 상태만 갱신
        const sig = `${st.analysis.queued}|${st.analysis.running?.id || ''}|${st.analysis.failed}`;
        const visibleBusy = ids.some(id => ['queued', 'running'].includes(Store.get(id)?.analysis_status));
        if (Router.route === 'mail' && (sig !== lastQueueSig || visibleBusy)) silentRefresh();
        lastQueueSig = sig;
    }

    async function silentRefresh() {
        const gantt = S.view === 'gantt';
        const query = gantt
            ? queryParams({ range_from: S.from, range_to: S.to, per_page: 1000, sort: 'received' })
            : queryParams({ page: S.page, per_page: PER_PAGE });
        try {
            const data = await api('/api/emails', { query, silent: true });
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

    function onLeave() {
        Panel.close();
    }

    global.Mail = { init, apply, onStatus, onLeave, open: openMail, get state() { return S; } };
})(window);
