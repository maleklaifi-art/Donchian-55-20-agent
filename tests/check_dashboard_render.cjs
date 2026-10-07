// Offline check of the actual Donchian dashboard renderer.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('__CSRF_TOKEN_JSON__', '"test"');
new vm.Script(source); // Validate the entire dashboard script.

// Every $('id') reference must exist in the markup.
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
for (const [, ref] of source.matchAll(/\$\('([^']+)'\)/g)) {
  assert.ok(ids.has(ref), `missing element id: ${ref}`);
}
assert.match(html, /Donchian 55\/20 Demo Agent/);
assert.ok(!/NR7|Supertrend/.test(html));

const elements = new Map();
const listeners = [];
const context = {
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {innerHTML: '', style: {}, value: '', checked: false, textContent: '', className: '', disabled: false});
      return elements.get(id);
    },
    querySelectorAll() { return []; },
    addEventListener() {},
  },
  setInterval() {},
  fetch() { return Promise.reject(new Error('offline')); },
  location: {},
};
vm.createContext(context);
// Execute real rendering functions without network refresh or event listeners.
vm.runInContext(source.slice(0, source.indexOf("  $('startBtn').addEventListener")) +
  '\n globalThis.renderOpen = renderOpen; globalThis.renderClosed = renderClosed;' +
  ' globalThis.renderEntrySignals = renderEntrySignals; globalThis.renderExitSignals = renderExitSignals;' +
  ' globalThis.renderNoAction = renderNoAction; globalThis.renderSettings = renderSettings;' +
  ' globalThis.render = render;\n})();', context);

const openRow = {symbol: 'BTCUSDT', signal_key: 'BTCUSDT:1', entry_price: 60000, current_price: 61000,
  entry_channel: 59000, exit_channel: 55000, distance_to_exit_pct: 10.9, atr: 900,
  risk_usdt: 2, quantity: 0.000333, opened_at: '2026-10-01T00:00:00Z',
  unrealized_pnl_usdt: 0.33, unrealized_pct: 1.67};
context.renderOpen([openRow]);
let output = elements.get('openCards').innerHTML;
assert.match(output, /Exit channel \(20d low\)<\/span><b>55,000<\/b>/);
assert.match(output, /Entry channel \(55d high\)<\/span><b>59,000<\/b>/);
assert.match(output, /Distance to exit channel/);
assert.match(output, /ATR\(20\)<\/span><b>900<\/b>/);
assert.match(output, /No stop order/);

context.renderClosed([{symbol: 'ETHUSDT', outcome: 'CHANNEL_EXIT', entry_price: 2400, exit_price: 2300,
  quantity: 0.5, pnl_usdt: -50, realized_r: -1, opened_at: '2026-09-20T00:00:00Z',
  closed_at: '2026-10-01T00:00:00Z',
  payload_json: JSON.stringify({exit_channel: 2320})}]);
output = elements.get('closedPanel').innerHTML;
assert.match(output, /Channel exit/);
assert.match(output, /2,320/);

context.renderEntrySignals([{symbol: 'SOLUSDT', close_time_utc: '2026-10-01T00:00:00Z', entry: 160,
  entry_channel: 155, exit_channel: 140, atr: 6, stop_distance: 12, universe: 'Core 10'}]);
output = elements.get('entryPanel').innerHTML;
assert.match(output, /55-day high/);
assert.match(output, /Sizing distance \(2×ATR\)/);

context.renderExitSignals([{symbol: 'XRPUSDT', close_time_utc: '2026-10-01T00:00:00Z', entry: 2.4,
  entry_channel: 2.6, exit_channel: 2.5, atr: 0.1, stop_distance: 0.2, universe: 'Core 10'}]);
output = elements.get('exitPanel').innerHTML;
assert.match(output, /20-day low \(exit\)/);
assert.match(output, /Below channel by/);

context.renderNoAction([{symbol: 'ADAUSDT', price: 0.9, stage: 'Entry', reason: 'Close did not exceed',
  entry_channel: 0.95, exit_channel: 0.8, atr: 0.03, holding: false}]);
output = elements.get('noactionPanel').innerHTML;
assert.match(output, /No action|Entry/);
assert.match(output, /55-day high/);

const settings = {starting_capital_usdt: 500, risk_per_trade_pct: 1, entry_period: 55, exit_period: 20,
  atr_period: 20, atr_multiplier: 2, daily_history_bars: 120, entry_grace_minutes: 15, scan_workers: 100,
  universes: [{name: 'Core 10', symbols: ['BTCUSDT', 'ETHUSDT']}], active_universe: 'Core 10',
  telegram_enabled: false, telegram_chat_id: '', telegram_token_configured: false,
  active_universe_symbols: ['BTCUSDT'], schedule: 'Daily at 00:00 UTC', strategy_version: 'donchian_55_20_v1'};
context.render({
  meta: {execution_ready: true, new_entries_enabled: true, scan_in_progress: false},
  risk: {starting_capital_usdt: 500, risk_per_trade_pct: 1, sizing_rule: 'risk % of equity / (2 x ATR)',
    max_position_notional_usdt: 100, daily_loss_limit_r: 3, entry_period: 55, exit_period: 20,
    atr_period: 20, atr_multiplier: 2, entry_grace_minutes: 15},
  stats: {demo_balance_usdt: 510, estimated_demo_equity_usdt: 520, available_demo_cash_usdt: 460,
    unrealized_pnl_usdt: 10, realized_pnl_usdt: 10, realized_r: 1, today_pnl_usdt: 0, today_r: 0,
    win_rate: 50, wins: 1, losses: 1, open_count: 1, open_risk_usdt: 2},
  settings,
  scan: {coins_scanned: 10, entry_count: 1, exit_count: 0, filtered: [], error_count: 0, duration_seconds: 1.2},
  open_trades: [], closed_trades: [], entry_signals: [], exit_signals: [], no_action: [],
});
assert.equal(elements.get('balance').textContent, '510 USDT');
assert.equal(elements.get('equity').textContent, '520 USDT');
assert.equal(elements.get('setCapital').value, 500);
assert.equal(elements.get('setUniverseSelect').value, 'Core 10');
assert.match(elements.get('setUniverseSymbols').value, /BTCUSDT, ETHUSDT/);
assert.match(elements.get('strategyRule').textContent, /55\/20 Donchian · ATR\(20\) × 2 · risk 1% · Daily at 00:00 UTC/);
assert.match(elements.get('capitalSummary').textContent, /available demo cash 460/);
assert.match(elements.get('freeUsdt').textContent, /before fees/);
console.log('Donchian dashboard render, settings, universe and balance checks passed.');
