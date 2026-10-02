import json
from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import patch

import pytest
import strategy
from strategy import Candle, StrategyConfig, entry_day_allowed, stop_distance_allowed, supertrend_state
from runtime_settings import SettingsStore

HOUR = 3600000


def close_ms(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000) - 1


@pytest.mark.parametrize('date,allowed', [('2026-09-26T23:00', True), ('2026-09-27T00:00', False),
    ('2026-09-28T00:00', False), ('2026-09-28T23:00', False), ('2026-09-29T00:00', True)])
def test_weekday_uses_exact_utc_close_boundary(date, allowed):
    assert entry_day_allowed(close_ms(date)) is allowed


@pytest.mark.parametrize('entry,stop,allowed', [(100, 97.44, False), (100, 97.440001, True),
    (100, 97.439999, False), (100, 100, False), (100, 101, False), (0, 0, False),
    (0.01, 0.009744, False), (0.01, 0.009744001, True)])
def test_strict_stop_boundary(entry, stop, allowed):
    assert stop_distance_allowed(entry, stop) is allowed


def test_zero_surges_save_reload_and_preserve_other_settings(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(dict(settings_revision=3, minimum_reward_risk=2,
        maximum_reward_risk=4, app_minimum_surge=0.15, top_n=379, telegram_chat_id='saved')))
    store = SettingsStore(str(path))
    assert store.get().minimum_reward_risk == 1
    assert store.get().app_minimum_surge == 0.15
    store.update(dict(app_minimum_surge=0, indicator_minimum_surge=0, minimum_reward_risk=1.2))
    saved = SettingsStore(str(path)).get()
    assert saved.app_minimum_surge == saved.indicator_minimum_surge == 0
    assert saved.minimum_reward_risk == 1.2
    assert saved.top_n == 379 and saved.telegram_chat_id == 'saved'
    score, _ = strategy._confidence(0, .8, 2, StrategyConfig(indicator_minimum_surge=0))
    assert 50 <= score <= 80  # disabled volume adds zero, never a fake perfect score
    for value in [-.1, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            store.update({'app_minimum_surge': value})


def test_supertrend_hand_calculated_flip_carry_and_equal_band():
    # period=2, multiplier=1: first ATR=2, up=8, down=12.
    vals = [(10, 11, 9, 10), (10, 11, 9, 10), (10, 10, 7, 8),
            (8, 8, 6, 7), (7, 13, 7, 12), (12, 13, 11, 12)]
    bars = [Candle(i*HOUR,*v,10,(i+1)*HOUR-1) for i,v in enumerate(vals)]
    assert supertrend_state(bars, 2, 1) == [None, 1, 1, -1, 1, 1]
    assert supertrend_state(bars[:2], 2, 1, False) == [None, None]


def evaluate_fixture(app=0, indicator=0, app_surge=None, st=1, end='2026-09-29T01:00', stop=109):
    end_ms = close_ms(end) + 1
    bars = [Candle(end_ms-(241-i)*HOUR,100,101,99,100,0,end_ms-(240-i)*HOUR-1,0) for i in range(241)]
    bars[-1] = replace(bars[-1],open=106,high=111,low=105,close=110)
    cfg = StrategyConfig(app_minimum_surge=app, indicator_minimum_surge=indicator)
    with patch('strategy.latest_prior_nr7_box',return_value=(105,stop)), \
         patch('strategy.app_volume_surge',return_value=app_surge), \
         patch('strategy.is_stablecoin_at_signal',return_value=False), \
         patch('strategy.strong_pivot_state',return_value=[(True,140)]*len(bars)), \
         patch('strategy.supertrend_state',return_value=[st]*len(bars)), \
         patch('strategy.previous_completed_daily',return_value=Candle(0,105,115,105,110,1,0)):
        return strategy.evaluate_latest_detailed('TESTUSDT',bars,cfg,end_ms)


def test_zero_disables_only_each_volume_gate():
    assert evaluate_fixture().signal is not None
    assert evaluate_fixture(app=1.5).rejection_stage == 'App Volume'
    assert evaluate_fixture(indicator=1.5).rejection_stage == 'Indicator Volume'
    assert evaluate_fixture(st=-1).rejection_stage == 'Supertrend'
    assert evaluate_fixture(st=None).rejection_stage == 'Supertrend'
    assert evaluate_fixture(end='2026-09-28T01:00').rejection_stage == 'Entry Weekday'
    assert evaluate_fixture(stop=105).rejection_stage == 'Stop Distance'
    assert evaluate_fixture(app=1.5, app_surge=1.5).rejection_stage == 'App Volume'
    assert evaluate_fixture(app=1.5, app_surge=1.50001).signal is not None
