/**
 * core.js — 공용 유틸 (다른 모든 스크립트보다 먼저 로드)
 *  - escapeHtml: 서버·LLM·메일에서 온 문자열은 반드시 이걸 거쳐 HTML에 넣는다
 *  - api(): fetch 래퍼 (res.ok 확인, 로그인 만료 시 /login, 오류 토스트)
 *  - toast / copyToClipboard / renderPager / 날짜 도우미
 *  - renderSandboxedHtml(): 외부 HTML(메일 원본·워드 변환)을 스크립트 없는 iframe + CSP로 표시
 *  - Bus: 화면 간 이벤트 (row 갱신 등)
 */
(function (global) {
    'use strict';

    // ─── 이스케이프 ──────────────────────────────────────────────────────────
    const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    function escapeHtml(v) {
        return String(v ?? '').replace(/[&<>"']/g, c => ESC[c]);
    }

    // ─── 이벤트 버스 ─────────────────────────────────────────────────────────
    const Bus = new EventTarget();
    Bus.emit = (type, detail) => Bus.dispatchEvent(new CustomEvent(type, { detail }));
    Bus.on = (type, fn) => Bus.addEventListener(type, e => fn(e.detail));

    // ─── API ────────────────────────────────────────────────────────────────
    class ApiError extends Error {
        constructor(message, status, data) { super(message); this.status = status; this.data = data; }
    }
    let _redirecting = false;

    /**
     * api('/api/emails', { method, json, query, form, raw, silent, signal })
     *  - json: 객체 → JSON 본문 / form: FormData
     *  - query: {k: v | [v...]} → 쿼리스트링 (빈 값 생략)
     *  - raw: true면 Response를 그대로 반환 (파일 다운로드)
     *  - silent: true면 오류 토스트를 띄우지 않음
     */
    async function api(path, opts = {}) {
        const { method = 'GET', json, form, query, raw, silent, signal } = opts;
        let url = path;
        if (query) {
            const qs = new URLSearchParams();
            Object.entries(query).forEach(([k, v]) => {
                if (Array.isArray(v)) v.forEach(x => (x !== '' && x != null) && qs.append(k, x));
                else if (v !== '' && v != null && v !== false) qs.append(k, v === true ? '1' : v);
            });
            const s = qs.toString();
            if (s) url += (url.includes('?') ? '&' : '?') + s;
        }
        const init = { method, signal, headers: {} };
        if (json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(json); }
        if (form) init.body = form;

        let res;
        try {
            res = await fetch(url, init);
        } catch (e) {
            if (e.name === 'AbortError') throw e;
            const err = new ApiError('서버에 연결할 수 없습니다', 0);
            if (!silent) toast(err.message, 'error');
            throw err;
        }
        if (res.status === 401 || (res.redirected && new URL(res.url).pathname === '/login')) {
            if (!_redirecting) { _redirecting = true; location.href = '/login'; }
            throw new ApiError('로그인이 필요합니다', 401);
        }
        if (raw) {
            if (!res.ok) {
                const err = new ApiError(`요청 실패 (${res.status})`, res.status);
                if (!silent) toast(err.message, 'error');
                throw err;
            }
            return res;
        }
        let data = null;
        try { data = await res.json(); } catch (_) { /* 본문 없음 */ }
        if (!res.ok || (data && data.success === false && !data.duplicate && !data.created)) {
            const msg = (data && (data.message || data.error?.message || data.error)) || `요청 실패 (${res.status})`;
            const err = new ApiError(String(msg), res.status, data);
            if (!silent) toast(err.message, 'error');
            throw err;
        }
        return data;
    }

    // ─── 토스트 ──────────────────────────────────────────────────────────────
    function toast(message, type = 'info', position = 'top-center') {
        const el = document.createElement('div');
        const bg = { error: 'bg-error text-on-error', success: 'bg-green-600 text-white' }[type]
            || 'bg-primary text-on-primary';
        const pos = {
            'bottom-right': 'toast-br bottom-24 lg:bottom-8 right-4',
            'top-center': 'toast-tc top-6 left-1/2 -translate-x-1/2',
        }[position] || 'toast-tc top-6 left-1/2 -translate-x-1/2';
        el.className = `toast fixed px-5 py-3 rounded-xl text-sm font-semibold z-[400] shadow-lg flex items-center gap-2 max-w-[90vw] ${pos} ${bg}`;
        el.setAttribute('role', 'status');
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 300); }, type === 'error' ? 4500 : 3000);
    }

    // ─── 복사 ────────────────────────────────────────────────────────────────
    function copyToClipboard(text, btn) {
        text = String(text || '');
        if (!text) return;
        const done = () => {
            toast('복사되었습니다.', 'success');
            const ic = btn?.querySelector('.material-symbols-outlined');
            if (ic) { const o = ic.textContent; ic.textContent = 'check'; setTimeout(() => { ic.textContent = o; }, 1200); }
        };
        const fallback = () => {
            const ta = document.createElement('textarea');
            ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
            document.body.appendChild(ta); ta.select();
            try { document.execCommand('copy'); } catch (_) {}
            ta.remove(); done();
        };
        if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done).catch(fallback);
        else fallback();
    }

    // ─── 페이저 (1~10, 11~20 블록) ─────────────────────────────────────────
    /** onGo(page)는 data-page 위임 클릭으로 호출된다. */
    function renderPager(el, page, perPage, total, onGo) {
        if (!el) return;
        const totalPages = Math.max(1, Math.ceil(total / perPage));
        const BLOCK = 10;
        const start = Math.floor((page - 1) / BLOCK) * BLOCK + 1;
        const end = Math.min(start + BLOCK - 1, totalPages);
        const btn = (label, p, { active = false, disabled = false } = {}) =>
            `<button type="button" ${disabled ? 'disabled' : `data-page="${p}"`}
                class="min-w-[26px] px-2 py-1 rounded text-xs font-medium transition-colors ${
                    active ? 'bg-primary text-white' : disabled ? 'text-slate-300 cursor-not-allowed'
                    : 'text-slate-600 hover:bg-surface-container-low'}">${label}</button>`;
        let html = btn('‹', start - 1, { disabled: start <= 1 });
        for (let p = start; p <= end; p++) html += btn(String(p), p, { active: p === page });
        html += btn('›', end + 1, { disabled: end >= totalPages });
        el.innerHTML = totalPages > 1 ? html : '';
        el.onclick = (e) => {
            const b = e.target.closest('[data-page]');
            if (b) onGo(Math.min(Math.max(1, parseInt(b.dataset.page, 10)), totalPages));
        };
    }

    // ─── 날짜 (서버는 KST 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' 문자열을 준다) ───
    const DAY = 86400000;
    function kstNow() { return new Date(Date.now() + 9 * 3600000); }
    /** 오늘(KST) 'YYYY-MM-DD' */
    function todayStr() { return kstNow().toISOString().slice(0, 10); }
    /** 'YYYY-MM-DD' → 자정 UTC 기준 epoch ms (날짜 차이 계산용) */
    function ymdToMs(s) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
        return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : NaN;
    }
    function addDays(ymd, n) {
        const t = ymdToMs(ymd);
        return isNaN(t) ? ymd : new Date(t + n * DAY).toISOString().slice(0, 10);
    }
    /** 마감일까지 남은 일수 (오늘=0, 지남=음수) */
    function daysUntil(ymd) {
        const t = ymdToMs(ymd);
        return isNaN(t) ? null : Math.round((t - ymdToMs(todayStr())) / DAY);
    }
    function ddayLabel(ymd) {
        const d = daysUntil(ymd);
        if (d === null) return '';
        return d === 0 ? 'D-day' : d > 0 ? `D-${d}` : `D+${-d}`;
    }
    /** KST 문자열 → Date */
    function parseKst(s) {
        const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(s || '');
        return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0) - 9, +(m[5] || 0))) : null;
    }
    function relativeTime(kstStr) {
        const d = parseKst(kstStr);
        if (!d) return '';
        const sec = Math.round((Date.now() - d.getTime()) / 1000);
        if (sec < 60) return '방금';
        if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
        if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
        return `${Math.floor(sec / 86400)}일 전`;
    }
    /** 목록용 짧은 수신일: 오늘이면 HH:MM, 올해면 MM-DD, 아니면 YYYY-MM-DD */
    function shortDate(kstStr) {
        if (!kstStr) return '';
        const ymd = kstStr.slice(0, 10);
        if (ymd === todayStr()) return kstStr.slice(11, 16) || ymd;
        if (ymd.slice(0, 4) === todayStr().slice(0, 4)) return ymd.slice(5);
        return ymd;
    }
    /** Date → KST 'YYYY-MM-DD HH:MM' */
    function toKstString(date) {
        const d = new Date(date.getTime() + 9 * 3600000);
        return isNaN(d) ? '' : d.toISOString().slice(0, 16).replace('T', ' ');
    }

    // ─── 외부 HTML을 안전하게 표시 ──────────────────────────────────────────
    const DROP_TAGS = 'script,iframe,frame,frameset,object,embed,applet,form,base,meta,link,noscript,template,svg,math';

    /**
     * container에 sandbox iframe을 만들어 html을 표시한다.
     *  - allow-scripts 없음 → 스크립트 실행 불가
     *  - CSP: 외부 리소스 기본 차단 (allowRemoteImages면 이미지만 허용)
     *  - 다크모드에서도 원문이 읽히도록 흰 바탕 고정
     *  options: { allowRemoteImages, extraCss, minHeight, maxHeight }
     */
    function renderSandboxedHtml(container, html, options = {}) {
        const { allowRemoteImages = false, extraCss = '', minHeight = 120, maxHeight = 0 } = options;
        const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
        doc.querySelectorAll(DROP_TAGS).forEach(n => n.remove());
        doc.querySelectorAll('*').forEach(el => {
            for (const a of [...el.attributes]) {
                const name = a.name.toLowerCase();
                const val = a.value.trim().toLowerCase();
                if (name.startsWith('on') || name === 'srcset' || name === 'formaction') { el.removeAttribute(a.name); continue; }
                if ((name === 'href' || name === 'src' || name === 'xlink:href' || name === 'action' || name === 'background')
                    && /^(javascript|vbscript|data:text\/html)/.test(val.replace(/\s+/g, ''))) {
                    el.removeAttribute(a.name);
                }
            }
            if (el.tagName === 'A' && el.getAttribute('href')) {
                el.setAttribute('target', '_blank');
                el.setAttribute('rel', 'noopener noreferrer');
            }
        });
        const imgSrc = allowRemoteImages ? 'data: https: http:' : 'data:';
        const head = `<meta charset="utf-8">`
            + `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src ${imgSrc}; font-src data:; form-action 'none'; base-uri 'none'">`
            + `<meta name="referrer" content="no-referrer"><base target="_blank">`
            + `<style>html{color-scheme:light}body{margin:12px;background:#fff;color:#1f2937;font:14px/1.6 -apple-system,'Segoe UI','Malgun Gothic',sans-serif;word-break:break-word}img{max-width:100%;height:auto}table{max-width:100%}${extraCss}</style>`;
        const iframe = document.createElement('iframe');
        iframe.setAttribute('sandbox', 'allow-same-origin allow-popups allow-popups-to-escape-sandbox');
        iframe.setAttribute('referrerpolicy', 'no-referrer');
        iframe.className = 'sandboxed-html w-full rounded-xl border border-slate-100';
        iframe.style.minHeight = minHeight + 'px';
        iframe.title = '원본 미리보기';
        const resize = () => {
            try {
                const h = iframe.contentDocument?.documentElement?.scrollHeight || minHeight;
                iframe.style.height = (maxHeight ? Math.min(h + 4, maxHeight) : h + 4) + 'px';
            } catch (_) {}
        };
        iframe.addEventListener('load', () => { resize(); setTimeout(resize, 300); });
        container.innerHTML = '';
        container.appendChild(iframe);
        // srcdoc은 프로퍼티로 대입 (속성 문자열에 끼워 넣으면 따옴표로 탈출 가능)
        iframe.srcdoc = `<!doctype html><html><head>${head}</head><body>${doc.body ? doc.body.innerHTML : ''}</body></html>`;
        return iframe;
    }

    // ─── 내보내기 ────────────────────────────────────────────────────────────
    Object.assign(global, {
        escapeHtml, Bus, api, ApiError, toast, copyToClipboard, renderPager,
        DateUtil: { todayStr, addDays, daysUntil, ddayLabel, parseKst, relativeTime, shortDate, toKstString, ymdToMs },
        renderSandboxedHtml,
    });
    // 옛 코드 호환 이름
    global.showToast = toast;
    global.escHtml = escapeHtml;
})(window);
