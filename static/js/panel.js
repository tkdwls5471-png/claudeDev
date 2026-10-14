/**
 * panel.js — 메일 상세 패널 (목록 오른쪽 / 모바일 전체 화면)
 * 헤더에서 바로 완료·마감·제외·재분석·읽음·삭제. 탭: AI 분석 | 원본(누를 때만 로드) | 첨부.
 * 어떤 처리든 Store를 통해 목록·일정에 즉시 반영된다.
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;
    const PRIO_BAR = { '높음': 'bg-red-500', '보통': 'bg-amber-400', '낮음': 'bg-emerald-400' };

    let root = null;          // #mail-panel
    let cb = {};              // { onClose, onPrev, onNext, navInfo }
    let currentId = null;
    let tab = 'ai';
    let loadTimer = null;
    let loadSeq = 0;
    let lastStatus = null;
    const rawCache = new Map();   // id → {html, has_html, images_blocked, message}

    const $ = sel => root.querySelector(sel);

    function mount(el, callbacks) {
        root = el;
        cb = callbacks || {};
        root.innerHTML = `
          <div class="flex flex-col h-full min-h-0">
            <div data-ref="bar" class="h-1.5 w-full shrink-0 bg-slate-200"></div>
            <div class="px-4 lg:px-5 pt-3 pb-3 shrink-0 border-b border-outline-variant/10">
              <div class="flex items-start gap-2">
                <button type="button" data-action="close" class="lg:hidden shrink-0 w-8 h-8 -ml-1 flex items-center justify-center rounded-full hover:bg-surface-container-low" title="목록으로">
                  <span class="material-symbols-outlined">arrow_back</span></button>
                <div class="flex-1 min-w-0">
                  <div data-ref="badges" class="flex flex-wrap items-center gap-1.5 mb-1.5"></div>
                  <div class="flex items-start gap-1">
                    <h2 data-ref="title" class="font-bold text-on-surface text-base leading-snug break-words"></h2>
                    <button type="button" data-action="copy-title" class="copy-btn shrink-0 mt-0.5" title="제목 복사"><span class="material-symbols-outlined text-sm">content_copy</span></button>
                  </div>
                  <p data-ref="meta" class="text-xs text-slate-500 mt-1 break-all"></p>
                </div>
                <div class="hidden lg:flex items-center shrink-0 -mr-1">
                  <button type="button" data-action="prev" class="panel-icon-btn" title="이전 메일 (k / ↑)"><span class="material-symbols-outlined">keyboard_arrow_up</span></button>
                  <button type="button" data-action="next" class="panel-icon-btn" title="다음 메일 (j / ↓)"><span class="material-symbols-outlined">keyboard_arrow_down</span></button>
                  <button type="button" data-action="close" class="panel-icon-btn" title="닫기 (Esc)"><span class="material-symbols-outlined">close</span></button>
                </div>
              </div>
              <div data-ref="actions" class="flex flex-wrap items-center gap-2 mt-3"></div>
              <div data-ref="status" class="hidden mt-2"></div>
            </div>
            <div class="flex gap-1 px-4 lg:px-5 border-b border-outline-variant/10 shrink-0" role="tablist">
              <button type="button" class="modal-tab" data-tab="ai"><span class="material-symbols-outlined text-sm">auto_awesome</span>AI 분석</button>
              <button type="button" class="modal-tab" data-tab="raw"><span class="material-symbols-outlined text-sm">mail</span>원본</button>
              <button type="button" class="modal-tab" data-tab="att"><span class="material-symbols-outlined text-sm">attach_file</span>첨부<span data-ref="attcount"></span></button>
            </div>
            <div data-ref="body" class="flex-1 min-h-0 overflow-y-auto overscroll-contain px-4 lg:px-5 py-4">
              <div data-pane="ai"></div>
              <div data-pane="raw" class="hidden"></div>
              <div data-pane="att" class="hidden"></div>
            </div>
          </div>`;

        root.addEventListener('click', onClick);
        root.addEventListener('change', onChange);
        Bus.on('row', row => {
            if (row.id !== currentId) return;
            renderHeader();
            // 대기/분석중 → 완료·실패로 바뀌면 분석 결과를 다시 받는다
            const prev = lastStatus;
            lastStatus = row.analysis_status;
            const busy = s => s === 'queued' || s === 'running';
            if (busy(prev) && !busy(row.analysis_status)) refresh();
        });
        Bus.on('row-removed', id => { if (id === currentId) cb.onClose?.(); });
    }

    function isOpen() { return currentId != null; }

    function open(id) {
        id = Number(id);
        const changed = id !== currentId;
        currentId = id;
        root.classList.remove('hidden');
        document.body.classList.add('panel-open');
        if (changed) {
            lastStatus = Store.get(id)?.analysis_status || null;
            tab = 'ai';
            renderHeader();
            setTab('ai');
            $('[data-pane="ai"]').innerHTML = spinner('불러오는 중...');
            $('[data-pane="raw"]').innerHTML = '';
            $('[data-pane="att"]').innerHTML = '';
            $('[data-ref="body"]').scrollTop = 0;
        }
        // j/k로 빠르게 넘길 때 요청이 몰리지 않도록 잠깐 기다렸다가 상세를 받는다
        clearTimeout(loadTimer);
        const cached = Store.getDetail(id);
        if (cached && changed) renderDetail(cached);
        loadTimer = setTimeout(() => loadDetail(id), cached ? 0 : 120);
    }

    function close() {
        currentId = null;
        clearTimeout(loadTimer);
        root?.classList.add('hidden');
        document.body.classList.remove('panel-open');
    }

    async function loadDetail(id, { quiet = false } = {}) {
        const seq = ++loadSeq;
        try {
            const d = await api(`/api/emails/${id}`, { query: { mark_read: 1 }, silent: quiet });
            if (seq !== loadSeq || id !== currentId) return;
            Store.setDetail(d);
            renderDetail(d);
            Bus.emit('mail-changed', { ids: [id], read: true });
        } catch (e) {
            if (id === currentId && !quiet) {
                $('[data-pane="ai"]').innerHTML = `<p class="text-center text-red-500 text-sm py-10">${esc(e.message)}</p>`;
            }
        }
    }

    /** 분석이 끝났거나 재처리 후 상세를 다시 받는다 */
    function refresh() { if (currentId != null) { Store.dropDetail(currentId); loadDetail(currentId, { quiet: true }); } }

    // ─── 헤더 ────────────────────────────────────────────────────────────────
    function renderHeader() {
        const r = Store.get(currentId);
        if (!r) return;
        const d = Store.getDetail(currentId) || {};
        $('[data-ref="bar"]').className = `h-1.5 w-full shrink-0 ${r.is_completed || r.is_excluded ? 'bg-slate-300' : (PRIO_BAR[r.priority] || 'bg-slate-200')}`;
        $('[data-ref="title"]').textContent = r.subject || '(제목 없음)';
        const meta = [r.sender, r.company && !String(r.sender || '').includes(r.company) ? r.company : '', r.received_at]
            .filter(Boolean).map(esc).join(' · ');
        $('[data-ref="meta"]').innerHTML = meta;
        $('[data-ref="badges"]').innerHTML = [
            r.is_completed ? '<span class="badge badge-green"><span class="material-symbols-outlined text-[12px]">check_circle</span>완료</span>' : '',
            r.is_excluded ? '<span class="badge badge-slate"><span class="material-symbols-outlined text-[12px]">visibility_off</span>제외</span>' : '',
            Badges.source(r), Badges.priority(r.priority), Badges.type(r.req_type),
        ].join('');

        const busy = r.analysis_status === 'queued' || r.analysis_status === 'running';
        const sp = parseSender(r.sender);
        const aiDeadline = (d.analysis_result || {}).마감일_원문 || (d.analysis_result || {}).마감일 || '';
        $('[data-ref="actions"]').innerHTML = `
          <button type="button" data-action="complete" class="panel-btn ${r.is_completed ? 'panel-btn-on-green' : 'panel-btn-green'}" title="완료 (e)">
            <span class="material-symbols-outlined text-base">${r.is_completed ? 'task_alt' : 'check_circle'}</span>${r.is_completed ? '완료됨' : '완료'}</button>
          <label class="panel-btn panel-btn-plain gap-1.5 pr-1.5" title="마감일 (d)">
            <span class="material-symbols-outlined text-base">event</span>
            <input type="date" data-ref="deadline" value="${esc(r.deadline || '')}" class="bg-transparent border-0 p-0 text-sm focus:ring-0 w-[8.5rem] cursor-pointer"/>
            ${r.deadline ? `<span class="text-[11px] ${(DateUtil.daysUntil(r.deadline) ?? 1) < 0 && !r.is_completed ? 'text-red-500' : 'text-slate-400'}">${esc(DateUtil.ddayLabel(r.deadline))}</span>` : ''}
          </label>
          <button type="button" data-action="exclude" class="panel-btn ${r.is_excluded ? 'panel-btn-on' : 'panel-btn-plain'}" title="제외 (x)">
            <span class="material-symbols-outlined text-base">${r.is_excluded ? 'restore_from_trash' : 'visibility_off'}</span>${r.is_excluded ? '제외 해제' : '제외'}</button>
          <button type="button" data-action="analyze" class="panel-btn panel-btn-plain" ${busy ? 'disabled' : ''} title="AI 분석 다시 하기">
            <span class="material-symbols-outlined text-base">auto_awesome</span>${r.analysis_status === 'done' ? '재분석' : '분석'}</button>
          <button type="button" data-action="read" class="panel-btn panel-btn-plain" title="읽음/안읽음 (u)">
            <span class="material-symbols-outlined text-base">${r.is_read ? 'mark_email_unread' : 'drafts'}</span>${r.is_read ? '안읽음' : '읽음'}</button>
          <div class="relative ml-auto">
            <button type="button" data-action="more" class="panel-icon-btn" title="더보기"><span class="material-symbols-outlined">more_horiz</span></button>
            <div data-ref="menu" class="hidden absolute right-0 top-full mt-1 z-30 w-60 py-1 bg-surface-container-lowest rounded-xl shadow-xl border border-outline-variant/20 text-sm">
              <button type="button" data-action="copy-sender" class="menu-item"><span class="material-symbols-outlined text-base">content_copy</span>발신자 복사</button>
              ${sp.domain ? `<button type="button" data-action="rule-domain" class="menu-item" title="${esc(sp.domain)}"><span class="material-symbols-outlined text-base">domain_disabled</span><span class="min-w-0"><span class="block">이 도메인 항상 제외</span><span class="block text-[11px] text-slate-400 truncate">${esc(sp.domain)}</span></span></button>` : ''}
              ${sp.address ? `<button type="button" data-action="rule-sender" class="menu-item" title="${esc(sp.address)}"><span class="material-symbols-outlined text-base">person_off</span><span class="min-w-0"><span class="block">이 발신자 항상 제외</span><span class="block text-[11px] text-slate-400 truncate">${esc(sp.address)}</span></span></button>` : ''}
              ${d.can_reprocess ? '<button type="button" data-action="reprocess" class="menu-item"><span class="material-symbols-outlined text-base">refresh</span>원본 다시 처리</button>' : ''}
              <button type="button" data-action="delete" class="menu-item text-red-600"><span class="material-symbols-outlined text-base">delete</span>앱에서 삭제</button>
            </div>
          </div>
          ${r.exclude_reason ? `<p class="w-full text-[11px] text-slate-500 flex items-center gap-1">
              <span class="material-symbols-outlined text-sm">filter_alt</span>자동 제외됨 · ${esc(r.exclude_reason)}
              <button type="button" data-action="goto-rules" class="underline underline-offset-2 hover:text-primary ml-1">규칙 관리</button></p>` : ''}
          ${r.deadline_manual && aiDeadline ? `<p class="w-full text-[11px] text-slate-400">AI가 찾은 마감: ${esc(aiDeadline)}
              <button type="button" data-action="reset-deadline" class="underline underline-offset-2 hover:text-primary ml-1">AI 값으로 되돌리기</button></p>`
            : (!r.deadline_manual && aiDeadline && !r.deadline ? `<p class="w-full text-[11px] text-slate-400">AI가 찾은 마감 표현: ${esc(aiDeadline)}</p>` : '')}`;

        const st = $('[data-ref="status"]');
        const s = r.analysis_status;
        if (s === 'queued' || s === 'running' || s === 'failed' || (s === 'done' && r.analysis_error)) {
            st.classList.remove('hidden');
            const tone = s === 'failed' ? 'bg-red-50 text-red-700' : s === 'done' ? 'bg-amber-50 text-amber-700' : 'bg-blue-50 text-blue-700';
            const text = s === 'running' ? 'AI가 분석 중입니다. 끝나면 자동으로 갱신됩니다.'
                : s === 'queued' ? (r.analysis_retry_count ? `재시도 대기 중${r.analysis_next_at ? ` (${r.analysis_next_at.slice(11, 16)} 예정)` : ''}` : '분석 대기 중입니다.')
                : s === 'failed' ? '분석에 실패했습니다.' : '재분석에 실패해 이전 결과를 보여 줍니다.';
            st.innerHTML = `<div class="rounded-lg px-3 py-2 text-xs ${tone} flex items-start gap-2">
                <span class="material-symbols-outlined text-base">${s === 'running' ? 'progress_activity' : s === 'queued' ? 'schedule' : 'error'}</span>
                <div class="flex-1 min-w-0"><p class="font-semibold">${text}</p>
                  ${r.analysis_error && s !== 'running' ? `<p class="mt-0.5 break-words opacity-90">${esc(r.analysis_error)}</p>` : ''}</div>
                ${s === 'failed' || (s === 'done' && r.analysis_error) ? '<button type="button" data-action="analyze" class="shrink-0 font-semibold underline underline-offset-2">다시 분석</button>' : ''}
              </div>`;
        } else {
            st.classList.add('hidden');
            st.innerHTML = '';
        }

        const nav = cb.navInfo?.(currentId) || {};
        root.querySelectorAll('[data-action="prev"]').forEach(b => { b.disabled = !nav.hasPrev; });
        root.querySelectorAll('[data-action="next"]').forEach(b => { b.disabled = !nav.hasNext; });
    }

    // ─── 탭 ──────────────────────────────────────────────────────────────────
    function setTab(t) {
        tab = t;
        root.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
        root.querySelectorAll('[data-pane]').forEach(p => p.classList.toggle('hidden', p.dataset.pane !== t));
        const d = Store.getDetail(currentId);
        if (t === 'raw') renderRaw(d);
        if (t === 'att') renderAttachments(d);
    }

    function renderDetail(d) {
        renderHeader();
        const n = (d.attachments || []).filter(a => a.type !== 'unsupported' || a.has_original).length;
        $('[data-ref="attcount"]').textContent = n ? `(${n})` : '';
        renderAi(d);
        if (tab === 'raw') renderRaw(d);
        if (tab === 'att') renderAttachments(d);
    }

    function renderAi(d) {
        const el = $('[data-pane="ai"]');
        const res = d.analysis_result || {};
        const has = !!(res.요구사항_요약 || (res.상세_요구사항 || []).length);
        if (!has) {
            const busy = d.analysis_status === 'queued' || d.analysis_status === 'running';
            el.innerHTML = `
              <div class="flex flex-col items-center justify-center py-14 gap-3 text-center">
                <span class="material-symbols-outlined text-5xl text-slate-300">${busy ? 'hourglass_top' : 'auto_awesome'}</span>
                <p class="text-sm text-slate-500">${busy ? 'AI 분석을 기다리는 중입니다.' : '아직 AI 분석 결과가 없습니다.'}</p>
                ${busy ? '' : '<button type="button" data-action="analyze" class="px-5 py-2 bg-primary text-on-primary text-sm font-semibold rounded-xl hover:bg-primary-container active:scale-95">지금 분석하기</button>'}
                <button type="button" data-tab-go="raw" class="text-xs text-slate-400 hover:text-slate-600 underline underline-offset-2">원본 보기</button>
              </div>`;
            return;
        }
        const items = Array.isArray(res.상세_요구사항) ? res.상세_요구사항 : [];
        const accent = { '높음': '#ef4444', '보통': '#f59e0b', '낮음': '#10b981' }[d.priority] || '#94a3b8';
        const cell = (label, value, copy) => `
            <div class="rounded-xl bg-slate-50 px-3 py-2.5 min-w-0">
              <p class="text-[10px] font-semibold tracking-wider text-slate-400 uppercase mb-0.5 flex items-center gap-1">${label}
                ${copy && value ? `<button type="button" data-action="copy" data-text="${esc(value)}" class="copy-btn !w-4 !h-4" title="복사"><span class="material-symbols-outlined text-[12px]">content_copy</span></button>` : ''}</p>
              <p class="text-sm font-semibold text-slate-700 break-words">${esc(value || '—')}</p>
            </div>`;
        el.innerHTML = `
          <div class="space-y-4">
            <div class="grid grid-cols-2 gap-2">
              ${cell('요청 업체', res.요청_업체, true)}
              ${cell('마감 (원문)', res.마감일_원문 || res.마감일)}
              ${cell('유형', d.req_type || res.요청_유형)}
              ${cell('우선순위', d.priority || res.우선순위)}
            </div>
            ${res.요구사항_요약 ? `
            <div class="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3">
              <p class="text-[11px] font-bold tracking-wider text-slate-400 uppercase mb-1.5 flex items-center gap-1"><span class="material-symbols-outlined text-sm">auto_awesome</span>AI 요약</p>
              <p class="text-sm leading-relaxed text-slate-700 whitespace-pre-line">${esc(res.요구사항_요약)}</p>
            </div>` : ''}
            ${items.length ? `
            <div>
              <p class="text-[11px] font-bold tracking-wider text-slate-400 uppercase mb-2">상세 요구사항 <span class="badge badge-slate ml-1">${items.length}</span></p>
              <ol class="space-y-1.5">
                ${items.map((it, i) => `
                  <li class="flex gap-3 rounded-xl bg-slate-50 px-3 py-2.5">
                    <span class="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-black text-white mt-0.5" style="background:${accent}">${i + 1}</span>
                    <p class="text-sm text-slate-700 leading-relaxed break-words">${esc(it)}</p>
                  </li>`).join('')}
              </ol>
            </div>` : ''}
            ${res.추가_참고사항 ? `
            <div class="rounded-xl bg-amber-50 border border-amber-100 px-4 py-3">
              <p class="text-[11px] font-bold tracking-wider text-amber-600 uppercase mb-1 flex items-center gap-1"><span class="material-symbols-outlined text-sm">info</span>추가 참고사항</p>
              <p class="text-sm text-amber-800 leading-relaxed whitespace-pre-line">${esc(res.추가_참고사항)}</p>
            </div>` : ''}
            <p class="text-[11px] text-slate-400">${esc([d.analysis_model, d.analyzed_at && `${d.analyzed_at} 분석`,
                (d.tokens_in || d.tokens_out) && `토큰 ${d.tokens_in}/${d.tokens_out}`].filter(Boolean).join(' · '))}</p>
          </div>`;
    }

    async function renderRaw(d) {
        const el = $('[data-pane="raw"]');
        if (!d) { el.innerHTML = spinner('불러오는 중...'); return; }
        const id = d.id;
        const info = [['발신', d.sender], ['수신', d.recipient], ['수신일시', d.received_at]].filter(([, v]) => v);
        el.innerHTML = `
          <div class="rounded-xl bg-slate-50 px-4 py-3 space-y-1 mb-3">
            ${info.map(([k, v]) => `<div class="flex gap-2 text-sm"><span class="text-[11px] font-bold text-slate-400 w-14 shrink-0 pt-0.5">${k}</span><span class="text-slate-700 break-all">${esc(v)}</span></div>`).join('')}
          </div>
          <div data-ref="rawbody">${d.has_html === false ? '' : spinner('원본 불러오는 중...')}</div>`;
        const box = el.querySelector('[data-ref="rawbody"]');
        const showText = (note) => {
            box.innerHTML = (note ? `<p class="text-xs text-slate-400 mb-2">${esc(note)}</p>` : '')
                + `<div class="rounded-xl bg-white border border-slate-100 px-4 py-3 text-sm text-slate-700 leading-relaxed whitespace-pre-wrap break-words">${
                    d.body_text ? esc(d.body_text) : '<span class="text-slate-400">본문이 없습니다.</span>'}</div>`;
        };
        if (d.has_html === false) { showText(); return; }
        let data = rawCache.get(id);
        if (!data) {
            try {
                data = await api(`/api/emails/${id}/body-html`, { query: { images: 0 }, silent: true });
            } catch (e) {
                data = { html: null, message: e.message };
            }
            rawCache.set(id, data);
            if (rawCache.size > 10) rawCache.delete(rawCache.keys().next().value);
        }
        if (currentId !== id || tab !== 'raw') return;
        if (!data.html) { showText(data.message && data.has_html !== false ? data.message : ''); return; }
        const blocked = /data-blocked-src=/.test(data.html);
        box.innerHTML = blocked
            ? `<div class="flex items-center gap-2 text-xs text-slate-500 bg-slate-50 rounded-lg px-3 py-2 mb-2">
                 <span class="material-symbols-outlined text-base">image_not_supported</span>외부 이미지를 차단했습니다 (수신 확인 추적 방지).
                 <button type="button" data-action="show-images" class="ml-auto font-semibold text-primary hover:underline">이미지 표시</button></div><div data-ref="frame"></div>`
            : '<div data-ref="frame"></div>';
        renderSandboxedHtml(box.querySelector('[data-ref="frame"]'), data.html, { minHeight: 200 });
    }

    function showRemoteImages() {
        const data = rawCache.get(currentId);
        const box = $('[data-pane="raw"] [data-ref="rawbody"]');
        if (!data?.html || !box) return;
        const doc = new DOMParser().parseFromString(data.html, 'text/html');
        doc.querySelectorAll('img[data-blocked-src]').forEach(img => {
            const src = img.getAttribute('data-blocked-src');
            img.setAttribute('src', src.startsWith('//') ? 'https:' + src : src);
            img.removeAttribute('alt');
        });
        box.innerHTML = '<div data-ref="frame"></div>';
        renderSandboxedHtml(box.querySelector('[data-ref="frame"]'), doc.body.innerHTML, { allowRemoteImages: true, minHeight: 200 });
    }

    async function renderAttachments(d) {
        const el = $('[data-pane="att"]');
        if (!d) { el.innerHTML = spinner('불러오는 중...'); return; }
        // 옛 형식으로 저장된 첨부(엑셀이 표로 안 보이는 등)는 열 때 한 번 자동 재처리
        if (d.can_reprocess && Attachments.hasLegacy(d.attachments) && !d._reprocessed) {
            d._reprocessed = true;
            await reprocess(true);
            return;
        }
        Attachments.render(el, {
            emailId: d.id, attachments: d.attachments, canReprocess: d.can_reprocess,
            onReprocess: () => reprocess(false),
        });
    }

    async function reprocess(auto) {
        const id = currentId;
        const el = $('[data-pane="att"]');
        el.innerHTML = spinner('원본을 다시 처리하는 중...');
        try {
            const r = await api(`/api/emails/${id}/reprocess`, { method: 'POST', silent: auto });
            const d = Store.getDetail(id);
            if (d) { d.attachments = r.attachments || []; d._reprocessed = true; }
            rawCache.delete(id);
            if (!auto) toast('첨부파일을 최신 형식으로 갱신했습니다.', 'success');
        } catch (_) { /* 실패해도 기존 첨부를 보여 준다 */ }
        if (id === currentId) {
            const d = Store.getDetail(id);
            if (d) Attachments.render(el, { emailId: id, attachments: d.attachments, canReprocess: d.can_reprocess, onReprocess: () => reprocess(false) });
        }
    }

    // ─── 이벤트 ──────────────────────────────────────────────────────────────
    function onClick(e) {
        const tabBtn = e.target.closest('[data-tab]');
        if (tabBtn) { setTab(tabBtn.dataset.tab); return; }
        const go = e.target.closest('[data-tab-go]');
        if (go) { setTab(go.dataset.tabGo); return; }
        const t = e.target.closest('[data-action]');
        const menu = $('[data-ref="menu"]');
        if (!t || t.dataset.action !== 'more') menu?.classList.add('hidden');
        if (!t) return;
        const id = currentId;
        switch (t.dataset.action) {
            case 'close': cb.onClose?.(); break;
            case 'prev': cb.onPrev?.(); break;
            case 'next': cb.onNext?.(); break;
            case 'complete': MailActions.toggleComplete(id); break;
            case 'exclude': MailActions.toggleExclude(id); break;
            case 'read': MailActions.toggleRead(id); break;
            case 'analyze': MailActions.analyze(id); break;
            case 'reset-deadline': MailActions.resetDeadline(id); break;
            case 'more': menu?.classList.toggle('hidden'); break;
            case 'delete': MailActions.remove(id); break;
            case 'reprocess': setTab('att'); reprocess(false); break;
            case 'copy-title': copyToClipboard(Store.get(id)?.subject, t); break;
            case 'copy-sender': copyToClipboard(Store.get(id)?.sender, t); break;
            case 'copy': copyToClipboard(t.dataset.text, t); break;
            case 'show-images': showRemoteImages(); break;
            case 'rule-domain':
            case 'rule-sender': {
                const sp = parseSender(Store.get(id)?.sender);
                const kind = t.dataset.action === 'rule-domain' ? 'domain' : 'sender';
                MailActions.addRule(kind, kind === 'domain' ? sp.domain : sp.address).then(ok => {
                    if (ok) api(`/api/emails/${id}`, { silent: true }).then(row => Store.upsert(row)).catch(() => {});
                });
                break;
            }
            case 'goto-rules': Router.go('settings'); setTimeout(() => document.getElementById('sender-rules-card')?.scrollIntoView({ behavior: 'smooth' }), 300); break;
        }
    }

    function onChange(e) {
        if (e.target.matches('[data-ref="deadline"]')) {
            MailActions.setDeadline(currentId, e.target.value || null);
        }
    }

    function focusDeadline() {
        const input = $('[data-ref="deadline"]');
        if (!input) return;
        input.focus();
        try { input.showPicker(); } catch (_) {}
    }

    function spinner(text) {
        return `<div class="flex flex-col items-center justify-center py-14 gap-3">
            <div class="w-8 h-8 border-2 border-slate-200 border-t-slate-500 rounded-full animate-spin"></div>
            <p class="text-sm text-slate-400">${esc(text)}</p></div>`;
    }

    global.Panel = {
        mount, open, close, isOpen, refresh, focusDeadline,
        get currentId() { return currentId; },
        renderHeader,
    };
})(window);
