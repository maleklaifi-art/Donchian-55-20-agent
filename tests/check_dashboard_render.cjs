// Offline check of the actual dashboard renderer, including legacy fallback.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('__CSRF_TOKEN_JSON__', '"test"');
new vm.Script(source); // Validate the entire dashboard script.
const elements = new Map();
const context = {
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {innerHTML: '', style: {}});
    return elements.get(id);
  }},
};
vm.createContext(context);
// Execute real rendering functions without network refresh or event listeners.
vm.runInContext(source.slice(0, source.indexOf("  $('startBtn').addEventListener")) +
  '\n globalThis.renderOpen = renderOpen; globalThis.renderClosed = renderClosed; globalThis.render = render;\n})();', context);
const row = {symbol: 'DJTBUSDT', entry_price: 9.15, signal_entry: 9.17,
  signal_rr: 3, rr: 4.142857, tp: 9.44, sl: 9.08, quantity: 2, risk_usdt: .14};
context.renderOpen([row]);
let output = elements.get('openCards').innerHTML;
assert.match(output, /R:R \(signal\)<\/span><b>3<\/b>/);
assert.match(output, /Entry \(fill\)<\/span><b>9.15<\/b>/);
assert.match(output, /Actual fill R:R: 4.14/);
context.renderOpen([{...row, signal_rr: null}]);
output = elements.get('openCards').innerHTML;
assert.match(output, /R:R \(fill\)<\/span><b>4.14<\/b>/);
assert.match(output, /Original signal unavailable/);
context.render({meta: {}, risk: {starting_capital_usdt: 500},
  stats: {demo_balance_usdt: 510, estimated_demo_equity_usdt: 520, available_demo_cash_usdt: 460},
  settings: {starting_capital_usdt: 500}, scan: {filtered: []},
  open_trades: [], closed_trades: [], passed_signals: [], confirmed_signals: [], filtered_signals: []});
assert.equal(elements.get('balance').textContent, '510 USDT');
assert.equal(elements.get('equity').textContent, '520 USDT');
assert.equal(elements.get('setCapital').value, 500);
assert.match(elements.get('capitalSummary').textContent, /available demo cash 460/);
assert.match(elements.get('freeUsdt').textContent, /before fees/);
console.log('Dashboard R:R, fill, legacy fallback, capital and balance checks passed.');
