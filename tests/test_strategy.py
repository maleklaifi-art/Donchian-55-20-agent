from decimal import Decimal
from demo_trader import floor_step, floor_tick, ceil_tick, deterministic_id


def test_rounding():
    assert floor_step(1.23456, Decimal("0.001")) == Decimal("1.234")
    assert floor_tick(10.129, Decimal("0.01")) == Decimal("10.12")
    assert ceil_tick(10.121, Decimal("0.01")) == Decimal("10.13")


def test_ids_are_deterministic():
    assert deterministic_id("nr7e", "ABC") == deterministic_id("nr7e", "ABC")
    assert deterministic_id("nr7e", "ABC") != deterministic_id("nr7e", "ABD")
