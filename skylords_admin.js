// 🪙 Админка токена SkyLords. Код приходит только после входа в админку
// (/admin/api/modules). Статус и переводы меняют только владельцы — админку
// для этого нужно открыть через бота командой /admin (подпись Telegram).
(function () {
    'use strict';
    if (window.__skyAdminLoaded) return;
    window.__skyAdminLoaded = true;
    const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
    const fmt = v => (Number(v) || 0).toLocaleString('ru-RU', { maximumFractionDigits: 2 }).replace(/ /g, ' ');
    const STATUS = {
        disabled: '⛔ disabled — выключено для всех',
        testing: '🧪 testing — видят и пользуются только админы (MAINTENANCE_WHITELIST), остальным API 403 и код не отдаётся',
        active: '✅ active — включено для всех игроков',
    };
    const KIND = {
        merchant_buy: '🛒 Купец', market_list: '📤 Лот выставлен', market_buy: '🤝 Лот куплен', market_cancel: '↩️ Лот снят',
        fusion_burn: '🔥 Улучшение за токены (сожжено)', stake_deposit: '📥 Стейк: внесение', stake_withdraw: '📤 Стейк: снятие',
        stake_claim: '🎁 Стейк: награда', stake_reinvest: '♻️ Стейк: реинвест', stake_burn_eagle: '🔥 Лесенка: сожжён орёл',
        admin_transfer: '🔀 Перевод админа', status: '⚙️ Статус',
    };

    // кнопка в меню и страница
    const nav = document.querySelector('#sidebar .nav-scroll');
    const main = document.getElementById('main');
    if (!nav || !main) return;
    const btn = document.createElement('button');
    btn.className = 'nav-btn';
    btn.dataset.skyPage = '1';
    btn.textContent = '🪙 SkyLords';
    nav.appendChild(btn);
    const page = document.createElement('div');
    page.className = 'page';
    page.id = 'page-sky';
    page.innerHTML = `
        <h2>🪙 Токен SkyLords</h2>
        <div class="nft-interval-panel">
            <b>Статус (skyLordsStatus)</b>
            <p class="muted" style="margin:6px 0 8px; font-size:12px;">Менять статус и переводить токены могут только владельцы — откройте админку через бота командой /admin.</p>
            <div id="skyAdmStatus" class="muted">Загрузка…</div>
            <div style="margin-top:8px;">
                <select id="skyAdmStatusSel">
                    <option value="disabled">disabled</option><option value="testing">testing</option><option value="active">active</option>
                </select>
                <button id="skyAdmStatusBtn">Сохранить статус</button>
            </div>
        </div>
        <div class="nft-interval-panel"><b>Сверка эмиссии</b><div id="skyAdmSupply" style="margin-top:8px;">Загрузка…</div></div>
        <div class="nft-interval-panel">
            <b>🔀 Перевод токенов</b>
            <p class="muted" style="margin:6px 0 8px; font-size:12px;">Откуда и куда: Telegram ID игрока или MERCHANT_POOL / TEAM_POOL / STAKING_POOL.
                Списание и зачисление атомарны: сколько списалось — ровно столько зачислится; не хватает на источнике — ошибка. Общие 10 000 000 не меняются.</p>
            <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
                Откуда <input id="skyAdmFrom" list="skyAdmPools" placeholder="ID или MERCHANT_POOL" style="width:180px;">
                Куда <input id="skyAdmTo" list="skyAdmPools" placeholder="ID или MERCHANT_POOL" style="width:180px;">
                Сколько <input id="skyAdmAmount" type="number" min="0.01" step="0.01" style="width:130px;">
                <input id="skyAdmNote" placeholder="Комментарий (необязательно)" style="width:220px;">
                <button id="skyAdmTransferBtn" style="font-weight:800;">Перевести</button>
            </div>
            <datalist id="skyAdmPools"><option value="MERCHANT_POOL"><option value="TEAM_POOL"><option value="STAKING_POOL"></datalist>
        </div>
        <div class="nft-interval-panel"><b>Журнал операций (последние 60)</b><div id="skyAdmLog" style="margin-top:8px; overflow-x:auto;">—</div></div>`;
    main.appendChild(page);
    // фильтр «Сделки рынка»: отдел токена
    const tm = document.getElementById('tradesMarket');
    if (tm && !tm.querySelector('option[value="skylords"]')) {
        const o = document.createElement('option'); o.value = 'skylords'; o.textContent = 'Токен SkyLords'; tm.appendChild(o);
    }

    btn.onclick = () => {
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        page.classList.add('active');
        load();
    };
    // уход на другую страницу админки — наша гаснет (их обработчик про неё не знает)
    document.querySelectorAll('.nav-btn[data-page]').forEach(b => b.addEventListener('click', () => { btn.classList.remove('active'); page.classList.remove('active'); }));

    async function load() {
        try { render(await api('/admin/api/skylords')); }
        catch (e) { document.getElementById('skyAdmSupply').textContent = e.message; }
    }
    function render(d) {
        document.getElementById('skyAdmStatus').innerHTML = `Сейчас: <b>${esc(STATUS[d.status] || d.status)}</b>`;
        document.getElementById('skyAdmStatusSel').value = d.status;
        const p = d.pools || {};
        const row = (k, v, hint) => `<tr><td>${k}</td><td style="text-align:right"><b>${fmt(v)}</b></td><td class="muted">${hint || ''}</td></tr>`;
        document.getElementById('skyAdmSupply').innerHTML = `
            <div style="margin-bottom:8px; font-weight:800; color:${d.balanced ? '#4ade80' : '#f87171'}">
                ${d.balanced ? '✅ Сходится' : '❌ НЕ СХОДИТСЯ'}: ${fmt(d.total)} из ${fmt(d.expected)}</div>
            <table>
                ${row('Пул Купца (MERCHANT_POOL)', p.merchant, '1000 токенов = 1 GRAM')}
                ${row('Пул команды (TEAM_POOL)', p.team)}
                ${row('Пул наград стейкинга (STAKING_POOL)', p.staking)}
                ${row('Балансы игроков', d.balances, `держателей: ${fmt(d.holders)}`)}
                ${row('В стейкинге', d.staked, `стейкеров: ${fmt(d.stakers)}`)}
                ${row('На рынке (лоты)', d.market, `лотов: ${fmt(d.lots)}`)}
                ${row('🔥 Сожжено навсегда', p.burned)}
                ${row('В обороте (всё, кроме сожжённого)', d.circulating)}
                ${row('Начислено к выдаче (ещё в пуле стейкинга)', d.claimable_promised, 'выдаётся из пула при «Забрать»')}
            </table>`;
        const logs = d.log || [];
        document.getElementById('skyAdmLog').innerHTML = logs.length ? `<table>
            <tr><th>Время</th><th>Операция</th><th>Игрок</th><th style="text-align:right">Токенов</th><th>Детали</th></tr>
            ${logs.map(l => {
                const extra = Object.keys(l).filter(k => !['ts', 'kind', 'user_id', 'units'].includes(k)).map(k => `${esc(k)}: ${esc(typeof l[k] === 'object' ? JSON.stringify(l[k]) : l[k])}`).join(' · ');
                return `<tr><td>${new Date(l.ts * 1000).toLocaleString('ru-RU')}</td><td>${esc(KIND[l.kind] || l.kind)}</td><td>${esc(l.user_id == null ? '—' : l.user_id)}</td>
                    <td style="text-align:right">${fmt((Number(l.units) || 0) / 100)}</td><td class="muted" style="white-space:normal">${extra}</td></tr>`;
            }).join('')}</table>` : '<span class="muted">Операций пока нет</span>';
    }

    let confirmUntil = 0;
    function ask(msg) {
        if (Date.now() < confirmUntil) return false;   // второй клик двойного клика после закрытия окна
        const ok = confirm(msg);
        confirmUntil = Date.now() + 400;
        return ok;
    }
    document.getElementById('skyAdmStatusBtn').onclick = async () => {
        const status = document.getElementById('skyAdmStatusSel').value;
        if (!ask(`Сменить статус токена на «${status}»?\n${STATUS[status]}`)) return;
        try {
            await api('/admin/api/skylords/status', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
            toast('Статус сохранён: ' + status);
            load();
        } catch (e) { toast(e.message, true); }
    };
    document.getElementById('skyAdmTransferBtn').onclick = async () => {
        const source = document.getElementById('skyAdmFrom').value.trim();
        const target = document.getElementById('skyAdmTo').value.trim();
        const amount = Number(document.getElementById('skyAdmAmount').value);
        const note = document.getElementById('skyAdmNote').value.trim();
        if (!source || !target || !(amount > 0)) { toast('Заполни «Откуда», «Куда» и сумму', true); return; }
        if (!ask(`Перевести ${fmt(amount)} токенов\nиз ${source}\nв ${target}?`)) return;
        try {
            const r = await api('/admin/api/skylords/transfer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source, target, amount, note }) });
            toast(`Переведено ${fmt(r.amount)} токенов`);
            document.getElementById('skyAdmAmount').value = '';
            load();
        } catch (e) { toast(e.message, true); }
    };
})();
