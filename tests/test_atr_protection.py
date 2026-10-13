"""Exchange-faithful state transitions for the uploaded Pine stop strategy."""
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch, Mock

import pytest

from atr_protection import maintain_stop, market_quantity
from demo_trader import (BinanceAPIError, TestnetExecutionClient as Client, allowed_to_trade,
                         close_position, load_state, sync_positions, save_state)
from runtime_settings import RuntimeSettings, SettingsStore
from test_multiple_positions import signal, setup, rule, enter, NOW


class StopExchange:
    def __init__(self, fill=91):
        self.fill = fill
        self.free = 200
        self.held = 0
        self.orders = {}
        self.calls = []
        self.stop_mode = None
        self.buy_lost = False
        self.query_lost = False
        self.cancel_mode = None
        self.bid = 90.9
        self.base_fee = 0

    def book_ticker(self, symbol):
        return {'askPrice':str(self.fill), 'bidPrice':str(self.bid)}

    def account(self):
        locked = sum(float(o['origQty'])-float(o['executedQty']) for o in self.orders.values()
                     if o['type']=='STOP_LOSS' and o['status'] in ('NEW','PARTIALLY_FILLED'))
        return {'canTrade':True,'balances':[{'asset':'USDT','free':str(self.free)},
                                          {'asset':'AAA','free':str(max(0,self.held-locked))}]}

    def market_buy_qty(self, symbol, quantity, cid):
        qty=float(quantity);self.calls.append(('buy',cid,qty))
        assert qty*self.fill<=self.free
        self.free-=qty*self.fill;self.held+=qty-self.base_fee
        result={'orderId':1,'type':'MARKET','status':'FILLED','executedQty':quantity,
                'cummulativeQuoteQty':str(qty*self.fill),'origQty':quantity,
                'fills':[{'qty':quantity,'commission':str(self.base_fee),'commissionAsset':'AAA'}]}
        self.orders[cid]=result
        if self.buy_lost:
            raise TimeoutError('buy response lost')
        return deepcopy(result)

    def stop_loss_sell(self, symbol, quantity, stop, cid):
        self.calls.append(('stop',cid,float(quantity),float(stop)))
        if self.stop_mode=='reject':
            raise BinanceAPIError('Filter failure: LOT_SIZE',-1013)
        if self.bid <= float(stop):
            raise BinanceAPIError('Order would trigger immediately.',-2010)
        assert float(quantity)<=self.held+1e-9
        self.orders[cid]={'orderId':2,'type':'STOP_LOSS','status':'NEW','executedQty':'0',
                         'cummulativeQuoteQty':'0','origQty':quantity,'stopPrice':stop}
        if self.stop_mode=='lost':
            raise TimeoutError('stop response lost')
        return deepcopy(self.orders[cid])

    def order(self, symbol, order_id=None, orig_client_order_id=None):
        if self.query_lost:
            raise TimeoutError('query unavailable')
        if orig_client_order_id not in self.orders:
            raise BinanceAPIError('Order does not exist', -2013)
        return deepcopy(self.orders[orig_client_order_id])

    def cancel_order(self, symbol, cid):
        self.calls.append(('cancel',cid))
        if self.cancel_mode=='unknown':
            raise TimeoutError('cancel unavailable')
        if self.cancel_mode=='trigger':
            self.trigger(cid)
            raise BinanceAPIError('Unknown order', -2011)
        self.orders[cid]['status']='CANCELED'
        return deepcopy(self.orders[cid])

    def trigger(self, cid=None, fraction=1, price=88.5):
        cid=cid or next(k for k,v in self.orders.items() if v['type']=='STOP_LOSS')
        order=self.orders[cid];qty=float(order['origQty'])*fraction
        self.held-=qty;self.free+=qty*price
        order.update(status='FILLED' if fraction==1 else 'PARTIALLY_FILLED',executedQty=str(qty),
                     cummulativeQuoteQty=str(qty*price),
                     fills=[{'qty':str(qty),'commission':'0','commissionAsset':'USDT'}])

    def market_sell_qty(self, symbol, quantity, cid):
        qty=float(quantity);self.calls.append(('sell',cid,qty))
        assert qty<=float(self.account()['balances'][1]['free'])+1e-9
        self.held-=qty;self.free+=qty*self.fill
        result={'orderId':10+len(self.calls),'status':'FILLED','executedQty':quantity,
                'cummulativeQuoteQty':str(qty*self.fill),
                'fills':[{'qty':quantity,'commission':'0','commissionAsset':'USDT'}]}
        self.orders[cid]=result
        return deepcopy(result)


def new_signal(**kwargs):
    return replace(signal(**kwargs),atr_stop_enabled=True,
                   strategy_version='donchian_55_20_adx25_atr_stop_v2')


def sync(exchange, context):
    cfg,market,state,logger=context
    sync_positions(exchange,state,logger,cfg,market,{'AAAUSDT':rule('AAAUSDT')})


def channel_exit(exchange, context):
    cfg,market,state,logger=context
    with patch('demo_trader.time.time',return_value=NOW/1000):
        close_position(signal(direction='EXIT'),market,exchange,{'AAAUSDT':rule('AAAUSDT')},state,cfg,logger)


def test_fill_based_stop_frozen_through_restart_and_actual_stop_fill_once(setup):
    exchange=StopExchange();exchange.base_fee=.0025
    enter(new_signal(atr=1.003),exchange,setup)
    cfg,market,state,logger=setup
    pos=state['open_positions']['AAAUSDT']
    assert pos['entry_price']==91  # signal close was90
    assert pos['fixed_stop_distance']==2.01
    assert pos['fixed_stop_price']==88.99
    assert pos['native_stop_state']=='NEW'
    assert pos['quantity']==pytest.approx(float(exchange.orders[pos['entry_client_order_id']]['executedQty'])-.0025,abs=1e-6)
    assert exchange.calls[-1][2]==pos['quantity']
    assert not state['pending_entries']
    frozen=pos['fixed_stop_price'];state=load_state(cfg.state_file)
    context=(cfg,market,state,logger)
    exchange.bid=150
    sync(exchange,context)
    assert state['open_positions']['AAAUSDT']['fixed_stop_price']==frozen
    assert sum(c[0]=='stop' for c in exchange.calls)==1
    exchange.trigger(price=88.5)
    sync(exchange,context)
    assert not state['open_positions'] and len(state['realized'])==1
    rec=state['realized'][0]
    assert rec['outcome']=='ATR_STOP' and rec['exit_price']==88.5
    assert rec['pnl_usdt']==pytest.approx(rec['quantity']*88.5-rec['entry_cost_usdt'])
    sync(exchange,context)
    assert len(state['realized'])==1 and not any(c[0]=='sell' for c in exchange.calls)


def test_cash_cap_reserves_fee_and_rounds_base_quantity_down(setup):
    exchange=StopExchange(fill=90);exchange.bid=89.9
    enter(new_signal(atr=.1),exchange,setup)
    qty=exchange.calls[0][2]
    assert qty==2.21668
    assert qty*90*1.0025<=200
    assert setup[2]['open_positions']['AAAUSDT']['risk_usdt']<=2


def test_market_and_regular_lot_grids_both_apply():
    r=replace(rule('AAAUSDT'),step_size=Decimal('.003'),market_step_size=Decimal('.002'))
    assert market_quantity(.013,r)==Decimal('.012')


@pytest.mark.parametrize('mode',['normal','trigger','unknown','partial'])
def test_channel_stop_cancel_races_never_double_sell(setup,mode):
    exchange=StopExchange();enter(new_signal(),exchange,setup)
    if mode=='partial':
        exchange.trigger(fraction=.4)
    exchange.cancel_mode={'trigger':'trigger','unknown':'unknown'}.get(mode)
    channel_exit(exchange,setup)
    sells=[c for c in exchange.calls if c[0]=='sell']
    if mode=='unknown':
        assert not sells and setup[2]['open_positions']
        assert not allowed_to_trade(setup[2],setup[0],setup[3])
    else:
        assert not setup[2]['open_positions'] and len(setup[2]['realized'])==1
        assert len(sells)==(0 if mode=='trigger' else 1)
        assert setup[2]['realized'][0]['outcome']==('CHANNEL_EXIT' if mode=='normal' else 'ATR_STOP')
        if mode=='partial':
            assert sells[0][2]==pytest.approx(.6)
        sync(exchange,setup)
        assert len(setup[2]['realized'])==1


def test_lost_buy_response_recovered_without_second_buy_and_protected(setup):
    exchange=StopExchange();exchange.buy_lost=True
    with pytest.raises(TimeoutError):
        enter(new_signal(),exchange,setup)
    cfg,market,_,logger=setup;state=load_state(cfg.state_file)
    assert state['pending_entries'] and not allowed_to_trade(state,cfg,logger)
    sync(exchange,(cfg,market,state,logger))
    assert state['open_positions']['AAAUSDT']['native_stop_state']=='NEW'
    assert sum(c[0]=='buy' for c in exchange.calls)==1
    assert sum(c[0]=='stop' for c in exchange.calls)==1
    assert not state['pending_entries']


def test_lost_stop_response_is_queried_never_resubmitted(setup):
    exchange=StopExchange();exchange.stop_mode='lost'
    enter(new_signal(),exchange,setup)
    assert setup[2]['open_positions']['AAAUSDT']['native_stop_state']=='UNKNOWN'
    assert not allowed_to_trade(setup[2],setup[0],setup[3])
    sync(exchange,setup)
    assert setup[2]['open_positions']['AAAUSDT']['native_stop_state']=='NEW'
    assert sum(c[0]=='stop' for c in exchange.calls)==1
    assert allowed_to_trade(setup[2],setup[0],setup[3])


def test_rejected_stop_flattens_and_accounts_trade(setup):
    exchange=StopExchange();exchange.stop_mode='reject'
    enter(new_signal(),exchange,setup)
    assert not setup[2]['open_positions']
    assert setup[2]['realized'][0]['outcome']=='PROTECTION_FAILURE'
    assert sum(c[0]=='sell' for c in exchange.calls)==1


def test_price_already_below_fixed_stop_flattens_instead_of_widening(setup):
    exchange=StopExchange();exchange.bid=80
    enter(new_signal(),exchange,setup)
    assert not setup[2]['open_positions']
    assert setup[2]['realized'][0]['outcome']=='ATR_STOP'
    assert sum(c[0]=='stop' for c in exchange.calls)==1


def test_native_orders_use_only_testnet_market_stop_api():
    client=object.__new__(Client);client._signed=Mock(return_value={})
    client.stop_loss_sell('AAAUSDT','1','88','dcs1')
    client._signed.assert_called_once_with('POST','/api/v3/order',{
        'symbol':'AAAUSDT','side':'SELL','type':'STOP_LOSS','quantity':'1','stopPrice':'88',
        'newClientOrderId':'dcs1','newOrderRespType':'RESULT'})
    client._signed.reset_mock();client.cancel_order('AAAUSDT','dcs1')
    client._signed.assert_called_once_with('DELETE','/api/v3/order',{'symbol':'AAAUSDT','origClientOrderId':'dcs1'})


def test_strategy_migration_keeps_funding_universe_and_is_one_time(tmp_path):
    p=tmp_path/'settings.json'
    old=asdict(RuntimeSettings(settings_revision=6,starting_capital_usdt=555,entry_period=60,
        exit_period=27,risk_per_trade_pct=.5,adx_threshold=20,
        universes=[{'name':'Mine','symbols':['BTCUSDT','ETHUSDT']}],active_universe='Mine'))
    old.pop('legacy_position_exit_period')
    p.write_text(json.dumps(old))
    settings=SettingsStore(str(p));v=settings.get()
    assert (v.entry_period,v.exit_period,v.adx_period,v.adx_smoothing,v.adx_threshold)==(55,20,14,14,25)
    assert (v.atr_period,v.atr_multiplier,v.risk_per_trade_pct,v.entry_fee_reserve_pct)==(20,2,1,.25)
    assert v.starting_capital_usdt==555 and v.universes==old['universes']
    assert settings.legacy_exit_period==27
    settings.update({'risk_per_trade_pct':.75})
    assert SettingsStore(str(p)).get().risk_per_trade_pct==.75


def test_existing_legacy_position_is_not_retrofitted_or_mutated(setup):
    from test_multiple_positions import position_state
    cfg,market,state,logger=setup
    state['open_positions']['AAAUSDT']=position_state()
    before=deepcopy(state);save_state(cfg.state_file,state);raw=Path(cfg.state_file).read_bytes()
    exchange=StopExchange()
    sync(exchange,setup)
    assert state==before and Path(cfg.state_file).read_bytes()==raw
    assert not exchange.calls


def test_unknown_stop_blocks_channel_sell_and_new_entry_until_resolved(setup):
    exchange=StopExchange();enter(new_signal(),exchange,setup)
    exchange.query_lost=True
    channel_exit(exchange,setup)
    assert not any(c[0]=='sell' for c in exchange.calls)
    assert setup[2]['open_positions']['AAAUSDT']['pending_exit_signal']
    exchange.query_lost=False
    sync(exchange,setup)
    assert len(setup[2]['realized'])==1
    assert sum(c[0]=='sell' for c in exchange.calls)==1


def test_stop_submission_crash_never_reuses_unknown_client_id(setup):
    exchange=StopExchange();exchange.stop_mode='lost'
    enter(new_signal(),exchange,setup)
    exchange.orders={k:v for k,v in exchange.orders.items() if v['type']!='STOP_LOSS'}
    sync(exchange,setup)
    sync(exchange,setup)
    assert sum(c[0]=='stop' for c in exchange.calls)==1
    assert not any(c[0]=='sell' for c in exchange.calls)
    assert not allowed_to_trade(setup[2],setup[0],setup[3])


def test_skipped_signal_atr_estimate_uses_frozen_signal_atr_and_gap(tmp_path):
    from research_store import ResearchStore
    from database import TradeDatabase
    from decision_tracker import config_for
    from strategy import Candle, DAY_MS
    store=ResearchStore(TradeDatabase(str(tmp_path/'db')))
    cfg=config_for(RuntimeSettings())
    cfg['cost'].update(fee=0,slip=0)
    row={'symbol':'AAAUSDT','signal_close_ms':40*DAY_MS-1,'strategy_version':'v2',
         'action':'NO_ACTION','stage':'ADX','reason':'Below ADX minimum','atr':2,
         'atr_stop_enabled':True,'holding':False}
    store.record_decisions({'decisions':[row]},cfg,True,True)
    bars=[Candle(i*DAY_MS,100,102,99,100,100,(i+1)*DAY_MS-1) for i in range(42)]
    bars[-1]=Candle(41*DAY_MS,90,94,89,93,100,42*DAY_MS-1)
    store.update_outcomes({'AAAUSDT':bars},42*DAY_MS)
    rec=store.decisions()['rows'][0]['outcomes']['strategy_exit']
    assert rec['reason']=='ATR_STOP_GAP' and rec['stop_price']==96
    assert rec['net_pct']==pytest.approx(-10)


def test_definitive_buy_rejection_releases_intent_without_phantom_position(setup):
    exchange=StopExchange()
    exchange.market_buy_qty=Mock(side_effect=BinanceAPIError('Account has insufficient balance for requested action.',-2010))
    enter(new_signal(),exchange,setup)
    assert not setup[2]['pending_entries'] and not setup[2]['open_positions']
    assert allowed_to_trade(setup[2],setup[0],setup[3])


def test_fill_time_is_exchange_time_after_offline_stop(setup):
    exchange=StopExchange();enter(new_signal(),exchange,setup)
    exchange.trigger()
    stop=next(o for o in exchange.orders.values() if o['type']=='STOP_LOSS')
    stop['updateTime']=1609459200000
    sync(exchange,setup)
    assert setup[2]['realized'][0]['closed_at']=='2021-01-01T00:00:00+00:00'
