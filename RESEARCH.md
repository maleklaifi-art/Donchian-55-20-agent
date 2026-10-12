> **Archived:** This job is no longer started or exposed by the app. Existing stored records are retained. Only demo trading and decision/outcome tracking run.

# Autonomous demo research

The existing daily Testnet execution engine remains responsible for orders. A separate
worker records and tests entry-rule changes. It cannot execute arbitrary generated
code, place orders, increase risk, change capital, change the coin list, or change exits.

## Baseline and open trades

The revision-6 upgrade sets future entries to Donchian **55/20, ADX(14) > 25** and
the agreed 19 coins, preserving funding, risk settings, trade history and credentials.
The active list is BTC, ETH, BNB, SOL, XRP, ADA, AVAX, DOT, LINK, LTC, ATOM, BCH,
TRX, NEAR, UNI, XLM, ALGO, VET and FIL (all USDT pairs).

Existing open positions retain all their original fill, quantity, cost, pending-order
and exit data. Their original exit-period setting is frozen in position metadata,
with a durable migration hint to survive a restart between migration steps. New
positions also store their strategy version and exit period. Research adoption
changes **future entry decisions only**. A normal original-rule exit can still close
an existing position. Research audit errors do not interrupt exits.

## What happens and when

- **Daily at 00:00 UTC:** the existing scanner evaluates completed candles, records
  each observed coin's action/filter/ADX/version, and executes through the existing
  Testnet path. Missing/unavailable data are explicit decisions, never fabricated
  candles. A later valid retry may repair a data-failure record. Valid original
  observations and fills are preserved on rescans.
- **Daily as outcomes mature:** record 1/7/30-day changes, maximum rise and worst
  fall from the next daily open, plus the original channel-exit counterfactual.
  Both winners and losers remain visible. These are descriptive per-signal
  outcomes, not executable portfolio profits; unobserved earlier decisions are
  never invented from historical candles.
- **Weekly by default:** analyze recorded outcomes and complete cached market
  history; propose one bounded entry change; test; compare; adopt, reject or keep
  observing. The dashboard shows each stage, timeline, reason and metrics.
- **Fresh forward checkpoints:** 90, 180 and 360 days after prospective registration
  by default. No historical result alone can authorize adoption. Too few trades
  extends observation to the next predeclared checkpoint; the final checkpoint
  rejects an inconclusive candidate. Repeated daily peeking is avoided.

Only one candidate runs at a time. Allowed changes are ADX threshold ±5 within
15–40, or entry period ±5 within 40–80. Exit rules and risk remain fixed. Previously
examined configurations require 90 new days before another test, which gets a
new trial number and a new forward period. Weekly reviews can legitimately retain
the current strategy; improvement is not guaranteed.

## Evidence and promotion

Historical diagnostics use up to three years of common 19-coin history, including
at least 500 warmup bars and two separate subwindows. Known history is labelled
as diagnostic, never an unseen holdout. Candidate and active simulations start flat
on identical dates with identical capital, fees, slippage and deterministic list-order
cash allocation. A separate original paper reference starts on the next full day
and remains unchanged after promotions.

The forward gate requires at least 20 closed trades and five traded coins in **each**
portfolio, candidate net return above both zero and the active return, no greater
maximum drawdown, and a positive paired daily-return block-bootstrap lower bound.
The bootstrap uses 5,000 seeded samples, seven-day blocks, and an alpha budget
adjusted across trials and the three declared checkpoints. This is an operational
screen, not statistical proof of an edge. Daily bars, floating-point quantities and
fixed costs do not model exchange lot filters, spread, depth, outages or Testnet
fills exactly. Open positions are marked to market, not forcibly liquidated at
window end. Hypothetical tests never replace actual demo P&L.

Promotion is serialized with existing execution and deferred during scans. A manual
change to the experiment's source strategy/capital/cost/universe invalidates it.
Restart recovery cannot silently rerun a checkpoint or adopt an unfinished test.
Research and auto-adoption can be paused separately in dashboard settings.

## Proposal source

Without extra credentials, the **rule-based bounded proposer** runs automatically.
Matured positive/negative outcomes rank entry hypotheses; historical and prospective
tests determine whether any change is accepted. It does not train a neural network.

For Claude-generated hypothesis selection, configure both `ANTHROPIC_API_KEY` and
`RESEARCH_ANTHROPIC_MODEL` on the service. The model receives aggregate research
summaries and a fixed list of candidate options. Its JSON must select one allowed
option; invalid output fails the cycle. It has no exchange credentials or execution
tools. Model usage may incur API charges. The dashboard identifies which proposer
was actually used; there is no claim that an LLM is configured when it is not.

## Persistence and operation

Existing `trades.sqlite3` gains `research_meta`, `research_cycles` and `daily_decisions`
tables. It remains on the same persistent DATA_DIR volume, alongside the original
position state and full-from-listing candle cache. No new deployment or account is
required. Research waits when any required coin lacks complete current history.
Authenticated endpoints expose paginated decisions and individual archived cycles.
The dashboard displays the latest 30 cycles; all cycles remain stored.

The background worker belongs to the deployed service. Closing a browser has no
effect; stopping the service stops both research and demo execution. No new trade
or live service mutation is performed by the offline test suite.
