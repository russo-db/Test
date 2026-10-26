// 🪙 Токен SkyLords — клиентский модуль. Сервер отдаёт этот файл только тем,
// кому токен открыт (статус active — всем, testing — только админам), через
// /api/client/modules; у остальных игроков этого кода нет вовсе. Модуль
// встраивается в игру через gameHooks (index.html): кнопка стейкинга над
// главным орлом, раздел у Купца, отдел «Токен SkyLords» на Рынке и вкладка
// «За токен 100%» в окне улучшения орлов. Все балансы считает сервер.
(function () {
    'use strict';
    if (window.__skyLoaded) return;
    window.__skyLoaded = true;

    const STR = {
        ru: {
            name: 'SkyLords', token: 'токенов', tokenShort: 'SKL',
            btn: 'Стейкинг', loading: 'Загрузка…', error: 'Не удалось загрузить — попробуй ещё раз',
            title: '🪙 Стейкинг SkyLords',
            balance: 'Баланс', staked: 'В стейкинге', rate: 'Ставка', perDay: 'в день',
            claimable: 'К получению', pending: 'Копится сегодня', nextPayout: 'Начисление через {t} (00:00 UTC)',
            depositLabel: 'Внести токены', depositBtn: 'Внести', withdrawLabel: 'Снять токены', withdrawBtn: 'Снять',
            withdrawIn: 'Снять через {t}', all: 'Все',
            claimBtn: 'Забрать {n}', reinvestBtn: 'Реинвест {n}', noReward: 'Награда начисляется раз в сутки',
            ladder: 'Лесенка — шаги строго по очереди снизу вверх',
            stepStart: 'Старт: от {n} токенов', stepAmount: 'Докинуть до {n} токенов', stepBurn: '🔥 Сжечь орла: {tier}',
            stepMax: ' (максимум)', stepN: 'Шаг {n}: ', burnBtn: '🔥 Сжечь', done: 'выполнено', notCounted: 'не считается — сначала шаги ниже',
            ladderHint: 'Ставка — по последнему шагу, до которого всё выполнено по порядку. Нарушен порядок — базовая ставка.',
            pickTitle: '🔥 Какого орла сжечь?', pickSub: 'Нужна редкость «{tier}». Орёл исчезнет навсегда.',
            pickEmpty: 'Нет свободного орла редкости «{tier}» (не на рынке, не в экспедиции, не на охоте).',
            level: 'ур. {n}', confirmBurn: 'Сжечь {name} (ур. {lvl}) навсегда ради шага лесенки?',
            burned: '🔥 Орёл сожжён — шаг выполнен', deposited: 'Внесено {n}', withdrawn: 'Снято {n}',
            claimed: 'Получено {n}', reinvested: 'Реинвестировано {n}', partial: ' (пул наград почти пуст)',
            cooldownHint: 'Снятие — через 24 ч после последнего внесения (реинвест тоже внесение).',
            toMerchant: '🛒 Купить токены у Купца', poolLeft: 'Пул наград: {n}',
            merchantTitle: '🪙 Токен SkyLords', merchantRate: '{tokens} токенов = {gram} GRAM', merchantLeft: 'У Купца осталось: {n}',
            packsLabel: 'Сколько пачек по {n} токенов', merchantCost: 'Получишь {tokens} токенов за {gram} GRAM',
            merchantBuy: 'Купить', merchantBought: 'Куплено {n} токенов', merchantMax: 'Максимум',
            marketDept: '🪙 SkyLords', sellBtn: 'Выставить токены на продажу', tabTokens: '🪙 Токены',
            lotTitle: '{n} токенов', perK: '{p} GRAM за 1000', minLine: 'Лот — кратно {step} токенов · минимум {min} GRAM за 1000',
            sellTitle: 'Продать токены', amountLabel: 'Сколько токенов (кратно {step})', priceLabel: 'Цена лота в GRAM',
            minPrice: 'Минимальная цена: {p} GRAM', net: 'Тебе придёт {n} GRAM (после комиссии {pct}%)', owned: 'У тебя: {n} токенов', commission: 'Комиссия рынка {pct}% — с продавца',
            listBtn: 'Выставить', cancel: 'Отмена', listed: 'Лот выставлен', bought: 'Куплено {n} токенов',
            confirmList: 'Выставить {n} токенов за {p} GRAM?', confirmBuy: 'Купить {n} токенов за {p} GRAM?',
            badAmount: 'Количество — кратно {step} токенов', lowPrice: 'Цена не ниже {p} GRAM',
            fusionTab: '🪙 За токен 100%', fusionInfo: 'Гарантированно: два орла → {name} без рулетки, шанс 100%.',
            fusionBurnNote: 'Потраченные токены сжигаются навсегда.', fusionBtn: 'Улучшить за {n} токенов',
            fusionNoCost: 'Для этой редкости улучшение за токены недоступно.', fusionHave: 'У тебя: {n} токенов',
            fusionNeed: 'Не хватает {n} токенов', fusionConfirm: 'Сжечь {n} токенов и гарантированно получить {name}?',
            fusionDone: '{a} + {a} → {b}! Сожжено {n} токенов', serverError: 'Сервер недоступен', online: 'Нужен интернет',
        },
        en: {
            name: 'SkyLords', token: 'tokens', tokenShort: 'SKL',
            btn: 'Staking', loading: 'Loading…', error: 'Could not load — try again',
            title: '🪙 SkyLords Staking',
            balance: 'Balance', staked: 'Staked', rate: 'Rate', perDay: 'per day',
            claimable: 'Claimable', pending: 'Accruing today', nextPayout: 'Payout in {t} (00:00 UTC)',
            depositLabel: 'Stake tokens', depositBtn: 'Stake', withdrawLabel: 'Unstake tokens', withdrawBtn: 'Unstake',
            withdrawIn: 'Unstake in {t}', all: 'All',
            claimBtn: 'Claim {n}', reinvestBtn: 'Reinvest {n}', noReward: 'Rewards are paid once a day',
            ladder: 'Ladder — steps strictly in order, bottom to top',
            stepStart: 'Start: from {n} tokens', stepAmount: 'Top up to {n} tokens', stepBurn: '🔥 Burn an eagle: {tier}',
            stepMax: ' (max)', stepN: 'Step {n}: ', burnBtn: '🔥 Burn', done: 'done', notCounted: 'not counted — finish the steps below first',
            ladderHint: 'Your rate is the last step reached with every step below done. Out of order — base rate.',
            pickTitle: '🔥 Which eagle to burn?', pickSub: 'Rarity “{tier}” needed. The eagle is gone forever.',
            pickEmpty: 'No free “{tier}” eagle (not on the market, expedition or hunt).',
            level: 'lv {n}', confirmBurn: 'Burn {name} (lv {lvl}) forever for this ladder step?',
            burned: '🔥 Eagle burned — step done', deposited: 'Staked {n}', withdrawn: 'Unstaked {n}',
            claimed: 'Claimed {n}', reinvested: 'Reinvested {n}', partial: ' (reward pool is almost empty)',
            cooldownHint: 'Unstaking opens 24 h after your last deposit (reinvest counts as a deposit).',
            toMerchant: '🛒 Buy tokens from the Merchant', poolLeft: 'Reward pool: {n}',
            merchantTitle: '🪙 SkyLords token', merchantRate: '{tokens} tokens = {gram} GRAM', merchantLeft: 'Merchant has: {n}',
            packsLabel: 'Packs of {n} tokens', merchantCost: 'You get {tokens} tokens for {gram} GRAM',
            merchantBuy: 'Buy', merchantBought: 'Bought {n} tokens', merchantMax: 'Max',
            marketDept: '🪙 SkyLords', sellBtn: 'Sell tokens', tabTokens: '🪙 Tokens',
            lotTitle: '{n} tokens', perK: '{p} GRAM per 1000', minLine: 'Lots in steps of {step} tokens · at least {min} GRAM per 1000',
            sellTitle: 'Sell tokens', amountLabel: 'Tokens (steps of {step})', priceLabel: 'Lot price in GRAM',
            minPrice: 'Minimum price: {p} GRAM', net: 'You get {n} GRAM (after the {pct}% fee)', owned: 'You have: {n} tokens', commission: 'Market fee {pct}% — paid by the seller',
            listBtn: 'List', cancel: 'Cancel', listed: 'Lot listed', bought: 'Bought {n} tokens',
            confirmList: 'List {n} tokens for {p} GRAM?', confirmBuy: 'Buy {n} tokens for {p} GRAM?',
            badAmount: 'Amount — steps of {step} tokens', lowPrice: 'Price at least {p} GRAM',
            fusionTab: '🪙 Token 100%', fusionInfo: 'Guaranteed: two eagles → {name}, no wheel, 100% chance.',
            fusionBurnNote: 'Spent tokens are burned forever.', fusionBtn: 'Upgrade for {n} tokens',
            fusionNoCost: 'Token upgrade is not available for this rarity.', fusionHave: 'You have: {n} tokens',
            fusionNeed: 'Missing {n} tokens', fusionConfirm: 'Burn {n} tokens for a guaranteed {name}?',
            fusionDone: '{a} + {a} → {b}! {n} tokens burned', serverError: 'Server unavailable', online: 'Internet needed',
        },
    };
    function tt(key, vars) {
        const d = STR[typeof LANG !== 'undefined' && LANG === 'en' ? 'en' : 'ru'];
        let s = d[key] != null ? d[key] : (STR.ru[key] != null ? STR.ru[key] : key);
        if (vars) Object.keys(vars).forEach(k => { s = s.split('{' + k + '}').join(vars[k]); });
        return s;
    }
    function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }
    function fmt(v) {
        const n = Number(v) || 0;
        return n.toLocaleString(LANG === 'en' ? 'en-US' : 'ru-RU', { maximumFractionDigits: 2 }).replace(/ /g, ' ');
    }
    function fmtG(v) { const n = Number(v) || 0; return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100); }
    function dur(sec) {
        sec = Math.max(0, Math.floor(sec));
        const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
        return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    function tierName(id) { const tr = (CONFIG.tiers || []).find(x => x.id === id); return tr ? tr.name : id; }
    function tierColor(id) { const tr = (CONFIG.tiers || []).find(x => x.id === id); return tr ? tr.color : '#fff'; }
    function tierOfMonster(mid) { const tr = (CONFIG.tiers || []).find(x => (x.monsters || []).some(m => m.id === mid)); return tr ? tr.id : null; }

    // --- стили ---
    const css = document.createElement('style');
    css.textContent = `
        .sky-entry { display: flex; justify-content: center; margin: 2px 0 -4px; position: relative; z-index: 3; }
        .sky-entry-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 999px; cursor: pointer;
            font-weight: 800; font-size: 12px; letter-spacing: .3px; color: #ffe9a8; text-transform: uppercase;
            background: linear-gradient(135deg, rgba(247,181,0,.28), rgba(140,82,255,.28)); border: 1px solid rgba(247,181,0,.55);
            box-shadow: 0 0 14px rgba(247,181,0,.25); }
        .sky-entry-btn b { color: #fff; font-weight: 800; text-transform: none; }
        .sky-box { max-width: 380px; text-align: left; max-height: 88vh; overflow-y: auto; padding: 18px; }
        .sky-box h2 { text-align: center; }
        .sky-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0 4px; }
        .sky-stat { background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.1); border-radius: 12px; padding: 8px 10px; min-width: 0; }
        .sky-stat span { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: .5px; color: var(--text-muted); font-weight: 700; }
        .sky-stat b { display: block; font-size: 15px; color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sky-stat b.gold { color: var(--gold-lt, #ffd36a); }
        .sky-row { display: flex; gap: 8px; align-items: center; }
        .sky-row .pay-input { flex: 1 1 auto; min-width: 0; margin: 0; }
        .sky-row .btn-action { flex: none; padding: 10px 12px; }
        .sky-act { width: 100%; margin-top: 8px; padding: 12px 14px; font-size: 14px; }
        .sky-muted { font-size: 12px; color: var(--text-muted); margin: 6px 2px; line-height: 1.35; }
        .sky-claim { display: flex; gap: 8px; margin-top: 10px; }
        .sky-claim button { flex: 1 1 0; min-width: 0; justify-content: center; }
        .sky-ladder { display: flex; flex-direction: column-reverse; gap: 6px; margin-top: 8px; }
        .sky-step { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 12px; border: 1px solid rgba(255,255,255,.08);
            background: rgba(255,255,255,.03); opacity: .45; }
        .sky-step.available { opacity: 1; border-color: rgba(247,181,0,.45); }
        .sky-step.counted { opacity: 1; background: rgba(46, 204, 113, .14); border-color: rgba(46, 204, 113, .5); }
        .sky-step.current { box-shadow: 0 0 0 1px rgba(46, 204, 113, .8), 0 0 14px rgba(46, 204, 113, .3); }
        .sky-step.done-loose { opacity: .7; border-style: dashed; }
        .sky-step-n { flex: none; width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
            font-size: 11px; font-weight: 800; background: rgba(255,255,255,.08); color: #fff; }
        .sky-step.counted .sky-step-n { background: rgba(46, 204, 113, .8); }
        .sky-step-t { flex: 1 1 auto; min-width: 0; font-size: 12.5px; font-weight: 700; color: #fff; line-height: 1.25; }
        .sky-step-t small { display: block; font-weight: 600; color: var(--text-muted); font-size: 10.5px; }
        .sky-step-r { flex: none; font-weight: 800; font-size: 12.5px; color: var(--gold-lt, #ffd36a); }
        .sky-step .btn-action { flex: none; padding: 6px 10px; font-size: 12px; }
        .sky-pick { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; max-height: 48vh; overflow-y: auto; }
        .sky-pick button { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; }
        .sky-pick img { width: 38px; height: 38px; object-fit: contain; }
        .sky-merchant { margin-top: 12px; padding: 16px; background: rgba(10, 6, 24, 0.78) !important; }
        .sky-coin { font-size: 26px; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; flex: none;
            border-radius: 12px; background: rgba(247,181,0,.12); }
        .sky-fusion { text-align: left; }
        .sky-fusion .sky-muted { text-align: center; }
        #marketDeptSky .pay-input { background: rgba(5, 3, 16, 0.12); }
    `;
    document.head.appendChild(css);

    // --- данные и запросы ---
    const sky = { data: null, loading: null, market: { loaded: false, listings: [], counts: {} }, marketTab: 'tokens', pickStep: null };
    window.skyState = sky;

    async function skyGet(path) {
        const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'user_id=' + state.userId, { headers: apiHeaders() });
        const data = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, data };
    }
    async function skyPost(path, body) {
        const r = await fetch(path, { method: 'POST', headers: apiHeaders(), body: JSON.stringify(Object.assign({ user_id: state.userId }, body || {})) });
        const data = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, data };
    }
    function applyWallet(d) {
        if (!d) return;
        if (!sky.data) sky.data = {};
        if (typeof d.balance === 'number') sky.data.balance = d.balance;
        if (d.stake) sky.data.stake = d.stake;
        if (typeof d.coins === 'number') state.coins = d.coins;
        if (typeof d.ops === 'number') state.ops = d.ops;
        if (typeof d.merchant_left === 'number' && sky.data.merchant) sky.data.merchant.left = d.merchant_left;
        if (d.monsters) state.farm = readFarm(d.monsters);
        if (d.farm_queue) state.farmQueue = readFarm(d.farm_queue);
        if (typeof d.active_slot === 'number') state.activeSlot = d.active_slot;
    }
    async function loadState() {
        if (offlineOnly || !state.userId) return null;
        if (sky.loading) return sky.loading;
        sky.loading = (async () => {
            try {
                const r = await skyGet('api/skylords/state');
                if (r.ok) { sky.data = r.data; sky.syncedAt = Date.now(); }
                else if (r.status === 403) { sky.data = null; sky.off = true; teardown(); }
            } catch (e) { /* сеть — оставляем прошлое */ }
            sky.loading = null;
            refreshAll();
            return sky.data;
        })();
        return sky.loading;
    }
    // время сервера: таймеры считаются от его часов, а не от часов телефона
    function srvNow() { return sky.data && sky.data.server_now ? sky.data.server_now + (Date.now() - (sky.syncedAt || Date.now())) / 1000 : Date.now() / 1000; }
    function busy(key, fn) {
        return async function (...a) {
            if (farmActionBusy.has(key)) return;
            farmActionBusy.add(key);
            try { return await fn.apply(this, a); } finally { setTimeout(() => farmActionBusy.delete(key), 600); }
        };
    }
    function fail(r) {
        if (r && r.status === 403) { sky.off = true; teardown(); toast(tt('serverError')); return; }
        toast((r && r.data && r.data.detail) || tt('serverError')); haptic.notify('warning');
    }
    // админ выключил токен, пока игра открыта: убираем весь интерфейс модуля
    function teardown() {
        ['skyEntry', 'skyMerchant', 'skyStakeModal', 'skyBurnModal', 'skySellModal'].forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
        const dt = document.querySelector('#marketDeptBar [data-dept="sky"]'); if (dt) dt.remove();
        const panel = document.getElementById('marketDeptSky'); if (panel) panel.remove();
        const di = gameHooks.marketDepts.findIndex(d => d.id === 'sky'); if (di >= 0) gameHooks.marketDepts.splice(di, 1);
        const fi = gameHooks.fusionModes.findIndex(m => m.id === 'token'); if (fi >= 0) gameHooks.fusionModes.splice(fi, 1);
        if (typeof marketDept !== 'undefined' && marketDept === 'sky' && document.getElementById('market').classList.contains('active')) switchMarketDept('eagles');
        else if (typeof marketDept !== 'undefined' && marketDept === 'sky') marketDept = 'eagles';
        const fm = document.getElementById('fusionModal');
        if (fm && fm.classList.contains('open') && fusionTab === 'token') { fusionTab = 'meat'; renderFusionModal(); }
    }

    // --- кнопка над главным орлом ---
    function renderEntry() {
        if (sky.off) return;
        const area = document.querySelector('#farm .farm-area');
        const row = area && area.querySelector('.farm-hero-row');
        if (!row) return;
        let el = document.getElementById('skyEntry');
        if (!el) {
            el = document.createElement('div');
            el.id = 'skyEntry';
            el.className = 'sky-entry';
            area.insertBefore(el, row);
        }
        const staked = sky.data && sky.data.stake ? sky.data.stake.staked : 0;
        const html = `<div class="sky-entry-btn" onclick="skyOpenStake(event)">🪙 ${tt('btn')}${staked ? ` <b>${fmt(staked)}</b>` : ''}</div>`;
        if (el.innerHTML !== html) el.innerHTML = html;
        alignEntry();
    }
    // ровно над главным орлом: орёл стоит между боковыми колонками и не всегда
    // в центре экрана — сдвигаем кнопку по его центру
    function alignEntry() {
        const el = document.getElementById('skyEntry'), stage = document.getElementById('monsterStage');
        if (!el || !stage || !el.offsetParent) return;
        const a = el.getBoundingClientRect(), b = stage.getBoundingClientRect();
        if (!a.width || !b.width) return;
        const cur = parseFloat(el.dataset.dx || '0');
        const dx = Math.round((b.left + b.width / 2) - (a.left + a.width / 2) + cur);
        if (dx !== cur) { el.dataset.dx = String(dx); el.style.transform = dx ? `translateX(${dx}px)` : ''; }
    }
    window.addEventListener('resize', () => alignEntry());

    // --- экран стейкинга ---
    function ensureModal(id, cls) {
        let m = document.getElementById(id);
        if (!m) {
            m = document.createElement('div');
            m.className = 'modal-backdrop';
            m.id = id;
            m.innerHTML = `<div class="modal-box glass-panel ${cls || 'pay-box'}" id="${id}Body"></div>`;
            m.addEventListener('click', ev => { if (ev.target === m) m.classList.remove('open'); });
            document.body.appendChild(m);
        }
        return m;
    }
    let stakeTimer = null;
    window.skyOpenStake = function (ev) {
        if (ev) ev.stopPropagation();
        if (offlineOnly || !state.userId) { toast(tt('online')); return; }
        haptic.select();
        const m = ensureModal('skyStakeModal', 'sky-box');
        m.classList.add('open');
        renderStake();
        loadState();
        clearInterval(stakeTimer);
        stakeTimer = setInterval(() => {
            if (!m.classList.contains('open')) { clearInterval(stakeTimer); return; }
            tickStake();
        }, 1000);
    };
    window.skyCloseStake = function () { const m = document.getElementById('skyStakeModal'); if (m) m.classList.remove('open'); };

    function stepTitle(s, i, n) {
        if (i === 0) return tt('stepStart', { n: fmt(s.min) });
        const pre = tt('stepN', { n: i });
        if (s.kind === 'amount') return pre + tt('stepAmount', { n: fmt(s.min) }) + (i === n - 1 ? tt('stepMax') : '');
        return pre + tt('stepBurn', { tier: `<span style="color:${tierColor(s.tier)}">${esc(tierName(s.tier))}</span>` }) + (i === n - 1 ? tt('stepMax') : '');
    }
    function renderStake() {
        const body = document.getElementById('skyStakeModalBody');
        if (!body) return;
        const d = sky.data;
        if (!d || !d.stake) {
            body.innerHTML = `<h2>${tt('title')}</h2><p class="sky-muted" style="text-align:center">${sky.loading ? tt('loading') : tt('error')}</p>
                <div class="pay-actions"><button class="btn-action" style="width:100%" onclick="skyCloseStake()">${t('common.close')}</button></div>`;
            return;
        }
        const st = d.stake, steps = st.steps || [];
        const cdLeft = (st.cooldown_until || 0) - srvNow();
        const ladder = steps.map(s => {
            const cls = s.counted ? 'counted' : (s.done ? 'done-loose' : (s.available ? 'available' : ''));
            const sub = s.counted ? tt('done') : (s.done ? tt('notCounted') : '');
            const btn = s.kind === 'burn' && !s.done && s.available ? `<button class="btn-action" onclick="skyPickBurn(${s.i})">${tt('burnBtn')}</button>` : '';
            return `<div class="sky-step ${cls} ${s.current ? 'current' : ''}">
                <div class="sky-step-n">${s.counted ? '✓' : (s.i === 0 ? '▶' : s.i)}</div>
                <div class="sky-step-t">${stepTitle(s, s.i, steps.length)}${sub ? `<small>${sub}</small>` : ''}</div>
                <div class="sky-step-r">${fmt(s.rate)}%</div>${btn}</div>`;
        }).join('');
        body.innerHTML = `
            <h2>${tt('title')}</h2>
            <div class="sky-stats">
                <div class="sky-stat"><span>${tt('balance')}</span><b>${fmt(d.balance)}</b></div>
                <div class="sky-stat"><span>${tt('staked')}</span><b>${fmt(st.staked)}</b></div>
                <div class="sky-stat"><span>${tt('rate')}</span><b class="gold">${fmt(st.rate)}% <small style="font-size:11px;color:var(--text-muted)">${tt('perDay')}</small></b></div>
                <div class="sky-stat"><span>≈ ${tt('perDay')}</span><b>+${fmt(st.per_day)}</b></div>
                <div class="sky-stat"><span>${tt('claimable')}</span><b class="gold">${fmt(st.claimable)}</b></div>
                <div class="sky-stat"><span>${tt('pending')}</span><b>${fmt(st.pending)}</b></div>
            </div>
            <p class="sky-muted" id="skyNextPayout">${tt('nextPayout', { t: dur((st.next_payout_at || 0) - srvNow()) })}</p>
            <div class="sky-claim">
                <button class="btn-primary" id="skyClaimBtn" ${st.claimable > 0 ? '' : 'disabled'} onclick="skyClaim(false)">${tt('claimBtn', { n: fmt(st.claimable) })}</button>
                <button class="btn-action" id="skyReinvestBtn" ${st.claimable > 0 ? '' : 'disabled'} onclick="skyClaim(true)">${tt('reinvestBtn', { n: fmt(st.claimable) })}</button>
            </div>
            <div class="pay-label">${tt('depositLabel')}</div>
            <div class="sky-row">
                <input class="pay-input" id="skyDepositAmount" type="number" inputmode="decimal" min="0.01" step="0.01" placeholder="1000">
                <button class="btn-action" onclick="skyFillAll('skyDepositAmount', ${Number(d.balance) || 0})">${tt('all')}</button>
            </div>
            <button class="btn-primary sky-act" id="skyDepositBtn" onclick="skyDeposit()">${tt('depositBtn')}</button>
            <div class="pay-label">${tt('withdrawLabel')}</div>
            <div class="sky-row">
                <input class="pay-input" id="skyWithdrawAmount" type="number" inputmode="decimal" min="0.01" step="0.01" placeholder="0">
                <button class="btn-action" onclick="skyFillAll('skyWithdrawAmount', ${Number(st.staked) || 0})">${tt('all')}</button>
            </div>
            <button class="btn-action sky-act" style="justify-content:center" id="skyWithdrawBtn" ${cdLeft > 0 || !(st.staked > 0) ? 'disabled' : ''} onclick="skyWithdraw()">${cdLeft > 0 && st.staked > 0 ? tt('withdrawIn', { t: dur(cdLeft) }) : tt('withdrawBtn')}</button>
            <p class="sky-muted">${tt('cooldownHint')}</p>
            <div class="pay-label">${tt('ladder')}</div>
            <div class="sky-ladder">${ladder}</div>
            <p class="sky-muted">${tt('ladderHint')}</p>
            <p class="sky-muted">${tt('poolLeft', { n: fmt(d.staking_pool_left) })}</p>
            <div class="pay-actions" style="display:flex;flex-direction:column;gap:8px">
                <button class="btn-action" style="width:100%" onclick="skyGoMerchant()">${tt('toMerchant')}</button>
                <button class="btn-action" style="width:100%" onclick="skyCloseStake()">${t('common.close')}</button>
            </div>`;
    }
    function tickStake() {
        const d = sky.data;
        if (!d || !d.stake) return;
        const st = d.stake;
        const np = document.getElementById('skyNextPayout');
        const left = (st.next_payout_at || 0) - srvNow();
        if (np) np.textContent = tt('nextPayout', { t: dur(left) });
        if (left <= 0 && !sky.loading && !sky.payoutReload) { sky.payoutReload = true; setTimeout(() => { sky.payoutReload = false; }, 5000); loadState(); }
        const wb = document.getElementById('skyWithdrawBtn');
        if (wb) {
            const cd = (st.cooldown_until || 0) - srvNow();
            const want = cd > 0 && st.staked > 0 ? tt('withdrawIn', { t: dur(cd) }) : tt('withdrawBtn');
            if (wb.textContent !== want) wb.textContent = want;
            const dis = cd > 0 || !(st.staked > 0);
            if (wb.disabled !== dis) wb.disabled = dis;
        }
    }
    window.skyFillAll = function (id, v) { const el = document.getElementById(id); if (el) el.value = Math.floor((Number(v) || 0) * 100) / 100; };
    window.skyGoMerchant = function () {
        skyCloseStake();
        const nav = document.querySelector(`.nav-item[onclick*="'merchant'"]`);
        if (nav) switchTab('merchant', nav);
    };
    function readAmount(id) { const v = Number((document.getElementById(id) || {}).value); return Number.isFinite(v) && v > 0 ? Math.round(v * 100) / 100 : 0; }
    function afterAction(r, msg) {
        applyWallet(r.data);
        haptic.notify('success');
        if (msg) toast(msg);
        renderHeader();
        refreshAll();
        loadState();
    }
    window.skyDeposit = busy('sky-stake', async function () {
        const amount = readAmount('skyDepositAmount');
        if (!amount) { toast(tt('depositLabel')); return; }
        try {
            const r = await skyPost('api/skylords/stake/deposit', { amount });
            if (!r.ok) { fail(r); return; }
            afterAction(r, tt('deposited', { n: fmt(amount) }));
        } catch (e) { toast(tt('serverError')); }
    });
    window.skyWithdraw = busy('sky-stake', async function () {
        const amount = readAmount('skyWithdrawAmount');
        if (!amount) { toast(tt('withdrawLabel')); return; }
        try {
            const r = await skyPost('api/skylords/stake/withdraw', { amount });
            if (!r.ok) { fail(r); return; }
            afterAction(r, tt('withdrawn', { n: fmt(amount) }));
        } catch (e) { toast(tt('serverError')); }
    });
    window.skyClaim = busy('sky-stake', async function (reinvest) {
        try {
            const r = await skyPost('api/skylords/stake/claim', { reinvest: !!reinvest });
            if (!r.ok) { fail(r); loadState(); return; }
            afterAction(r, tt(reinvest ? 'reinvested' : 'claimed', { n: fmt(r.data.claimed) }) + (r.data.partial ? tt('partial') : ''));
        } catch (e) { toast(tt('serverError')); }
    });

    // сжечь орла для шага лесенки: выбор орла нужной редкости
    function freeEaglesOf(tier) {
        return (state.farm || []).map((slot, i) => ({ slot, i }))
            .filter(x => tierOfMonster(x.slot.id) === tier && !isListed(x.slot) && !isOnExpedition(x.slot) && !isOnHunt(x.slot) && !isInRaid(x.slot));
    }
    window.skyPickBurn = function (step) {
        const s = ((sky.data && sky.data.stake && sky.data.stake.steps) || [])[step];
        if (!s) return;
        sky.pickStep = step;
        const list = freeEaglesOf(s.tier).sort((a, b) => (a.slot.feedLevel || 1) - (b.slot.feedLevel || 1));
        const m = ensureModal('skyBurnModal', 'pay-box');
        const tn = esc(tierName(s.tier));
        document.getElementById('skyBurnModalBody').innerHTML = `
            <h2>${tt('pickTitle')}</h2>
            <p>${tt('pickSub', { tier: tn })}</p>
            <div class="sky-pick">${list.length ? list.map(x => {
                const mon = MONSTERS[x.slot.id] || {};
                return `<button class="lang-option" onclick="skyBurn(${step}, ${x.i}, '${x.slot.id}')">
                    <img src="${framePath(mon.sprite, 0)}" alt=""><span style="color:${tierColor(s.tier)}">${esc(mon.name || x.slot.id)}</span>
                    <span style="margin-left:auto;color:var(--text-muted)">${tt('level', { n: x.slot.feedLevel || 1 })}</span></button>`;
            }).join('') : `<p class="sky-muted">${tt('pickEmpty', { tier: tn })}</p>`}</div>
            <div class="pay-actions"><button class="btn-action" style="width:100%" onclick="document.getElementById('skyBurnModal').classList.remove('open')">${tt('cancel')}</button></div>`;
        m.classList.add('open');
    };
    window.skyBurn = busy('sky-burn', async function (step, slotIndex, monsterId) {
        const slot = state.farm[slotIndex];
        if (!slot || slot.id !== monsterId) { toast(t('farm.staleSlot')); return; }
        const mon = MONSTERS[monsterId] || {};
        if (!confirm(tt('confirmBurn', { name: mon.name || monsterId, lvl: slot.feedLevel || 1 }))) return;
        try {
            const r = await skyPost('api/skylords/stake/burn', { step, slot_index: slotIndex, monster_id: monsterId });
            if (!r.ok) { fail(r); await syncFromServer({ keepActiveSlot: true }).catch(() => {}); renderAll(); return; }
            const bm = document.getElementById('skyBurnModal'); if (bm) bm.classList.remove('open');
            afterAction(r, tt('burned'));
            renderAll();
            saveLocally();
        } catch (e) { toast(tt('serverError')); }
    });

    // --- Купец ---
    function renderMerchant() {
        if (sky.off) return;
        const host = document.getElementById('merchant');
        const shop = document.getElementById('merchantBody');
        if (!host || !shop) return;
        let el = document.getElementById('skyMerchant');
        if (!el) {
            el = document.createElement('div');
            el.id = 'skyMerchant';
            el.className = 'glass-panel sky-merchant';
            shop.insertAdjacentElement('afterend', el);
        }
        const d = sky.data;
        if (!d || !d.merchant) { el.innerHTML = `<div class="exchange-section-title">${tt('merchantTitle')}</div><p class="sky-muted">${tt('loading')}</p>`; return; }
        const m = d.merchant;
        const prev = document.getElementById('skyPacks');
        const packs = prev ? prev.value : '1';
        const affordable = m.pack_price_gram > 0 ? Math.floor((Number(state.coins) || 0) / m.pack_price_gram + 1e-9) : m.max_packs;
        const maxPacks = Math.max(0, Math.min(m.max_packs, affordable, Math.floor((Number(m.left) || 0) / m.pack_tokens)));
        el.innerHTML = `
            <div class="exchange-section-title">${tt('merchantTitle')}</div>
            <div class="exchange-rate-line">${tt('merchantRate', { tokens: fmt(m.pack_tokens), gram: fmtG(m.pack_price_gram) })}</div>
            <div class="exchange-rate-line">${tt('merchantLeft', { n: fmt(m.left) })} · ${tt('balance')}: ${fmt(d.balance)}</div>
            <div class="pay-label">${tt('packsLabel', { n: fmt(m.pack_tokens) })}</div>
            <div class="sky-row">
                <input class="pay-input" id="skyPacks" type="number" inputmode="numeric" min="1" max="${m.max_packs}" step="1" value="${esc(packs)}" oninput="skyMerchantPreview()">
                <button class="btn-action" onclick="skySetPacks(${maxPacks})">${tt('merchantMax')}</button>
            </div>
            <div class="exchange-receive-line" id="skyMerchantCost"></div>
            <div class="merchant-meat-actions"><button class="btn-primary" id="skyMerchantBuyBtn" onclick="skyMerchantBuy()">${tt('merchantBuy')}</button></div>`;
        skyMerchantPreview();
    }
    window.skySetPacks = function (n) { const el = document.getElementById('skyPacks'); if (el) { el.value = Math.max(1, n); skyMerchantPreview(); } };
    window.skyMerchantPreview = function () {
        const d = sky.data, out = document.getElementById('skyMerchantCost');
        if (!d || !d.merchant || !out) return;
        const p = Math.max(0, Math.floor(Number((document.getElementById('skyPacks') || {}).value) || 0));
        out.textContent = tt('merchantCost', { tokens: fmt(p * d.merchant.pack_tokens), gram: fmtG(Math.round(p * d.merchant.pack_price_gram * 100) / 100) });
    };
    window.skyMerchantBuy = busy('sky-merchant', async function () {
        const packs = Math.floor(Number((document.getElementById('skyPacks') || {}).value) || 0);
        if (packs < 1) { toast(tt('packsLabel', { n: fmt((sky.data && sky.data.merchant || {}).pack_tokens || 1000) })); return; }
        try {
            const r = await skyPost('api/skylords/merchant/buy', { packs });
            if (!r.ok) { fail(r); loadState(); return; }
            afterAction(r, tt('merchantBought', { n: fmt(r.data.bought) }));
            spawnBurst(10);
        } catch (e) { toast(tt('serverError')); }
    });

    // --- Рынок: отдел «Токен SkyLords» (как отдел «Ресурсы») ---
    function mountMarket() {
        const bar = document.getElementById('marketDeptBar');
        const market = document.getElementById('market');
        if (!bar || !market || document.getElementById('marketDeptSky')) return;
        const tab = document.createElement('div');
        tab.className = 'subtab-item';
        tab.dataset.dept = 'sky';
        tab.textContent = tt('marketDept');
        tab.onclick = () => switchMarketDept('sky');
        bar.appendChild(tab);
        const panel = document.createElement('div');
        panel.className = 'subtab-panel';
        panel.id = 'marketDeptSky';
        panel.innerHTML = `
            <button class="btn-primary market-sell-btn" onclick="skyOpenSell()">
                <div class="icon-md"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"></path><path d="M5 12h14"></path></svg></div>
                <span id="skySellBtnLabel">${tt('sellBtn')}</span>
            </button>
            <div class="market-tab-bar" id="skyMarketTabBar"></div>
            <div id="skyMarketBody"></div>`;
        market.appendChild(panel);
        gameHooks.marketDepts.push({ id: 'sky', panelId: 'marketDeptSky', load: loadMarket });
        if (typeof marketDept !== 'undefined' && marketDept === 'sky') panel.classList.add('active');
    }
    const skyIcon = '<div class="sky-coin">🪙</div>';
    function mcfg() { const m = sky.market; return { step: m.lot_step || 1000, min: m.min_gram_per_1000 != null ? m.min_gram_per_1000 : 1, commission: m.commission != null ? m.commission : 0.1 }; }
    async function loadMarket() {
        if (offlineOnly || !state.userId) { renderMarketDept(); return; }
        sky.marketSeq = (sky.marketSeq || 0) + 1;
        const seq = sky.marketSeq;
        try {
            const r = await skyGet('api/skylords/market/listings');
            if (seq !== sky.marketSeq) return;
            if (r.ok) {
                Object.assign(sky.market, { loaded: true, listings: r.data.listings || [], counts: r.data.counts || {},
                    lot_step: r.data.lot_step, min_gram_per_1000: r.data.min_gram_per_1000, commission: r.data.commission });
                applyWallet(r.data);
            }
        } catch (e) { /* сеть */ }
        sky.market.loaded = true;
        renderMarketDept();
    }
    window.skyMarketTab = function (id) { sky.marketTab = id; haptic.select(); renderMarketDept(); };
    function renderMarketDept() {
        const body = document.getElementById('skyMarketBody');
        const bar = document.getElementById('skyMarketTabBar');
        if (!body || !bar) return;
        const lbl = document.getElementById('skySellBtnLabel'); if (lbl) lbl.textContent = tt('sellBtn');
        const dt = document.querySelector('#marketDeptBar [data-dept="sky"]'); if (dt && dt.textContent !== tt('marketDept')) dt.textContent = tt('marketDept');
        const L = sky.market.listings || [];
        bar.innerHTML = myListingsTab(sky.marketTab === 'mine', L, "skyMarketTab('mine')") +
            `<div class="market-tab-item ${sky.marketTab === 'tokens' ? 'active' : ''}" onclick="skyMarketTab('tokens')">${tt('tabTokens')}</div>`;
        if (offlineOnly) { body.innerHTML = `<div class="market-locked"><p>${t('market.onlineOnly')}</p></div>`; return; }
        if (!sky.market.loaded) { body.innerHTML = `<div class="market-locked"><p>${t('market.loading')}</p></div>`; return; }
        const own = L.filter(l => String(l.seller_id) === String(state.userId));
        const title = l => tt('lotTitle', { n: fmt(l.amount) });
        if (sky.marketTab === 'mine') {
            body.innerHTML = own.length ? own.map(l => ownListingCard(skyIcon, title(l), l, 'skyCancelLot')).join('')
                : `<p class="market-section-empty">${t('market.noOwnListings')}</p>`;
            return;
        }
        const rows = L.filter(l => String(l.seller_id) !== String(state.userId))
            .sort((a, b) => a.unit_price - b.unit_price || a.price_gram - b.price_gram);
        const c = mcfg();
        body.innerHTML = `
            ${own.map(l => ownListingCard(skyIcon, title(l), l, 'skyCancelLot')).join('')}
            <p class="market-min-price-line">${tt('minLine', { step: fmt(c.step), min: fmtG(c.min) })}</p>
            ${marketShownNote(sky.market.counts, 'tokens', rows.length)}
            ${rows.length ? rows.map((l, idx) => `
                <div class="card glass-panel market-card">
                    ${skyIcon}
                    <div class="card-info">
                        <h3>${title(l)} ${idx === 0 ? `<span class="market-best-badge">${t('market.bestPrice')}</span>` : ''}</h3>
                        <p style="font-size:12px; color:var(--text-muted)">${escName(l.seller_name) || t('market.seller')} · ${tt('perK', { p: fmtG(l.unit_price) })}</p>
                    </div>
                    <button class="btn-action btn-buy" onclick="skyBuyLot('${l.id}')">${lotPriceHtml(l)}</button>
                </div>`).join('') : `<p class="market-section-empty">${t('market.sectionEmpty')}</p>`}`;
    }
    window.skyBuyLot = guardMarketAction(async function (id) {
        const lot = (sky.market.listings || []).find(l => l.id === id);
        if (lot && !confirm(tt('confirmBuy', { n: fmt(lot.amount), p: fmtG(lot.price_gram) }))) return;
        try {
            const r = await skyPost('api/skylords/market/buy', { listing_id: id });
            if (!r.ok) { fail(r); loadMarket(); return; }
            applyWallet(r.data);
            haptic.notify('success');
            spawnBurst(12);
            toast(tt('bought', { n: fmt(r.data.bought) }));
            renderHeader();
            loadMarket();
            refreshAll();
        } catch (e) { toast(t('market.serverError')); }
    }, () => 'sky-trade', LIST_ACTION_HOLD_MS);
    window.skyCancelLot = guardMarketAction(async function (id) {
        try {
            const r = await skyPost('api/skylords/market/cancel', { listing_id: id });
            if (!r.ok) { fail(r); loadMarket(); return; }
            applyWallet(r.data);
            haptic.notify('success');
            toast(t('market.cancelled'));
            loadMarket();
            refreshAll();
        } catch (e) { toast(t('market.serverError')); }
    }, () => 'sky-trade', LIST_ACTION_HOLD_MS);
    window.skyOpenSell = function () {
        if (farmActionBusy.has('sky-trade')) return;
        if (offlineOnly || !state.userId) { toast(t('market.onlineOnly')); return; }
        const m = ensureModal('skySellModal', 'pay-box');
        const c = mcfg();
        const bal = sky.data ? Number(sky.data.balance) || 0 : 0;
        const maxLot = Math.floor(bal / c.step) * c.step;
        const amount = Math.max(c.step, Math.min(maxLot || c.step, c.step));
        document.getElementById('skySellModalBody').innerHTML = `
            <h2>${tt('sellTitle')}</h2>
            <div class="pay-label">${tt('amountLabel', { step: fmt(c.step) })}</div>
            <input class="pay-input" id="skySellAmount" type="number" inputmode="numeric" min="${c.step}" step="${c.step}" value="${amount}" oninput="skySellPreview()">
            <div class="pay-label">${tt('priceLabel')}</div>
            <input class="pay-input" id="skySellPrice" type="number" inputmode="decimal" min="0.01" step="0.01" value="${fmtG(amount / 1000 * c.min)}" oninput="skySellPreview()">
            <p class="pay-warning" id="skySellPer" style="color:var(--gold-lt)"></p>
            <p class="pay-warning" id="skySellNet" style="color:var(--text-main, #fff)"></p>
            <p class="pay-warning" id="skySellMin"></p>
            <p class="pay-warning">${tt('owned', { n: fmt(bal) })}</p>
            <p class="pay-warning">${tt('commission', { pct: Math.round(c.commission * 100) })}</p>
            <div class="pay-actions">
                <button class="btn-primary" id="skySellConfirmBtn" onclick="skyConfirmSell()">${tt('listBtn')}</button>
                <button class="btn-action" onclick="document.getElementById('skySellModal').classList.remove('open')">${tt('cancel')}</button>
            </div>`;
        m.classList.add('open');
        skySellPreview();
    };
    window.skySellPreview = function () {
        const c = mcfg();
        const amount = Math.floor(Number((document.getElementById('skySellAmount') || {}).value) || 0);
        const price = Number((document.getElementById('skySellPrice') || {}).value) || 0;
        const min = Math.round(amount / 1000 * c.min * 100) / 100;
        const per = document.getElementById('skySellPer');
        if (per) per.textContent = amount > 0 && price > 0 ? tt('perK', { p: fmtG(Math.round(price / (amount / 1000) * 10000) / 10000) }) : '';
        const net = document.getElementById('skySellNet');
        if (net) net.textContent = price > 0 ? tt('net', { n: String(Math.floor(price * (1 - c.commission) * 1000 + 1e-6) / 1000), pct: Math.round(c.commission * 100) }) : '';
        const mn = document.getElementById('skySellMin');
        if (mn) mn.textContent = amount % c.step || amount < c.step ? tt('badAmount', { step: fmt(c.step) }) : tt('minPrice', { p: fmtG(min) });
    };
    window.skyConfirmSell = guardMarketAction(async function () {
        const c = mcfg();
        const amount = Math.floor(Number((document.getElementById('skySellAmount') || {}).value) || 0);
        const price = Math.round((Number((document.getElementById('skySellPrice') || {}).value) || 0) * 100) / 100;
        if (amount < c.step || amount % c.step) { toast(tt('badAmount', { step: fmt(c.step) })); haptic.notify('warning'); return; }
        const min = Math.round(amount / 1000 * c.min * 100) / 100;
        if (price < min - 1e-9) { toast(tt('lowPrice', { p: fmtG(min) })); haptic.notify('warning'); return; }
        if (!confirm(tt('confirmList', { n: fmt(amount), p: fmtG(price) }))) return;
        try {
            const r = await skyPost('api/skylords/market/list', { amount, price_gram: price });
            if (!r.ok) { fail(r); return; }
            const m = document.getElementById('skySellModal'); if (m) m.classList.remove('open');
            applyWallet(r.data);
            haptic.notify('success');
            toast(tt('listed'));
            sky.marketTab = 'mine';
            loadMarket();
            refreshAll();
        } catch (e) { toast(t('market.serverError')); }
    }, () => 'sky-trade', LIST_ACTION_HOLD_MS);

    // --- окно улучшения: вкладка «За токен 100%» ---
    gameHooks.fusionModes.push({
        id: 'token',
        label: () => tt('fusionTab'),
        render: ({ monster, nextMonster, nextTier }) => {
            const d = sky.data;
            if (!d) { loadState().then(() => { if (fusionModalA >= 0 && fusionTab === 'token') renderFusionModal(); }); return `<p class="sky-muted" style="text-align:center">${tt('loading')}</p>`; }
            const cost = nextTier ? Number((d.fusion_cost || {})[nextTier.id]) || 0 : 0;
            if (!cost || !nextMonster) return `<p class="sky-muted" style="text-align:center">${tt('fusionNoCost')}</p>`;
            const bal = Number(d.balance) || 0;
            const lack = Math.max(0, cost - bal);
            return `<div class="sky-fusion">
                <p class="sky-muted">${tt('fusionInfo', { name: esc(nextMonster.name) })}</p>
                <p class="sky-muted" style="color:var(--accent-rose)">${tt('fusionBurnNote')}</p>
                <p class="sky-muted">${tt('fusionHave', { n: fmt(bal) })}${lack ? ` · <span style="color:var(--accent-rose)">${tt('fusionNeed', { n: fmt(lack) })}</span>` : ''}</p>
                <div class="fusion-actions"><button class="btn-primary" id="skyFusionBtn" ${lack ? 'disabled' : ''} onclick="skyFusion()">${tt('fusionBtn', { n: fmt(cost) })}</button></div>
            </div>`;
        },
    });
    window.skyFusion = busy('sky-fusion', async function () {
        const a = state.farm[fusionModalA], b = state.farm[fusionModalB];
        if (!a || !b || a.id !== b.id) { closeFusionModal(); return; }
        const monster = MONSTERS[a.id];
        const nt = nextTierOf(monster);
        const nm = nt ? MONSTERS[nt.monsters[0].id] : null;
        const cost = nt ? Number(((sky.data || {}).fusion_cost || {})[nt.id]) || 0 : 0;
        if (!nm || !cost) return;
        if (!confirm(tt('fusionConfirm', { n: fmt(cost), name: nm.name }))) return;
        const btn = document.getElementById('skyFusionBtn'); if (btn) btn.disabled = true;
        try {
            const r = await skyPost('api/skylords/fusion', { slot_a: fusionModalA, slot_b: fusionModalB, monster_id: a.id });
            if (!r.ok) { fail(r); if (btn) btn.disabled = false; loadState().then(() => { if (fusionModalA >= 0) renderFusionModal(); }); return; }
            applyWallet(r.data);
            toast(tt('fusionDone', { a: monster.name, b: MONSTERS[r.data.monster].name, n: fmt(cost) }));
            haptic.notify('success');
            spawnBurst(16);
            closeFusionModal();
            renderAll();
            saveLocally();
            loadState();
        } catch (e) { toast(tt('serverError')); if (btn) btn.disabled = false; }
    });

    // --- перерисовка ---
    function refreshAll() {
        renderEntry();
        const sm = document.getElementById('skyStakeModal');
        if (sm && sm.classList.contains('open')) {
            // не затираем то, что игрок вводит прямо сейчас
            const keep = ['skyDepositAmount', 'skyWithdrawAmount'].map(id => [id, (document.getElementById(id) || {}).value]);
            const focus = document.activeElement && document.activeElement.id;
            renderStake();
            keep.forEach(([id, v]) => { const el = document.getElementById(id); if (el && v) el.value = v; });
            if (focus) { const el = document.getElementById(focus); if (el) el.focus(); }
        }
        const merch = document.getElementById('merchant');
        if (merch && merch.classList.contains('active') && document.activeElement !== document.getElementById('skyPacks')) renderMerchant();
        const fm = document.getElementById('fusionModal');
        if (fm && fm.classList.contains('open') && fusionTab === 'token' && !fusionSpinning) renderFusionModal();
    }
    gameHooks.afterRender.push(() => { renderEntry(); const mp = document.getElementById('marketDeptSky'); if (mp && mp.classList.contains('active')) renderMarketDept(); });
    gameHooks.tab.push(tabId => { if (tabId === 'merchant') { renderMerchant(); loadState(); } });

    mountMarket();
    renderEntry();
    loadState();
})();
