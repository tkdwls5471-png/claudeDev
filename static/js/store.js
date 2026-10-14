/**
 * store.js — 메일 Row 저장소 (단일 출처)
 * 목록·상세 패널·일정(Gantt)·대시보드가 같은 Row를 보고, upsert 시 'row' 이벤트로 함께 다시 그린다.
 */
(function (global) {
    'use strict';
    const rows = new Map();
    const details = new Map();       // id → 상세(본문·분석 JSON·첨부), 최근 10건만
    const DETAIL_MAX = 10;

    const Store = {
        get(id) { return rows.get(Number(id)); },

        upsert(row, { silent = false } = {}) {
            if (!row || row.id == null) return row;
            const id = Number(row.id);
            const merged = { ...(rows.get(id) || {}), ...row };
            rows.set(id, merged);
            const d = details.get(id);
            if (d) details.set(id, { ...d, ...row });
            if (!silent) Bus.emit('row', merged);
            return merged;
        },

        upsertMany(list) { (list || []).forEach(r => Store.upsert(r, { silent: true })); },

        remove(id) {
            id = Number(id);
            rows.delete(id);
            details.delete(id);
            Bus.emit('row-removed', id);
        },

        getDetail(id) { return details.get(Number(id)); },

        setDetail(detail) {
            const id = Number(detail.id);
            details.delete(id);
            details.set(id, detail);
            while (details.size > DETAIL_MAX) details.delete(details.keys().next().value);
            Store.upsert(detail);
        },

        dropDetail(id) { details.delete(Number(id)); },

        /** 낙관적 갱신: patch 적용 후, 실패 시 되돌릴 함수를 반환 */
        optimistic(id, patch) {
            const before = { ...(rows.get(Number(id)) || {}) };
            Store.upsert({ id, ...patch });
            return () => Store.upsert(before);
        },
    };

    global.Store = Store;
})(window);
