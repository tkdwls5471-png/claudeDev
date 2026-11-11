/**
 * settings.js — 설정 화면 (#settings?tab=accounts|ai|rules)
 *  - 메일 계정: 목록·테스트·삭제, 추가 모달(이메일 주소로 서버 자동 채움)
 *  - AI 분석: 자동 분석, 프로바이더·모델(+ 고른 프로바이더의 API 키), 분석 지침
 *  - 제외 규칙: 발신자·도메인 규칙과 추천 도메인
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    const TABS = ['accounts', 'ai', 'rules'];
    // 메일 도메인 → IMAP 서버
    const PRESETS = {
        'daum.net': 'imap.daum.net', 'hanmail.net': 'imap.daum.net',
        'kakao.com': 'imap.kakao.com',
        'gmail.com': 'imap.gmail.com',
        'naver.com': 'imap.naver.com',
        'outlook.com': 'outlook.office365.com', 'hotmail.com': 'outlook.office365.com',
    };
    const KEY_PROVIDERS = {
        anthropic: { label: 'Anthropic API 키', placeholder: 'sk-ant-...' },
        openai: { label: 'OpenAI API 키', placeholder: 'sk-...' },
    };

    let tab = 'accounts';
    const loaded = new Set();     // 이번 방문에서 불러온 탭

    function apply(params, entering) {
        const t = params.get('tab');
        tab = TABS.includes(t) ? t : 'accounts';
        if (entering) loaded.clear();
        document.querySelectorAll('[data-settings-tab]').forEach(b => b.classList.toggle('active', b.dataset.settingsTab === tab));
        document.querySelectorAll('[data-settings-pane]').forEach(p => p.classList.toggle('hidden', p.dataset.settingsPane !== tab));
        if (loaded.has(tab)) return;
        loaded.add(tab);
        if (tab === 'accounts') loadAccounts();
        else if (tab === 'ai') { loadAutoAnalyze(); loadLlmSettings(); loadAnalysisPrompt(); }
        else loadRules();
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('settings-tabs')?.addEventListener('click', e => {
            const b = e.target.closest('[data-settings-tab]');
            if (b) Router.go(`settings?tab=${b.dataset.settingsTab}`, { replace: true });
        });
        bindRules();
        bindAccountModal();
    });

    function setStatus(id, ok, text) {
        const s = document.getElementById(id);
        s.textContent = text;
        s.className = `text-xs ${ok ? 'text-green-600' : 'text-red-500'}`;
    }

    // ─── 자동 분석 ───────────────────────────────────────────────────────────
    function renderAutoToggle(enabled) {
        const btn = document.getElementById('auto-analyze-toggle');
        btn.dataset.enabled = enabled ? '1' : '0';
        btn.setAttribute('aria-checked', String(enabled));
        btn.className = 'relative shrink-0 w-12 h-7 rounded-full transition-colors ' + (enabled ? 'bg-primary' : 'bg-slate-300');
        btn.querySelector('span').className = 'absolute top-1 left-1 w-5 h-5 rounded-full bg-white shadow transition-transform' + (enabled ? ' translate-x-5' : '');
    }

    async function loadAutoAnalyze() {
        try { renderAutoToggle(!!(await api('/api/settings/auto-analyze', { silent: true })).enabled); } catch (_) {}
    }

    global.toggleAutoAnalyze = async function () {
        const next = document.getElementById('auto-analyze-toggle').dataset.enabled !== '1';
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
        try { document.getElementById('settings-analysis-prompt').value = (await api('/api/settings/prompt', { silent: true })).prompt || ''; } catch (_) {}
    }

    global.saveAnalysisPrompt = async function () {
        try {
            await api('/api/settings/prompt', { method: 'PUT', json: { prompt: document.getElementById('settings-analysis-prompt').value } });
            setStatus('analysis-prompt-status', true, '✓ 저장했습니다. 이후 모든 분석에 적용됩니다.');
        } catch (e) {
            setStatus('analysis-prompt-status', false, e.message);
        }
    };

    // ─── AI 모델 + API 키 ────────────────────────────────────────────────────
    let llmOptions = { remote: [], anthropic: [], openai: [] };
    let keyState = {};

    async function loadLlmSettings() {
        const [llm, keys] = await Promise.all([
            api('/api/settings/llm', { silent: true }).catch(() => null),
            api('/api/settings/keys', { silent: true }).catch(() => ({})),
        ]);
        keyState = keys || {};
        if (llm) {
            llmOptions = llm.options || llmOptions;
            document.getElementById('llm-provider').value = llm.provider || 'remote';
            fillModels(llm.model);
        }
        renderKeyRow();
    }

    function provider() { return document.getElementById('llm-provider').value || 'remote'; }

    function fillModels(selected) {
        const sel = document.getElementById('llm-model');
        const list = llmOptions[provider()] || [];
        sel.innerHTML = list.length ? list.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')
            : '<option value="">(조회된 모델 없음)</option>';
        if (selected && list.includes(selected)) sel.value = selected;
    }

    /** 고른 프로바이더가 API 키를 쓰면 그 키 하나만 보여 준다 */
    function renderKeyRow() {
        const box = document.getElementById('api-key-row');
        const p = KEY_PROVIDERS[provider()];
        box.classList.toggle('hidden', !p);
        if (!p) { box.innerHTML = ''; return; }
        const s = keyState[provider()] || {};
        box.innerHTML = `
          <label class="field-label" for="key-input">${esc(p.label)}
            ${s.set ? `<span class="badge badge-green ml-1">저장됨 ${esc(s.preview || '')}</span>` : '<span class="badge badge-amber ml-1">미설정</span>'}</label>
          <div class="flex gap-2">
            <input type="password" id="key-input" placeholder="${s.set ? '바꾸려면 새 키 입력' : esc(p.placeholder)}" autocomplete="off" class="field-input flex-1"/>
            ${s.set ? '<button type="button" data-key-del class="btn-plain !text-red-600">삭제</button>' : ''}
          </div>
          <p class="text-[11px] text-slate-400 mt-1">DB에 암호화해 저장하며 브라우저에는 남기지 않습니다. 아래 '저장'을 누르면 함께 저장됩니다.</p>`;
        box.querySelector('[data-key-del]')?.addEventListener('click', deleteKey);
    }

    async function deleteKey() {
        if (!confirm('저장된 API 키를 삭제할까요? 이 프로바이더를 쓰고 있다면 이후 분석이 멈춥니다.')) return;
        try {
            await api(`/api/settings/keys/${provider()}`, { method: 'DELETE' });
            keyState[provider()] = { set: false };
            renderKeyRow();
            toast('API 키를 삭제했습니다.', 'success');
        } catch (_) {}
    }

    global.onLlmProviderChange = function () {
        fillModels(null);
        renderKeyRow();
        document.getElementById('llm-test-result').classList.add('hidden');
    };

    global.saveLlmSettings = async function () {
        const p = provider();
        const key = document.getElementById('key-input')?.value.trim();
        try {
            if (key) {
                const r = await api(`/api/settings/keys/${p}`, { method: 'PUT', json: { api_key: key } });
                keyState[p] = { set: true, preview: r.preview };
            }
            await api('/api/settings/llm', { method: 'PUT', json: { provider: p, model: document.getElementById('llm-model').value || '' } });
            renderKeyRow();
            setStatus('llm-settings-status', true, '✓ 저장했습니다. 멈춰 있던 분석이 있으면 다시 시작합니다.');
            Bus.emit('queue-changed');
        } catch (e) {
            setStatus('llm-settings-status', false, e.message);
        }
    };

    global.testLlmConnection = async function () {
        const btn = document.getElementById('llm-test-btn');
        const out = document.getElementById('llm-test-result');
        const json = {
            provider: provider(), model: document.getElementById('llm-model').value || '',
            api_key: document.getElementById('key-input')?.value.trim() || undefined,
        };
        btn.disabled = true;
        out.className = 'mt-3 text-xs px-3 py-2 rounded-lg bg-slate-100 text-slate-500';
        out.textContent = '연결 확인 중...';
        try {
            const d = await api('/api/settings/llm/test', { method: 'POST', json, silent: true });
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
    async function loadRules() {
        const list = document.getElementById('rules-list');
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

        document.getElementById('rule-suggestions').innerHTML = d.suggestions.length ? `
            <p class="text-xs text-slate-500 mb-1.5">자주 오는 발신 도메인 — 업무 메일이 아니면 눌러서 바로 추가하세요</p>
            <div class="flex flex-wrap gap-1.5">${d.suggestions.map(s =>
                `<button type="button" data-rule-suggest="${esc(s.domain)}" class="facet-chip !py-1">${esc(s.domain)} <span class="opacity-60">${s.count}</span></button>`).join('')}</div>`
            : '';
    }

    function bindRules() {
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
        Bus.on('rules-changed', () => { if (Router.route === 'settings' && tab === 'rules') loadRules(); else loaded.delete('rules'); });
    }

    // ─── 메일 계정 ───────────────────────────────────────────────────────────
    async function loadAccounts() {
        const box = document.getElementById('accounts-list');
        try {
            const accs = await api('/api/accounts', { silent: true });
            if (!accs.length) {
                box.innerHTML = `<div class="text-center py-8">
                    <p class="text-slate-500 text-sm mb-3">연결된 메일 계정이 없습니다.</p>
                    <button type="button" onclick="openAddAccountModal()" class="btn-primary mx-auto"><span class="material-symbols-outlined text-sm">add</span>첫 계정 추가</button></div>`;
                return;
            }
            box.innerHTML = accs.map(a => `
              <div class="flex items-center justify-between gap-3 p-4 bg-surface-container-low rounded-xl">
                <div class="flex items-start gap-3 min-w-0">
                  <span class="status-dot mt-1.5 ${a.last_sync_status === 'error' ? 'status-dot-error' : a.last_sync_status === 'ok' ? 'status-dot-ok' : 'status-dot-idle'}"></span>
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
                        toast((d.success ? '✓ ' : '✗ ') + d.message, d.success ? 'success' : 'error');
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

    const $v = id => document.getElementById(id);

    function form() {
        return {
            label: $v('acc-label').value.trim(), email_address: $v('acc-email').value.trim(),
            imap_server: $v('acc-server').value.trim(), imap_port: parseInt($v('acc-port').value, 10) || 993,
            imap_username: $v('acc-username').value.trim(), imap_password: $v('acc-password').value, use_ssl: true,
        };
    }
    function accError(msg) {
        $v('acc-error').textContent = msg;
        $v('acc-error').classList.toggle('hidden', !msg);
    }

    /** 이메일 주소를 입력하면 서버·아이디를 채운다 (사용자가 직접 고친 값은 건드리지 않음) */
    function bindAccountModal() {
        $v('acc-email').addEventListener('input', () => {
            const email = $v('acc-email').value.trim();
            const domain = email.split('@')[1]?.toLowerCase() || '';
            const server = $v('acc-server'), user = $v('acc-username');
            if (!server.dataset.touched) server.value = PRESETS[domain] || (domain ? `imap.${domain}` : '');
            if (!user.dataset.touched) user.value = email;
        });
        ['acc-server', 'acc-username'].forEach(id => $v(id).addEventListener('input', e => { e.target.dataset.touched = '1'; }));
    }

    global.openAddAccountModal = function () {
        $v('add-account-modal').classList.remove('hidden');
        accError('');
        ['acc-label', 'acc-email', 'acc-server', 'acc-username', 'acc-password'].forEach(id => {
            $v(id).value = '';
            delete $v(id).dataset.touched;
        });
        $v('acc-port').value = '993';
        $v('acc-email').focus();
    };
    global.closeAddAccountModal = function () {
        $v('add-account-modal').classList.add('hidden');
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
        if (!d.email_address || !d.imap_server || !d.imap_username || !d.imap_password) { accError('이메일 주소, 비밀번호, 서버, 아이디를 입력하세요'); return; }
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

    global.Settings = { apply };
})(window);
