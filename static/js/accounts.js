/**
 * accounts.js — 설정 화면 (자동 분석, AI 모델, API 키, 분석 지침, 메일 계정)
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const PRESETS = {
        daum:    { server: 'imap.daum.net',         port: 993 },
        kakao:   { server: 'imap.kakao.com',        port: 993 },
        gmail:   { server: 'imap.gmail.com',        port: 993 },
        naver:   { server: 'imap.naver.com',        port: 993 },
        outlook: { server: 'outlook.office365.com', port: 993 },
    };
    const API_KEY_PROVIDERS = [
        { id: 'anthropic', label: 'Anthropic (Claude)', placeholder: 'sk-ant-...' },
        { id: 'openai', label: 'OpenAI (GPT)', placeholder: 'sk-...' },
    ];

    function load() {
        loadRules();
        loadAccounts();
        loadApiKeys();
        loadAnalysisPrompt();
        loadLlmSettings();
        loadAutoAnalyzeSetting();
    }

    // ─── 자동분석 토글 ───────────────────────────────────────────────────────
    function renderAutoToggle(enabled) {
        const btn = document.getElementById('auto-analyze-toggle');
        if (!btn) return;
        btn.dataset.enabled = enabled ? '1' : '0';
        btn.setAttribute('aria-checked', String(enabled));
        btn.className = 'relative shrink-0 w-12 h-7 rounded-full transition-colors ' + (enabled ? 'bg-primary' : 'bg-slate-300');
        const knob = btn.querySelector('span');
        if (knob) knob.className = 'absolute top-1 left-1 w-5 h-5 rounded-full bg-white shadow transition-transform' + (enabled ? ' translate-x-5' : '');
    }

    async function loadAutoAnalyzeSetting() {
        try { renderAutoToggle(!!(await api('/api/settings/auto-analyze', { silent: true })).enabled); } catch (_) {}
    }

    global.toggleAutoAnalyze = async function () {
        const btn = document.getElementById('auto-analyze-toggle');
        const next = btn.dataset.enabled !== '1';
        renderAutoToggle(next);
        try {
            const r = await api('/api/settings/auto-analyze', { method: 'PUT', json: { enabled: next } });
            toast(next ? `자동 분석을 켰습니다.${r.changed ? ` 최근 메일 ${r.changed}건을 대기열에 올렸습니다.` : ''}`
                       : `자동 분석을 껐습니다.${r.changed ? ` 대기 중이던 ${r.changed}건을 내렸습니다.` : ''}`, 'success');
            Bus.emit('queue-changed');
        } catch (_) {
            renderAutoToggle(!next);
        }
    };

    // ─── 분석 지침 ───────────────────────────────────────────────────────────
    async function loadAnalysisPrompt() {
        const ta = document.getElementById('settings-analysis-prompt');
        if (!ta) return;
        try { ta.value = (await api('/api/settings/prompt', { silent: true })).prompt || ''; } catch (_) {}
    }

    global.saveAnalysisPrompt = async function () {
        const ta = document.getElementById('settings-analysis-prompt');
        const status = document.getElementById('analysis-prompt-status');
        try {
            await api('/api/settings/prompt', { method: 'PUT', json: { prompt: ta.value } });
            status.textContent = '✓ 저장되었습니다. 이후 모든 분석(자동 포함)에 적용됩니다.';
            status.className = 'text-xs text-green-600';
        } catch (e) {
            status.textContent = e.message;
            status.className = 'text-xs text-red-500';
        }
    };

    // ─── API 키 ──────────────────────────────────────────────────────────────
    async function loadApiKeys() {
        const box = document.getElementById('api-keys-list');
        if (!box) return;
        let st = {};
        try { st = await api('/api/settings/keys', { silent: true }); } catch (_) {}
        box.innerHTML = API_KEY_PROVIDERS.map(p => {
            const s = st[p.id] || {};
            const badge = s.set
                ? `<span class="badge badge-green">✓ 저장됨 (${esc(s.preview || '')})</span>`
                : '<span class="badge badge-amber">미설정</span>';
            return `
              <div class="p-4 border border-outline-variant/20 rounded-lg">
                <div class="flex items-center justify-between mb-2">
                  <span class="text-sm font-semibold text-on-surface">${esc(p.label)}</span>${badge}
                </div>
                <div class="flex gap-2">
                  <input type="password" id="key-input-${p.id}" placeholder="${esc(p.placeholder)}" autocomplete="off"
                      class="flex-1 px-3 py-2 text-sm border border-outline-variant/30 rounded-lg focus:ring-2 focus:ring-primary/40"/>
                  <button type="button" data-key-save="${p.id}" class="px-4 bg-primary text-white text-sm font-semibold rounded-lg hover:bg-primary-container whitespace-nowrap">저장</button>
                  ${s.set ? `<button type="button" data-key-del="${p.id}" class="px-3 border border-red-200 text-red-600 text-sm font-medium rounded-lg hover:bg-red-50 whitespace-nowrap">삭제</button>` : ''}
                </div>
              </div>`;
        }).join('');
        box.onclick = async e => {
            const save = e.target.closest('[data-key-save]');
            const del = e.target.closest('[data-key-del]');
            if (save) {
                const id = save.dataset.keySave;
                const val = document.getElementById(`key-input-${id}`)?.value?.trim();
                if (!val) { toast('API 키를 입력하세요.', 'error'); return; }
                try {
                    await api(`/api/settings/keys/${id}`, { method: 'PUT', json: { api_key: val } });
                    toast('API 키를 저장했습니다. 멈춰 있던 분석이 다시 시작됩니다.', 'success');
                    loadApiKeys();
                    Bus.emit('queue-changed');
                } catch (_) {}
            } else if (del) {
                if (!confirm('저장된 API 키를 삭제할까요? 이 프로바이더를 쓰고 있다면 이후 분석이 멈춥니다.')) return;
                try {
                    await api(`/api/settings/keys/${del.dataset.keyDel}`, { method: 'DELETE' });
                    toast('API 키를 삭제했습니다.', 'success');
                    loadApiKeys();
                } catch (_) {}
            }
        };
    }

    // ─── LLM 설정 ────────────────────────────────────────────────────────────
    let llmOptions = { remote: [], anthropic: [], openai: [] };

    async function loadLlmSettings() {
        const prov = document.getElementById('llm-provider');
        if (!prov) return;
        try {
            const d = await api('/api/settings/llm', { silent: true });
            llmOptions = d.options || llmOptions;
            prov.value = d.provider || 'remote';
            fillModels(d.model);
        } catch (_) {}
    }

    function fillModels(selected) {
        const prov = document.getElementById('llm-provider');
        const sel = document.getElementById('llm-model');
        const list = llmOptions[prov.value] || [];
        sel.innerHTML = list.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
        if (selected && list.includes(selected)) sel.value = selected;
    }

    global.onLlmProviderChange = function () {
        fillModels(null);
        document.getElementById('llm-test-result')?.classList.add('hidden');
    };

    global.saveLlmSettings = async function () {
        const provider = document.getElementById('llm-provider').value || 'remote';
        const model = document.getElementById('llm-model').value || '';
        const status = document.getElementById('llm-settings-status');
        try {
            await api('/api/settings/llm', { method: 'PUT', json: { provider, model } });
            status.textContent = '✓ 저장되었습니다.';
            status.className = 'text-xs text-green-600';
            Bus.emit('queue-changed');
        } catch (e) {
            status.textContent = e.message;
            status.className = 'text-xs text-red-500';
        }
    };

    global.testLlmConnection = async function () {
        const provider = document.getElementById('llm-provider').value || 'remote';
        const model = document.getElementById('llm-model').value || '';
        const btn = document.getElementById('llm-test-btn');
        const out = document.getElementById('llm-test-result');
        const typedKey = document.getElementById(`key-input-${provider}`)?.value?.trim() || undefined;
        btn.disabled = true;
        out.classList.remove('hidden');
        out.className = 'mt-3 text-xs px-3 py-2 rounded-lg bg-slate-100 text-slate-500';
        out.textContent = '연결 확인 중...';
        try {
            const d = await api('/api/settings/llm/test', { method: 'POST', json: { provider, model, api_key: typedKey }, silent: true });
            out.className = `mt-3 text-xs px-3 py-2 rounded-lg ${d.ok ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`;
            out.textContent = d.ok ? `✓ 연결 성공 — ${d.provider}/${d.model} (${d.latency_ms}ms)` : `✗ 연결 실패 — ${d.message}`;
        } catch (e) {
            out.className = 'mt-3 text-xs px-3 py-2 rounded-lg bg-red-50 text-red-600';
            out.textContent = '✗ 요청 실패: ' + e.message;
        } finally {
            btn.disabled = false;
        }
    };

    // ─── 발신자 제외 규칙 ────────────────────────────────────────────────────
    let rulesBound = false;

    async function loadRules() {
        const list = document.getElementById('rules-list');
        const sug = document.getElementById('rule-suggestions');
        if (!list) return;
        if (!rulesBound) bindRules();
        let d;
        try { d = await api('/api/sender-rules', { silent: true }); }
        catch (_) { list.innerHTML = '<p class="text-sm text-red-400">규칙을 불러오지 못했습니다.</p>'; return; }

        list.innerHTML = d.rules.length ? d.rules.map(r => `
            <div class="flex items-center gap-3 px-4 py-2.5 bg-surface-container-low rounded-lg">
              <span class="material-symbols-outlined text-slate-400">${r.kind === 'domain' ? 'domain_disabled' : 'person_off'}</span>
              <div class="min-w-0 flex-1">
                <p class="text-sm font-semibold text-on-surface truncate">${esc(r.value)}</p>
                <p class="text-xs text-slate-500">${r.kind === 'domain' ? '도메인 (하위 도메인 포함)' : '발신자 주소'} · 지금 ${r.excluded_count}건 제외 중 · ${esc((r.created_at || '').slice(0, 10))} 추가</p>
              </div>
              <button type="button" data-rule-del="${r.id}" data-count="${r.excluded_count}" class="text-xs px-3 py-1.5 border border-red-200 text-red-500 rounded-lg hover:bg-red-50 shrink-0">삭제</button>
            </div>`).join('')
            : '<p class="text-center text-slate-400 py-4 text-sm">등록된 규칙이 없습니다.</p>';

        sug.innerHTML = d.suggestions.length ? `
            <p class="text-xs text-slate-500 mb-1.5">자주 오는 발신 도메인 — 업무가 아니면 눌러서 바로 추가하세요</p>
            <div class="flex flex-wrap gap-1.5">${d.suggestions.map(s =>
                `<button type="button" data-rule-suggest="${esc(s.domain)}" class="facet-chip !py-1">${esc(s.domain)} <span class="opacity-60">${s.count}</span></button>`).join('')}</div>`
            : '';
    }

    function bindRules() {
        rulesBound = true;
        document.getElementById('rule-form').addEventListener('submit', async e => {
            e.preventDefault();
            const input = document.getElementById('rule-value');
            const value = input.value.trim();
            if (!value) { input.focus(); return; }
            if (await MailActions.addRule(document.getElementById('rule-kind').value, value)) input.value = '';
        });
        document.getElementById('rule-suggestions').addEventListener('click', async e => {
            const b = e.target.closest('[data-rule-suggest]');
            if (b) await MailActions.addRule('domain', b.dataset.ruleSuggest);
        });
        document.getElementById('rules-list').addEventListener('click', async e => {
            const b = e.target.closest('[data-rule-del]');
            if (!b) return;
            const n = Number(b.dataset.count);
            if (!confirm('이 규칙을 삭제할까요? 앞으로 오는 메일은 다시 분석 대상이 됩니다.')) return;
            const restore = n > 0 && confirm(`이 규칙으로 제외됐던 메일 ${n}건도 되돌릴까요?\n(취소를 누르면 규칙만 지우고 메일은 '제외'에 그대로 둡니다)`);
            try {
                const r = await api(`/api/sender-rules/${b.dataset.ruleDel}`, { method: 'DELETE', query: { restore: restore ? 1 : '' } });
                toast(`규칙을 삭제했습니다.${r.restored ? ` 메일 ${r.restored}건을 되돌렸습니다.` : ''}`, 'success');
                Bus.emit('mail-changed', { bulk: true });
                loadRules();
            } catch (_) {}
        });
        Bus.on('rules-changed', () => { if (Router.route === 'settings') loadRules(); });
    }

    // ─── 메일 계정 ───────────────────────────────────────────────────────────
    async function loadAccounts() {
        const box = document.getElementById('accounts-list');
        if (!box) return;
        try {
            const accs = await api('/api/accounts', { silent: true });
            if (!accs.length) {
                box.innerHTML = '<p class="text-center text-slate-400 py-6 text-sm">연결된 계정이 없습니다.</p>';
                return;
            }
            box.innerHTML = accs.map(a => `
              <div class="flex items-center justify-between gap-3 p-4 bg-surface-container-low rounded-xl">
                <div class="flex items-start gap-3 min-w-0">
                  <span class="material-symbols-outlined text-primary mt-0.5">mail</span>
                  <div class="min-w-0">
                    <p class="font-semibold text-sm text-on-surface truncate">${esc(a.label || a.email_address)}</p>
                    <p class="text-xs text-slate-500 truncate">${esc(a.email_address)} · ${esc(a.imap_server)}:${Number(a.imap_port)}</p>
                    <p class="text-xs mt-0.5 ${a.last_sync_status === 'error' ? 'text-red-600' : 'text-slate-400'}">
                      ${a.syncing ? '동기화 중...' : a.last_synced ? `마지막 동기화 ${esc(a.last_synced)}${a.last_sync_status === 'ok' && a.last_sync_new ? ` · 신규 ${a.last_sync_new}건` : ''}` : '아직 동기화 전'}
                    </p>
                    ${a.last_sync_status === 'error' && a.last_sync_error ? `<p class="text-xs text-red-600 break-words">${esc(a.last_sync_error)}</p>` : ''}
                  </div>
                </div>
                <div class="flex gap-2 shrink-0">
                  <button type="button" data-acc-test="${a.id}" class="text-xs px-3 py-1.5 border border-outline-variant/30 rounded-lg hover:bg-surface-container">테스트</button>
                  <button type="button" data-acc-del="${a.id}" class="text-xs px-3 py-1.5 border border-red-200 text-red-500 rounded-lg hover:bg-red-50">삭제</button>
                </div>
              </div>`).join('');
            box.onclick = async e => {
                const test = e.target.closest('[data-acc-test]');
                const del = e.target.closest('[data-acc-del]');
                if (test) {
                    test.disabled = true;
                    try {
                        const d = await api(`/api/accounts/${test.dataset.accTest}/test`, { method: 'POST', silent: true });
                        toast(d.success ? '✓ ' + d.message : '✗ ' + d.message, d.success ? 'success' : 'error');
                    } catch (er) { toast('✗ ' + er.message, 'error'); }
                    test.disabled = false;
                } else if (del) {
                    if (!confirm('이 계정을 삭제할까요? 이미 받은 메일은 유지됩니다.')) return;
                    try { await api(`/api/accounts/${del.dataset.accDel}`, { method: 'DELETE' }); loadAccounts(); } catch (_) {}
                }
            };
        } catch (_) {
            box.innerHTML = '<p class="text-center text-red-400 py-4 text-sm">계정 목록을 불러오지 못했습니다.</p>';
        }
    }

    function form() {
        const v = id => document.getElementById(id).value;
        return {
            label: v('acc-label').trim(), email_address: v('acc-email').trim(),
            imap_server: v('acc-server').trim(), imap_port: parseInt(v('acc-port'), 10) || 993,
            imap_username: v('acc-username').trim(), imap_password: v('acc-password'), use_ssl: true,
        };
    }
    function accError(msg) {
        const el = document.getElementById('acc-error');
        el.textContent = msg;
        el.classList.toggle('hidden', !msg);
    }

    global.openAddAccountModal = function () {
        document.getElementById('add-account-modal').classList.remove('hidden');
        accError('');
        ['acc-label', 'acc-email', 'acc-server', 'acc-username', 'acc-password'].forEach(id => { document.getElementById(id).value = ''; });
        document.getElementById('acc-port').value = '993';
        document.getElementById('acc-preset').value = '';
    };
    global.closeAddAccountModal = function () {
        document.getElementById('add-account-modal').classList.add('hidden');
    };
    global.applyPreset = function () {
        const p = PRESETS[document.getElementById('acc-preset').value];
        if (!p) return;
        document.getElementById('acc-server').value = p.server;
        document.getElementById('acc-port').value = p.port;
    };
    global.testAccountConn = async function (btn) {
        const d = form();
        if (!d.imap_server || !d.imap_username || !d.imap_password) { accError('서버, 아이디, 비밀번호를 입력하세요'); return; }
        btn.disabled = true; btn.textContent = '테스트 중...';
        try {
            await api('/api/accounts', { method: 'POST', json: { ...d, _test_only: true }, silent: true });
            accError('');
            toast('✓ 연결 성공!', 'success');
        } catch (e) { accError('✗ ' + e.message); }
        finally { btn.disabled = false; btn.textContent = '연결 테스트'; }
    };
    global.submitAddAccount = async function (btn) {
        const d = form();
        if (!d.email_address || !d.imap_server || !d.imap_username || !d.imap_password) { accError('모든 필드를 입력하세요'); return; }
        btn.disabled = true; btn.textContent = '추가 중...';
        accError('');
        try {
            await api('/api/accounts', { method: 'POST', json: d, silent: true });
            global.closeAddAccountModal();
            toast('계정을 추가했습니다. 최근 메일부터 바로 받아옵니다.', 'success');
            loadAccounts();
            Status.kick();
        } catch (e) { accError(e.message); }
        finally { btn.disabled = false; btn.textContent = '추가'; }
    };

    global.Settings = { load };
})(window);
