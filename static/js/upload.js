/**
 * upload.js — 대시보드의 EML 업로드
 * 파일을 하나씩 서버 메일함(가상 '업로드' 계정)에 저장하고 분석 대기열에 올린다.
 * 결과는 메일 화면에서 대기 → 분석중 → 완료로 이어서 볼 수 있다.
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;
    let busy = false;

    function init() {
        const zone = document.getElementById('upload-zone');
        const input = document.getElementById('file-input');
        if (!zone || !input) return;
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); }));
        ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, () => zone.classList.add('upload-zone-active')));
        ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove('upload-zone-active')));
        zone.addEventListener('drop', e => send([...e.dataTransfer.files]));
        zone.addEventListener('click', e => { if (!e.target.closest('a,button[data-action]')) input.click(); });
        input.addEventListener('change', () => { send([...input.files]); input.value = ''; });
        document.getElementById('upload-result')?.addEventListener('click', e => {
            if (e.target.closest('[data-action="open-uploads"]')) Router.go('mail?tab=all&acct=upload');
        });
    }

    async function send(files) {
        if (busy) { toast('이전 업로드가 끝난 뒤 다시 시도하세요.', 'error'); return; }
        const emls = files.filter(f => f.name.toLowerCase().endsWith('.eml'));
        if (!emls.length) { toast('EML 파일을 선택해 주세요.', 'error'); return; }
        busy = true;
        const status = document.getElementById('upload-status');
        const result = document.getElementById('upload-result');
        const created = [], dups = [], errors = [];
        try {
            for (let i = 0; i < emls.length; i++) {
                const f = emls[i];
                status.textContent = `업로드 중 ${i + 1} / ${emls.length} · ${f.name}`;
                const form = new FormData();
                form.append('file', f, f.name);
                try {
                    const r = await api('/api/upload', { method: 'POST', form, silent: true });
                    if (r.created && r.success) { created.push(r.created); Store.upsert(r.created); }
                    else if (r.duplicate) dups.push(f.name);
                    else errors.push({ filename: f.name, message: r.error?.message || '저장 실패' });
                } catch (e) {
                    errors.push({ filename: f.name, message: e.message });
                }
            }
        } finally {
            busy = false;
            status.textContent = '';
        }
        Bus.emit('queue-changed');
        Bus.emit('mail-changed', {});
        const lines = [];
        if (created.length) lines.push(`<p class="text-green-700"><b>${created.length}건</b>을 저장했고 분석 대기열에 올렸습니다.</p>`);
        if (dups.length) lines.push(`<p class="text-slate-500">이미 있는 메일 ${dups.length}건은 건너뛰었습니다: ${esc(dups.slice(0, 5).join(', '))}${dups.length > 5 ? ' 외' : ''}</p>`);
        errors.forEach(er => lines.push(`<p class="text-red-600">${esc(er.filename)}: ${esc(er.message)}</p>`));
        result.innerHTML = `<div class="rounded-xl border border-outline-variant/20 bg-surface-container-lowest px-4 py-3 text-sm space-y-1">
            ${lines.join('')}
            <button type="button" data-action="open-uploads" class="mt-1 inline-flex items-center gap-1 text-primary font-semibold hover:underline">
              <span class="material-symbols-outlined text-base">inbox</span>메일 화면에서 보기</button></div>`;
        result.classList.remove('hidden');
        if (created.length && !errors.length) toast(`${created.length}건 업로드 완료 · 분석을 시작합니다.`, 'success');
        else if (errors.length) toast(`${errors.length}건은 저장하지 못했습니다.`, 'error');
    }

    global.Upload = { init };
})(window);
