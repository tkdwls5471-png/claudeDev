/**
 * attachments.js — 첨부파일 뷰어 + 이미지 라이트박스
 *  - PDF: 원본을 브라우저 PDF 뷰어로 (스캔본 포함)
 *  - 이미지: 디스크 원본(/raw) 또는 base64, 클릭 시 라이트박스
 *  - Excel: 시트별 셀 그리드 / Word·HWPX: 서식 HTML(스크립트 없는 샌드박스) / 텍스트
 */
(function (global) {
    'use strict';
    const esc = escapeHtml;
    const IMG_MT = /^image\/(png|jpeg|gif|bmp|tiff|webp)$/;
    const DOCX_CSS = `h1{font-size:1.4em;font-weight:700;margin:.6em 0 .3em}h2{font-size:1.2em;font-weight:700;margin:.6em 0 .3em}`
        + `h3{font-size:1.05em;font-weight:600;margin:.5em 0 .3em}p{margin:.4em 0}ul{list-style:disc;padding-left:1.4em}`
        + `ol{list-style:decimal;padding-left:1.4em}table{border-collapse:collapse;margin:.6em 0}`
        + `td,th{border:1px solid #cbd5e1;padding:4px 8px;vertical-align:top}.hwpx-tbl p{margin:0}a{color:#0047AB}`;

    let lbImages = [];
    const lb = { idx: 0, zoom: false };
    const sliders = {};    // sliderId → [base64...]

    function dataSrc(att) {
        const mt = IMG_MT.test(att.media_type || '') ? att.media_type : 'image/png';
        return `data:${mt};base64,${att.content}`;
    }

    /**
     * container에 첨부 목록을 그린다.
     * opts: { emailId, attachments, canReprocess }
     */
    function render(container, opts) {
        const { emailId, attachments, canReprocess } = opts;
        const id = Number(emailId);
        const visible = (attachments || []).filter(a => a.type !== 'unsupported' || a.has_original);
        if (!visible.length) {
            container.innerHTML = '<p class="text-center text-slate-400 text-sm py-10">첨부파일이 없습니다.</p>';
            return;
        }
        const canOrig = att => !!(id && att.has_original && att.idx != null);
        const rawUrl = (att, dl) => `/api/emails/${id}/attachment/${Number(att.idx)}/raw${dl ? '?dl=1' : ''}`;
        const dlLink = att => canOrig(att)
            ? `<a href="${rawUrl(att, true)}" class="ml-auto inline-flex items-center gap-1 text-xs text-slate-400 hover:text-primary whitespace-nowrap" title="원본 다운로드">
                 <span class="material-symbols-outlined text-sm">download</span>원본</a>`
            : '';

        lbImages = [];
        const bases = visible.map(att => {
            const base = lbImages.length;
            if (att.type === 'images' && Array.isArray(att.content)) {
                att.content.forEach((p, i) => lbImages.push({
                    src: `data:image/png;base64,${p}`, label: `${att.filename} · ${i + 1}/${att.content.length}p` }));
            } else if (att.type === 'image') {
                const src = att.content ? dataSrc(att) : (canOrig(att) ? rawUrl(att) : '');
                if (src) lbImages.push({ src, label: att.filename });
            }
            return base;
        });

        const head = (icon, iconCls, att, extra = '', collapsible = false) => `
            <div class="flex items-center gap-2 px-4 py-2.5 border-b border-outline-variant/10 ${collapsible ? 'cursor-pointer select-none' : ''}"
                 ${collapsible ? 'data-action="att-toggle"' : ''}>
              <span class="material-symbols-outlined text-sm ${iconCls}">${icon}</span>
              <span class="text-sm font-semibold text-on-surface truncate" title="${esc(att.filename)}">${esc(att.filename)}</span>
              ${extra}
              ${collapsible ? '<span class="material-symbols-outlined text-xs text-slate-400 ml-1">expand_more</span>' : ''}
            </div>`;
        const card = inner => `<div class="bg-surface-container-lowest rounded-xl border border-outline-variant/10 overflow-hidden">${inner}</div>`;

        const html = visible.map((att, i) => {
            const base = bases[i];
            if (canOrig(att) && /\.pdf$/i.test(att.filename || '')) {
                const url = rawUrl(att, false);
                return card(head('picture_as_pdf', 'text-red-500', att, `
                    <div class="ml-auto flex items-center gap-3 shrink-0">
                      <a href="${url}" target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-primary"><span class="material-symbols-outlined text-sm">open_in_new</span>새 탭</a>
                      <a href="${rawUrl(att, true)}" class="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-primary"><span class="material-symbols-outlined text-sm">download</span>다운로드</a>
                    </div>`)
                    + `<iframe src="${url}" class="w-full bg-slate-100" style="height:70vh;border:0" title="${esc(att.filename)}"></iframe>`);
            }
            if (att.type === 'images' && Array.isArray(att.content) && att.content.length) {
                const sid = `att-slider-${id}-${i}`;
                sliders[sid] = att.content;
                const multi = att.content.length > 1;
                return card(head('picture_as_pdf', 'text-slate-400', att, `<span class="ml-auto text-xs text-slate-400">${att.content.length}페이지</span>`) + `
                    <div id="${sid}" data-cur="0" data-base="${base}" class="p-4">
                      <div class="relative bg-black rounded-lg overflow-hidden flex items-center justify-center min-h-[300px]">
                        <img data-role="slide-img" src="data:image/png;base64,${att.content[0]}" data-action="slide-zoom" data-slider="${sid}"
                             class="max-w-full max-h-[70vh] object-contain cursor-zoom-in" alt="페이지 1"/>
                        ${multi ? `
                        <button type="button" data-action="slide" data-slider="${sid}" data-delta="-1" class="absolute left-2 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-black/50 hover:bg-black/80 text-white flex items-center justify-center"><span class="material-symbols-outlined text-sm">chevron_left</span></button>
                        <button type="button" data-action="slide" data-slider="${sid}" data-delta="1" class="absolute right-2 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-black/50 hover:bg-black/80 text-white flex items-center justify-center"><span class="material-symbols-outlined text-sm">chevron_right</span></button>` : ''}
                      </div>
                      ${multi ? `<p data-role="slide-label" class="text-center text-xs text-slate-500 mt-2">1 / ${att.content.length}</p>` : ''}
                    </div>`);
            }
            if (att.type === 'image') {
                const src = att.content ? dataSrc(att) : (canOrig(att) ? rawUrl(att) : '');
                return card(head('image', 'text-slate-400', att, dlLink(att)) + `
                    <div class="p-4 flex justify-center bg-slate-50">
                      ${src ? `<img src="${src}" data-action="lightbox" data-index="${base}" class="max-w-full max-h-[60vh] object-contain rounded cursor-zoom-in" alt="${esc(att.filename)}"/>`
                            : '<p class="text-xs text-slate-400 py-6">이미지 원본이 없습니다.</p>'}
                    </div>`);
            }
            if (att.type === 'table') {
                const sheets = (att.content || []).map(sheet => {
                    const body = (sheet.rows || []).map((r, ri) =>
                        `<tr class="${ri === 0 ? 'bg-slate-100 font-semibold' : ''}">`
                        + r.map(c => `<td class="border border-slate-200 px-2.5 py-1.5 text-xs text-slate-700 whitespace-nowrap">${esc(c)}</td>`).join('')
                        + '</tr>').join('');
                    return `<div class="mb-3">
                        <p class="flex items-center gap-1.5 mb-1.5 text-xs font-bold text-slate-500"><span class="material-symbols-outlined text-xs text-emerald-500">tab</span>${esc(sheet.name)}
                          ${sheet.truncated ? '<span class="text-[10px] text-amber-500 font-normal">(일부만 표시)</span>' : ''}</p>
                        <div class="overflow-auto max-h-[60vh] rounded-lg border border-slate-200"><table class="border-collapse w-max">${body}</table></div>
                      </div>`;
                }).join('');
                return card(head('table_chart', 'text-emerald-500', att, dlLink(att), true)
                    + `<div class="p-4">${sheets || '<p class="text-xs text-slate-400">표 데이터 없음</p>'}</div>`);
            }
            if (att.type === 'html') {
                return card(head('description', 'text-blue-500', att, dlLink(att), true)
                    + `<div class="p-3" data-role="docx" data-att="${i}"></div>`);
            }
            if (att.type === 'text') {
                const fn = att.filename || '';
                const icon = /\.pdf$/i.test(fn) ? 'picture_as_pdf' : /\.hwpx?$/i.test(fn) ? 'article' : /\.pptx?$/i.test(fn) ? 'slideshow' : 'description';
                return card(head(icon, 'text-slate-400', att, dlLink(att), true)
                    + `<pre class="hidden p-4 text-xs text-slate-600 whitespace-pre-wrap overflow-x-auto max-h-96 leading-relaxed">${esc(att.content)}</pre>`);
            }
            return card(head('attach_file', 'text-slate-400', att,
                dlLink(att) || '<span class="ml-auto text-xs text-slate-400">미리보기 미지원</span>'));
        }).join('');

        container.innerHTML = `
            <div class="flex items-center gap-2 mb-3">
              <span class="text-xs font-bold text-slate-400 uppercase tracking-wider">첨부파일 ${visible.length}개</span>
              ${canReprocess ? `<button type="button" data-action="reprocess" class="ml-auto flex items-center gap-1 text-xs text-slate-400 hover:text-primary" title="원본을 다시 받아 첨부를 최신 형식으로 갱신">
                  <span class="material-symbols-outlined text-sm">refresh</span>원본 갱신</button>` : ''}
            </div>
            <div class="space-y-3">${html}</div>`;

        // Word/HWPX 변환 HTML → 스크립트 없는 샌드박스
        container.querySelectorAll('[data-role="docx"]').forEach(el => {
            const att = visible[Number(el.dataset.att)];
            renderSandboxedHtml(el, att.content || '', { extraCss: DOCX_CSS, maxHeight: Math.round(window.innerHeight * 0.6) });
        });

        container.onclick = (e) => {
            const t = e.target.closest('[data-action]');
            if (!t || !container.contains(t)) return;
            const a = t.dataset.action;
            if (a === 'att-toggle') t.nextElementSibling?.classList.toggle('hidden');
            else if (a === 'lightbox') openLightbox(Number(t.dataset.index));
            else if (a === 'slide') slide(t.dataset.slider, Number(t.dataset.delta));
            else if (a === 'slide-zoom') {
                const box = document.getElementById(t.dataset.slider);
                openLightbox(Number(box.dataset.base) + Number(box.dataset.cur || 0));
            } else if (a === 'reprocess' && opts.onReprocess) opts.onReprocess();
        };
    }

    function slide(sid, delta) {
        const pages = sliders[sid] || [];
        const box = document.getElementById(sid);
        if (!box || !pages.length) return;
        const cur = Math.max(0, Math.min(pages.length - 1, Number(box.dataset.cur || 0) + delta));
        box.dataset.cur = String(cur);
        const img = box.querySelector('[data-role="slide-img"]');
        if (img) img.src = `data:image/png;base64,${pages[cur]}`;
        const label = box.querySelector('[data-role="slide-label"]');
        if (label) label.textContent = `${cur + 1} / ${pages.length}`;
    }

    // ─── 라이트박스 ──────────────────────────────────────────────────────────
    function lbRender() {
        const data = lbImages[lb.idx];
        if (!data) return;
        const img = document.getElementById('lightbox-img');
        if (img) {
            img.src = data.src;
            img.style.transform = lb.zoom ? 'scale(2.2)' : 'scale(1)';
            img.style.cursor = lb.zoom ? 'zoom-out' : 'zoom-in';
        }
        const label = document.getElementById('lightbox-label');
        if (label) label.textContent = `${data.label}  ·  ${lb.idx + 1} / ${lbImages.length}`;
        const multi = lbImages.length > 1;
        document.getElementById('lightbox-prev').style.display = multi ? '' : 'none';
        document.getElementById('lightbox-next').style.display = multi ? '' : 'none';
    }
    function openLightbox(index) {
        if (!lbImages.length) return;
        lb.idx = Math.max(0, Math.min(lbImages.length - 1, index || 0));
        lb.zoom = false;
        lbRender();
        document.getElementById('image-lightbox')?.classList.remove('hidden');
    }
    function isLightboxOpen() {
        return !document.getElementById('image-lightbox')?.classList.contains('hidden');
    }

    global.lightboxNav = function (delta, e) {
        e?.stopPropagation();
        if (!lbImages.length) return;
        lb.idx = (lb.idx + delta + lbImages.length) % lbImages.length;
        lb.zoom = false;
        lbRender();
    };
    global.toggleLightboxZoom = function (e) { e?.stopPropagation(); lb.zoom = !lb.zoom; lbRender(); };
    global.closeLightbox = function (e) {
        e?.stopPropagation();
        document.getElementById('image-lightbox')?.classList.add('hidden');
    };

    document.addEventListener('keydown', (e) => {
        if (!isLightboxOpen()) return;
        if (e.key === 'ArrowLeft') global.lightboxNav(-1);
        else if (e.key === 'ArrowRight') global.lightboxNav(1);
        else if (e.key === 'Escape') global.closeLightbox();
        else if (e.key === '+' || e.key === '=' || e.key === '-') global.toggleLightboxZoom();
        else return;
        e.preventDefault();
        e.stopImmediatePropagation();
    }, true);

    /** 구버전 포맷으로 저장된 첨부(재처리하면 더 잘 보이는 것) */
    function hasLegacy(atts) {
        return (atts || []).some(a => {
            const f = (a.filename || '').toLowerCase();
            if (a.type === 'text' && typeof a.content === 'string' && a.content.trim().startsWith('[')) return false;
            if (/\.(xlsx|xls)$/.test(f)) return a.type !== 'table';
            if (/\.docx$/.test(f)) return a.type !== 'html';
            if (/\.(hwp|hwpx|doc)$/.test(f)) return a.type === 'unsupported';
            return false;
        });
    }

    global.Attachments = { render, hasLegacy, isLightboxOpen };
})(window);
