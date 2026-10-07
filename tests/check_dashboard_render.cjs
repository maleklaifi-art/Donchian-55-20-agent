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
    if (!elements.has(id)) elements.set(id, {innerHTML: ''});
    return elements.get(id);
  }},
};
vm.createContext(context);
// Execute real rendering functions without network refresh or event listeners.
vm.runInContext(source.slice(0, source.indexOf("  $('startBtn').addEventListener")) +
  '\n globalThis.renderOpen = renderOpen; globalThis.renderClosed = renderClosed;\n})();', context);
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
console.log('Dashboard signal R:R, actual fill and legacy fallback checks passed.');
