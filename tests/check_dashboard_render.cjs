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
  ' globalThis.renderNoAction = renderNoAction; globalThis.renderSettings = (s)=>{settingsInitialized=false;renderSettings(s);};' +
  ' globalThis.render = render; globalThis.upsertUniverse = upsertUniverse; globalThis.settingsPayload = settingsPayload; globalThis.saveSettings = saveSettings; globalThis.loadUniverseInputs = loadUniverseInputs;\n})();', context);

const openRow = {symbol: 'BTCUSDT', signal_key: 'BTCUSDT:1', entry_price: 60000, current_price: 61000,
  entry_channel: 59000, exit_channel: 55000, distance_to_exit_pct: 10.9, atr: 900,
  risk_usdt: 2, quantity: 0.000333, opened_at: '2026-10-01T00:00:00Z',
  unrealized_pnl_usdt: 0.33, unrealized_pct: 1.67};
context.renderOpen([openRow]);
let output = elements.get('openCards').innerHTML;
assert.match(output, /Exit channel \(20d low\)<\/span><b>55,000<\/b>/);
assert.match(output, /Entry channel \(at entry\)<\/span><b>59,000<\/b>/);
assert.match(output, /Distance to exit channel/);
assert.match(output, /ATR at entry<\/span><b>900<\/b>/);
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
    max_position_notional_usdt: null, daily_loss_limit_r: null, entry_period: 55, exit_period: 20,
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
assert.equal(elements.get('setUniverseName').value, 'Core 10');
for(const removed of ['setUniverseSelect','setUniverseNew','setUniverseSave','setUniverseActivate','setUniverseDelete']) assert.ok(!ids.has(removed));
assert.match(elements.get('setUniverseSymbols').value, /BTCUSDT, ETHUSDT/);
assert.match(elements.get('strategyRule').textContent, /55\/20 Donchian · ATR\(20\) × 2 · risk 1% · Daily at 00:00 UTC/);
assert.match(elements.get('capitalSummary').textContent, /available demo cash 460/);
assert.match(elements.get('freeUsdt').textContent, /new trades include fees/);
assert.match(elements.get('engineBody').innerHTML, /no fixed position-value cap/);
assert.doesNotMatch(elements.get('engineBody').innerHTML, /max 0 USDT|100 USDT\/position/);
console.log('Donchian dashboard render, settings, universe and balance checks passed.');

// Edit the active list in place, including renaming, without a second record.
elements.get('setUniverseName').value = 'My coins';
elements.get('setUniverseSymbols').value = 'BTCUSDT, SOLUSDT, SOLUSDT';
context.upsertUniverse();
let payload = JSON.parse(JSON.stringify(context.settingsPayload()));
assert.equal(payload.universes.length, 1);
assert.equal(payload.active_universe, 'My coins');
assert.deepEqual(payload.universes[0], {name:'My coins', symbols:['BTCUSDT','SOLUSDT']});
// Case-only renaming also preserves the selected record.
elements.get('setUniverseName').value = 'MY COINS';
context.upsertUniverse();
assert.equal(context.settingsPayload().active_universe, 'MY COINS');
// Renaming into another saved name must not overwrite that list.
context.renderSettings({...settings, universes:[...settings.universes, {name:'Other', symbols:['LTCUSDT']}]});
elements.get('setUniverseName').value = 'Other';
assert.throws(()=>context.upsertUniverse(), /already uses this name/);
assert.equal(context.settingsPayload().universes.length, 2);
assert.equal(context.settingsPayload().active_universe, 'Core 10');
// Simplifying the editor must preserve any previously saved inactive records.
elements.get('setUniverseName').value = 'Active renamed';
context.upsertUniverse();
assert.equal(context.settingsPayload().active_universe, 'Active renamed');
assert.equal(context.settingsPayload().universes.length, 2);
assert.deepEqual(JSON.parse(JSON.stringify(context.settingsPayload().universes[1])), {name:'Other',symbols:['LTCUSDT']});
// SAVE SETTINGS directly captures edits to the single visible active list.
context.renderSettings(settings);
elements.get('setUniverseName').value = 'Direct edit';
elements.get('setUniverseSymbols').value = 'ETHUSDT,LINKUSDT';
const posted=[];
context.fetch = async (url, options) => {
  if(url==='/api/config'){
    posted.push(JSON.parse(options.body));
    return {ok:true, json:async()=>({ok:true})};
  }
  return {ok:false, status:503}; // No live dashboard refresh in this offline test.
};
(async()=>{
  await context.saveSettings();
  assert.equal(posted.length,1);
  assert.equal(posted[0].universes.length,1);
  assert.equal(posted[0].active_universe,'Direct edit');
  assert.deepEqual(posted[0].universes[0].symbols,['ETHUSDT','LINKUSDT']);
  console.log('Universe add/remove, rename, collision and direct-save checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Research states and model-generated strings are rendered through the real UI.
vm.runInContext('globalThis.renderResearchForTest=undefined', context);
const researchSource=source.slice(0,source.indexOf("  $('startBtn').addEventListener")) +
 '\n globalThis.renderResearch=renderResearch; globalThis.renderDecisions=renderDecisions;\n})();';
vm.runInContext(researchSource,context);
context.renderResearch({enabled:true,auto_adopt:true,review_days:7,trial_count:0,cycles:[],decisions:{total:0,rows:[]}});
assert.match(elements.get('cycleTrack').innerHTML,/Analyze/);
assert.match(elements.get('cycleTrack').innerHTML,/Adopt or reject/);
assert.match(elements.get('researchHypothesis').innerHTML,/No results are fabricated/);
context.renderResearch({enabled:true,auto_adopt:true,review_days:7,trial_count:1,
 active_version:'v1',proposer:'Rule-based',notice:'Observing',cycles:[{id:1,status:'OBSERVING',stage:'decide',
 payload:{hypothesis:'<img src=x onerror=alert(1)>',analysis:{note:'All results',groups:[{stage:'ADX',decisions:19,matured_7d:10,positive_7d:4,avg_7d_net_pct:-2}]},
 timeline:[{stage:'analyze',message:'Collected'},{stage:'decide',message:'Observe'}]}}],decisions:{total:0,rows:[]}});
assert.match(elements.get('cycleTrack').innerHTML,/Keep observing/);
assert.doesNotMatch(elements.get('researchHypothesis').innerHTML,/<img/);
assert.match(elements.get('researchHypothesis').innerHTML,/&lt;img/);
assert.match(elements.get('researchAnalysis').innerHTML,/-2/);
context.renderDecisions({total:1,rows:[{symbol:'AAAUSDT',action:'NO_ACTION',execution:'SKIPPED',reason:'ADX below threshold',payload:{adx:24,adx_threshold:25},outcomes:{'7':{return_pct:10}},strategy_version:'v1'}]});
assert.match(elements.get('decisionsTable').innerHTML,/10.00%/);
assert.match(elements.get('decisionsTable').innerHTML,/ADX below threshold/);
assert.equal(elements.get('decisionNext').disabled,true);
console.log('Research stages, missed opportunities, negative results, empty state and escaping checks passed.');

// Independent pattern research remains readable for waiting and trained states.
vm.runInContext(source.slice(0,source.indexOf("  $('startBtn').addEventListener")) +
 '\n globalThis.renderPatterns=renderPatterns;\n})();',context);
context.renderPatterns({enabled:true,status:{stage:'WAITING_DATA',message:'Waiting for BTCUSDT history'},models:[],forecasts:{total:0,rows:[]}});
assert.match(elements.get('patternNotice').textContent,/Waiting for BTCUSDT/);
assert.match(elements.get('patternTraining').textContent,/730/);
assert.match(elements.get('patternRules').innerHTML,/No records yet/);
const metrics={return_pct:-8,net_profit:-16,max_drawdown_pct:14,closed_trades:67,open_positions:0,fee_bps:25,slippage_bps:5};
context.renderPatterns({enabled:true,models:[{id:1,created_ms:1,training_rows:20000,verdict:'Diagnostic only',
 validation:{label:'Chronological test',rmse_pct:10,mean_baseline_rmse_pct:9,beats_mean_baseline:false,folds:[]},
 historical:{pattern:metrics,donchian:{...metrics,return_pct:6},label:'Past diagnostics'},
 rules:[{id:'root<script>',conditions:[{feature:'return_7d_pct',op:'<=',value:2}],samples:200,days:40,prediction_pct:-3,positive_fraction:.4}]}],
 forecasts:{total:1,rows:[{symbol:'BTCUSDT',model_id:1,leaf_id:'rootL',prediction_pct:2,outcome:null}]},scorecard:[],activity:[]});
assert.match(elements.get('patternValidation').innerHTML,/Did not improve/);
assert.match(elements.get('patternComparisons').innerHTML,/Donchian 55\/20 \+ ADX25/);
assert.match(elements.get('patternComparisons').innerHTML,/-8.00%/);
assert.doesNotMatch(elements.get('patternRules').innerHTML,/<script>/);
assert.match(elements.get('patternRules').innerHTML,/&lt;script&gt;/);
assert.match(elements.get('patternForecasts').innerHTML,/Pending/);
assert.equal(elements.get('patternNext').disabled,true);
console.log('Pattern training, failure evidence, paper comparison, pending forecasts and escaping checks passed.');

// Explorer evidence: controls, single changes, failures, dates and escaping.
vm.runInContext(source.slice(0, source.indexOf("  $('startBtn').addEventListener")) +
 '\n globalThis.renderExplorer=renderExplorer; globalThis.renderExplorerEvidence=renderExplorerEvidence;\n})();',context);
context.renderExplorer({enabled:true,status:{stage:'DOWNLOADING',message:'History ready 0/19'},spec:{weekly_budget:12,forward_slots:3},catalogue_size:363,trials:[],attempted:0,recent_attempts:0});
assert.match(elements.get('explorerNotice').textContent,/0\/19/);
assert.match(elements.get('explorerSummary').innerHTML,/1H · 4H · 1D/);
assert.match(elements.get('explorerSpec').innerHTML,/cannot guarantee/);
context.renderExplorerEvidence({id:2,registered_ms:1,payload:{candidate:{kind:'CONTROLLED_CHANGE',hypothesis:'<script>bad</script>',parent_id:'1h:breakout:v1:none',change:{factor:'entry_bars',before:20,after:55}},
 historical:{candidate:metrics,baseline:{...metrics,return_pct:6},parent:{...metrics,return_pct:8},gates:{parent_net_return:false},folds:[]},
 timeline:[{time_ms:1,status:'REJECTED',message:'<script>bad</script>'}]}});
assert.match(elements.get('explorerHypothesis').textContent,/Single change: entry_bars, 20 → 55/);
assert.match(elements.get('explorerComparisons').innerHTML,/Unchanged parent/);
assert.match(elements.get('explorerGates').innerHTML,/FAIL \/ INSUFFICIENT/);
assert.doesNotMatch(elements.get('explorerTimeline').innerHTML,/<script>/);
assert.match(elements.get('explorerTimeline').innerHTML,/&lt;script&gt;/);
console.log('Explorer empty state, single-change controls, evidence, failure and escaping checks passed.');
