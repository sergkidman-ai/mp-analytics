// Open-month selection leaves closed report columns untouched.
window.mpReportPeriodQuery = function () {
  const value = new URLSearchParams(location.search).get('period');
  const box = document.querySelector('[data-open-report-period]');
  return value && (!box || value.slice(0, 7) > box.dataset.closed)
    ? '?period=' + encodeURIComponent(value) : '';
};

window.mpReportSetShare = function (td, amount, turnover) {
  td.querySelectorAll('span.pc').forEach(span => span.remove());
  if (!Number.isFinite(amount) || !Number.isFinite(turnover) || turnover === 0) return;
  const share = document.createElement('span');
  share.className = 'pc';
  share.textContent = (amount / turnover * 100).toFixed(1) + '%';
  td.append(share);
};

window.mpReportLiveShares = function (current, platform, tables) {
  const base = {ozon: 'sales', wb: 'own_price', yandex: 'own'}[platform];
  const actual = platform === 'ozon' ? 'jul' : 'cur';
  for (const table of tables) {
    const account = table.closest('section.org').dataset.acc;
    const cells = (current.accounts || {})[account] || {};
    for (const row of table.querySelectorAll('tr[data-showpc="1"]:not([data-det-of])')) {
      for (const column of [actual, 'fc']) {
        const td = row.querySelector('td.live[data-c="' + column + '"]');
        if (!td) continue;
        const value = cells[row.dataset.k] && cells[row.dataset.k][column];
        const turnover = cells[base] && cells[base][column];
        window.mpReportSetShare(td, value && value.value, turnover && turnover.value);
      }
    }
  }
};

// Annual amounts combine saved months and actual open months exactly once.
// Forecast cells are deliberately excluded; unknown amounts remain unknown.
window.mpReportYearTotals = async function (current) {
  const box = document.querySelector('[data-open-report-period]');
  const tables = [...document.querySelectorAll('#mpr section.org table')];
  if (!box || !current || !current.month || !tables.length) return;
  const platform = box.dataset.platform;
  window.mpReportLiveShares(current, platform, tables);
  const endpoint = {ozon: '/api/ozon/mp-current', wb: '/api/wb/mp-current',
    yandex: '/api/yandex/mp-current'}[platform];
  const selected = current.month.month_key;
  const year = selected.slice(0, 4);
  const periods = Array.from({length: Number(selected.slice(5, 7))},
    (_, i) => year + '-' + String(i + 1).padStart(2, '0'));
  const saved = new Set([...tables[0].querySelectorAll('td[data-period]')]
    .map(td => td.dataset.period));
  const actuals = new Map([[selected, current]]);
  const caption = 'Факт с января по ' + selected + ', без прогноза. ' +
    'Включены открытые месяцы без отдельного столбца. Незакрытые месяцы предварительные; ' +
    'маржа и средний чек рассчитаны из сумм.';
  tables.forEach(table => {
    const head = table.querySelector('th.year-total');
    if (head) { head.textContent = 'Итого ' + year; head.title = caption; }
  });
  try {
    await Promise.all(periods.filter(p => !saved.has(p) && p !== selected).map(async p => {
      const response = await fetch(endpoint + '?period=' + encodeURIComponent(p));
      if (!response.ok) throw new Error('Annual report source unavailable');
      const data = await response.json();
      if (!data.month || data.month.month_key !== p) throw new Error('Annual month missing');
      actuals.set(p, data);
    }));

    for (const table of tables) {
      const account = table.closest('section.org').dataset.acc;
      let rows = new Map([...table.querySelectorAll('tr[data-k]')].map(tr => [tr.dataset.k, tr]));
      // A service may exist only in an earlier open month, not the selected one.
      for (const data of actuals.values()) {
        for (const [key, cell] of Object.entries((data.accounts || {})[account] || {})) {
          if (!key.startsWith('d:') || rows.has(key)) continue;
          const line = key.split(':')[1];
          const parent = rows.get(line);
          if (!parent) continue;
          const tr = parent.cloneNode(true);
          tr.className = 'sub det d-' + line + (parent.classList.contains('open') ? ' open' : '');
          tr.dataset.k = key;
          tr.dataset.detOf = line;
          delete tr.dataset.det;
          tr.querySelectorAll('td').forEach((td, i) => {
            if (!i) { td.className = 'lbl ind'; td.textContent = cell.label || key; }
            else if (td.hasAttribute('data-period')) {
              td.className = 'num muted'; td.textContent = '—'; td.dataset.value = 'null';
              td.removeAttribute('data-ym');
            } else if (td.classList.contains('year-total')) td.textContent = '—';
            else if (td.classList.contains('live')) {
              const entry = ((current.accounts || {})[account] || {})[key];
              td.className = 'num live' + (td.dataset.c === 'fc' ? ' fc' : '');
              td.textContent = entry && entry[td.dataset.c] ? entry[td.dataset.c].txt : '0';
            } else { td.textContent = ''; }
          });
          if (!parent.dataset.det) {
            parent.dataset.det = line;
            parent.classList.add('hasdet');
            const label = parent.querySelector('td.lbl');
            const marker = document.createElement('span');
            marker.className = 'detbtn'; marker.textContent = '▸'; label.prepend(marker);
          }
          let anchor = parent;
          table.querySelectorAll('tr[data-det-of]').forEach(sibling => {
            if (sibling.dataset.detOf === line) anchor = sibling;
          });
          anchor.after(tr);
          rows.set(key, tr);
        }
      }

      const totals = new Map();
      for (const [key, tr] of rows) {
        const history = new Map([...tr.querySelectorAll('td[data-period]')]
          .map(td => [td.dataset.period, td.dataset.value === 'null' ? null : Number(td.dataset.value)]));
        const values = periods.map(p => {
          if (history.has(p)) return history.get(p);
          const cell = (((actuals.get(p) || {}).accounts || {})[account] || {})[key];
          const actual = cell && (cell.cur || cell.jul);
          return actual ? actual.value : key.startsWith('d:') ? 0 : null;
        });
        totals.set(key, values.every(v => typeof v === 'number' && Number.isFinite(v))
          ? values.reduce((a, b) => a + b, 0) : null);
      }
      const turnover = totals.get({ozon: 'sales', wb: 'own_price', yandex: 'own'}[platform]);
      const orders = totals.get('orders');
      const net = totals.get('net');
      const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
      for (const tr of table.querySelectorAll('tr[data-k]')) {
        const key = tr.dataset.k;
        const td = tr.querySelector('td.year-total');
        if (!td) continue;
        let value = totals.get(key);
        const kind = tr.dataset.kind;
        if (kind === 'margin') value = turnover && net != null ? net / turnover * 100 : null;
        if (kind === 'check') value = orders && turnover != null ? turnover / orders : null;
        td.dataset.value = value == null ? 'null' : String(value);
        td.title = value == null ? 'Нет полной детализации или суммы за все месяцы выбранного периода' : caption;
        td.classList.toggle('muted', value == null);
        if (value == null) td.textContent = '—';
        else if (kind === 'margin') td.textContent = value.toFixed(1) + '%';
        else {
          const fraction = Math.abs(value) % 1;
          const rounded = Math.abs(fraction - 0.5) < 1e-9
            ? Math.sign(value) * (Math.floor(Math.abs(value)) + (Math.floor(Math.abs(value)) % 2))
            : Math.round(value);
          const sign = kind === 'expense' ? (rounded > 0 ? '−' : rounded < 0 ? '+' : '')
            : rounded < 0 ? '−' : '';
          td.textContent = sign + number.format(Math.abs(rounded));
        }
        if (tr.dataset.showpc === '1' && !tr.dataset.detOf) {
          window.mpReportSetShare(td, value, turnover);
        }
      }
      table.dataset.yearStatus = 'complete';
    }
  } catch (_) {
    tables.forEach(table => {
      table.dataset.yearStatus = 'unavailable';
      table.querySelectorAll('td.year-total').forEach(td => {
        td.textContent = '—'; td.title = 'Не все фактические месяцы доступны; неполный итог не показан';
      });
    });
  }
};
document.addEventListener('DOMContentLoaded', async function () {
  const box = document.querySelector('[data-open-report-period]');
  if (!box) return;
  try {
    const response = await fetch('/api/business/meta');
    if (!response.ok) return;
    const meta = await response.json();
    const periods = [...new Set(meta.slices.filter(s => s.platform === box.dataset.platform)
      .map(s => s.period_from.slice(0, 7)).filter(p => p > box.dataset.closed))].sort();
    if (!periods.length) return;
    const names = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
    const select = document.createElement('select');
    const selected = new URLSearchParams(location.search).get('period');
    periods.forEach(p => select.add(new Option(names[Number(p.slice(5)) - 1] + ' ' + p.slice(0, 4), p)));
    select.value = selected && periods.includes(selected.slice(0, 7)) ? selected.slice(0, 7) : periods[0];
    select.addEventListener('change', function () {
      const url = new URL(location.href);
      url.searchParams.set('period', select.value);
      location.assign(url);
    });
    const label = document.createElement('label');
    label.textContent = 'Открытый отчёт: ';
    label.append(select);
    box.append(label);
  } catch (_) { /* Existing report remains usable if metadata is unavailable. */ }
});
