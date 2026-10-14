/**
 * mailui.js — 메일 배지(우선순위·유형·마감·분석상태)와 처리 동작(완료·제외·읽음·마감·분석·삭제)
 * 목록·상세 패널·일정·대시보드·AI 검색이 같은 표시와 같은 동작을 쓰도록 한 곳에 모은다.
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;

    // ─── 배지 ────────────────────────────────────────────────────────────────
    const PRIO_CLS = { '높음': 'badge-red', '보통': 'badge-amber', '낮음': 'badge-green' };

    const Badges = {
        priority(p) {
            return p ? `<span class="badge ${PRIO_CLS[p] || 'badge-slate'}">${esc(p)}</span>` : '';
        },
        type(t) {
            return t ? `<span class="badge badge-slate">${esc(t)}</span>` : '';
        },
        source(row) {
            return row.source === 'upload'
                ? '<span class="badge badge-violet" title="EML 업로드"><span class="material-symbols-outlined text-[12px]">upload_file</span>업로드</span>'
                : '';
        },
        /** 마감 D-day: 지남=빨강, 3일 이내=주황, 그 외=회색. 완료/제외면 흐리게 */
        deadline(row, { long = false } = {}) {
            if (!row.deadline) return long ? '<span class="text-xs text-slate-400">마감 없음</span>' : '';
            const d = DateUtil.daysUntil(row.deadline);
            const closed = row.is_completed || row.is_excluded;
            const cls = closed ? 'badge-slate' : d < 0 ? 'badge-red' : d <= 3 ? 'badge-amber' : 'badge-slate';
            const label = long ? `${row.deadline} · ${DateUtil.ddayLabel(row.deadline)}` : DateUtil.ddayLabel(row.deadline);
            return `<span class="badge ${cls}" title="마감 ${esc(row.deadline)}${row.deadline_manual ? ' (직접 입력)' : ''}">`
                + `<span class="material-symbols-outlined text-[12px]">event</span>${esc(label)}</span>`;
        },
        /** 분석 상태. done이면 표시하지 않음(완료가 기본) — 오류가 남아 있으면 경고 */
        analysis(row, { retry = true } = {}) {
            const err = row.analysis_error ? esc(row.analysis_error) : '';
            switch (row.analysis_status) {
                case 'queued': {
                    const when = row.analysis_retry_count && row.analysis_next_at
                        ? ` · ${esc(row.analysis_next_at.slice(11, 16))} 재시도` : '';
                    return `<span class="badge badge-blue" title="${err || '분석 대기 중'}"><span class="material-symbols-outlined text-[12px]">schedule</span>대기${when}</span>`;
                }
                case 'running':
                    return '<span class="badge badge-blue"><span class="analyze-spinner !w-3 !h-3"></span>분석중</span>';
                case 'failed':
                    return `<span class="badge badge-red" title="${err}"><span class="material-symbols-outlined text-[12px]">error</span>분석 실패</span>`
                        + (retry ? ` <button type="button" class="badge-btn" data-action="analyze" data-id="${row.id}" title="다시 분석">재시도</button>` : '');
                case 'none':
                    return '<span class="badge badge-slate">미분석</span>';
                default:
                    return err ? `<span class="badge badge-amber" title="${err}"><span class="material-symbols-outlined text-[12px]">warning</span>재분석 실패</span>` : '';
            }
        },
    };

    // ─── 동작 ────────────────────────────────────────────────────────────────
    const MailActions = {
        /** PATCH (낙관적 갱신 → 응답 반영, 실패 시 되돌림) */
        async patch(id, patch, optimisticRow) {
            const undo = Store.optimistic(id, optimisticRow || patch);
            try {
                const row = await api(`/api/emails/${id}`, { method: 'PATCH', json: patch });
                Store.upsert(row);
                Bus.emit('mail-changed', { ids: [id] });
                return row;
            } catch (e) {
                undo();
                throw e;
            }
        },

        toggleComplete(id) {
            const r = Store.get(id);
            if (!r) return;
            const next = !r.is_completed;
            return MailActions.patch(id, { is_completed: next })
                .then(() => toast(next ? '완료 처리했습니다.' : '완료를 취소했습니다.', 'success'))
                .catch(() => {});
        },

        toggleExclude(id) {
            const r = Store.get(id);
            if (!r) return;
            const next = !r.is_excluded;
            return MailActions.patch(id, { is_excluded: next })
                .then(() => toast(next ? '제외했습니다. (분석 대기열에서도 빠집니다)' : '제외를 해제했습니다.', 'success'))
                .catch(() => {});
        },

        toggleRead(id) {
            const r = Store.get(id);
            if (!r) return;
            return MailActions.patch(id, { is_read: !r.is_read }).catch(() => {});
        },

        setDeadline(id, ymd) {
            return MailActions.patch(id, { deadline: ymd || null }, { deadline: ymd || null, deadline_manual: true })
                .then(() => toast(ymd ? `마감일을 ${ymd}로 저장했습니다.` : '마감일을 비웠습니다.', 'success'))
                .catch(() => {});
        },

        resetDeadline(id) {
            return MailActions.patch(id, { deadline_manual: false }, { deadline_manual: false })
                .then(r => toast(r.deadline ? `AI가 찾은 마감일(${r.deadline})로 되돌렸습니다.` : 'AI가 찾은 마감일이 없습니다.', 'success'))
                .catch(() => {});
        },

        async analyze(id) {
            try {
                const row = await api(`/api/emails/${id}/analyze`, { method: 'POST' });
                Store.upsert(row);
                Bus.emit('mail-changed', { ids: [id] });
                Bus.emit('queue-changed');
                toast('분석 대기열 맨 앞에 올렸습니다.', 'success');
            } catch (_) {}
        },

        async remove(id) {
            const r = Store.get(id);
            if (!confirm(`'${(r?.subject || '이 메일').slice(0, 40)}' 메일을 앱에서 삭제할까요?\n(메일 서버의 원본은 지워지지 않습니다)`)) return false;
            try {
                await api(`/api/emails/${id}`, { method: 'DELETE' });
                Store.remove(id);
                Bus.emit('mail-changed', { ids: [id], removed: true });
                toast('삭제했습니다.', 'success');
                return true;
            } catch (_) { return false; }
        },

        async bulk(ids, action) {
            if (!ids.length) return 0;
            const r = await api('/api/emails/bulk', { method: 'POST', json: { ids, action } });
            Bus.emit('mail-changed', { ids, action, bulk: true });
            if (action === 'analyze') Bus.emit('queue-changed');
            return r.affected || 0;
        },

        /** 발신자·도메인 제외 규칙 추가: 기존 메일 몇 건이 제외되는지 보여 주고 확인받는다. 추가되면 true */
        async addRule(kind, value) {
            let p;
            try { p = await api('/api/sender-rules/preview', { method: 'POST', json: { kind, value } }); }
            catch (_) { return false; }
            const what = kind === 'domain' ? `'${p.value}' 도메인(하위 도메인 포함)` : `'${p.value}'`;
            const msg = `${what}에서 오는 메일을 앞으로 자동으로 '제외'하고 분석하지 않습니다.`
                + (p.matches ? `\n이미 받은 메일 ${p.matches}건도 제외합니다 (완료한 메일은 그대로).` : '')
                + `\n\n설정 > 발신자 제외 규칙에서 언제든 되돌릴 수 있습니다. 추가할까요?`;
            if (!confirm(msg)) return false;
            try {
                const r = await api('/api/sender-rules', { method: 'POST', json: { kind, value: p.value, apply_existing: true } });
                toast(`규칙을 추가했습니다.${r.affected ? ` 기존 메일 ${r.affected}건을 제외했습니다.` : ''}`, 'success');
                Bus.emit('mail-changed', { bulk: true });
                Bus.emit('rules-changed');
                return true;
            } catch (_) { return false; }
        },

        async exportExcel(ids) {
            if (!ids.length) { toast('다운로드할 메일을 선택하세요.', 'error'); return; }
            try {
                const res = await api('/download/emails', { method: 'POST', json: { email_ids: ids }, raw: true });
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = '요구사항_분석결과.xlsx';
                document.body.appendChild(a); a.click(); a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            } catch (_) {}
        },
    };

    /** '홍길동 <Hong@A.co.kr>' → { address: 'hong@a.co.kr', domain: 'a.co.kr' } */
    function parseSender(sender) {
        const s = String(sender || '');
        const m = /<([^<>\s]+@[^<>\s]+)>/.exec(s) || /([^\s<>"']+@[^\s<>"']+)/.exec(s);
        const address = m ? m[1].toLowerCase() : '';
        return { address, domain: address ? address.split('@').pop() : '' };
    }

    global.parseSender = parseSender;
    global.Badges = Badges;
    global.MailActions = MailActions;
})(window);
