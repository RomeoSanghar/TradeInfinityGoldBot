"""
Unit tests for signal validation, TP/SL hit logic, ambiguous jumps,
stale quotes, and basic persistence.
"""
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from services.trade_manager import (
    parse_signal,
    evaluate_levels,
    HitResult,
)
from services.market_data import Quote
from db.models import TradeRepository, TradeStatus


def test_parse_buy_valid():
    r = parse_signal(["XAUUSD", "BUY", "2650.5", "2640", "2675"])
    assert r.ok
    assert r.direction == "BUY"
    assert r.entry == 2650.5
    assert r.sl == 2640.0
    assert r.tp == 2675.0


def test_parse_sell_valid():
    r = parse_signal(["XAU/USD", "SELL", "2650", "2660", "2620"])
    assert r.ok
    assert r.direction == "SELL"
    assert r.sl > r.entry > r.tp


def test_parse_buy_invalid_sl_above_entry():
    r = parse_signal(["XAUUSD", "BUY", "2650", "2660", "2675"])
    assert not r.ok
    assert "SL must be below" in r.error


def test_parse_sell_invalid_tp_above_entry():
    r = parse_signal(["XAUUSD", "SELL", "2650", "2660", "2680"])
    assert not r.ok
    assert "TP below ENTRY" in r.error


def test_parse_wrong_arity():
    r = parse_signal(["XAUUSD", "BUY", "2650"])
    assert not r.ok
    assert "Invalid format" in r.error


def test_parse_non_numeric():
    r = parse_signal(["XAUUSD", "BUY", "abc", "2640", "2675"])
    assert not r.ok
    assert "numeric" in r.error


def _quote(price: float, age_s: float = 5) -> Quote:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return Quote(symbol="XAU/USD", price=price, timestamp=ts)


def test_buy_tp_hit():
    r = evaluate_levels("BUY", 2650, 2640, 2675, _quote(2675.1), 120)
    assert r.hit and r.kind == "TP"


def test_buy_sl_hit():
    r = evaluate_levels("BUY", 2650, 2640, 2675, _quote(2639.9), 120)
    assert r.hit and r.kind == "SL"


def test_sell_tp_hit():
    r = evaluate_levels("SELL", 2650, 2660, 2620, _quote(2619.5), 120)
    assert r.hit and r.kind == "TP"


def test_sell_sl_hit():
    r = evaluate_levels("SELL", 2650, 2660, 2620, _quote(2660.5), 120)
    assert r.hit and r.kind == "SL"


def test_no_hit():
    r = evaluate_levels("BUY", 2650, 2640, 2675, _quote(2655), 120)
    assert not r.hit


def test_stale_quote_no_hit():
    r = evaluate_levels("BUY", 2650, 2640, 2675, _quote(2680, age_s=300), 120)
    assert not r.hit
    assert r.reason == "stale_quote"


def test_missing_quote_no_hit():
    r = evaluate_levels("BUY", 2650, 2640, 2675, None, 120)
    assert not r.hit
    assert r.reason == "no_quote"


def test_ambiguous_jump_buy():
    # Price far beyond both levels
    r = evaluate_levels("BUY", 2650, 2640, 2675, _quote(2700), 120)
    # 2700 is only TP, not SL
    assert r.hit and r.kind == "TP"

    # Force both: for BUY, SL below and TP above – a single price can't be both
    # unless levels are inverted, which validation prevents.
    # Simulate by giving a quote that would satisfy both if levels were wrong:
    # We test the code path by temporarily using inverted logic via direct call.
    # Realistic ambiguous: price gaps across the whole range in one observation
    # is only possible if previous price was on one side – the function checks
    # current price against both levels simultaneously.
    # For BUY, price >= TP and price <= SL is impossible when SL < TP.
    # So AMBIGUOUS is mainly for SELL/BUY when levels cross or data error.
    # We still cover the branch:
    r2 = evaluate_levels("BUY", 2650, 2680, 2640, _quote(2660), 120)  # inverted levels
    # With inverted, 2660 >= 2640 (TP) and 2660 <= 2680 (SL) → both true
    assert r2.hit and r2.kind == "AMBIGUOUS"


def test_ambiguous_sell():
    # SELL with inverted levels so both can fire
    r = evaluate_levels("SELL", 2650, 2620, 2680, _quote(2650), 120)
    assert r.hit and r.kind == "AMBIGUOUS"


def test_persistence_open_and_close():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        repo = TradeRepository(path)
        t = repo.create_trade("XAU/USD", "BUY", 2650, 2640, 2675)
        assert t.status == TradeStatus.OPEN.value
        open_list = repo.list_open_trades()
        assert len(open_list) == 1
        assert open_list[0].trade_id == t.trade_id

        closed = repo.close_trade(
            t.trade_id, TradeStatus.CLOSED_TP.value, close_price=2676, close_reason="tp"
        )
        assert closed.status == TradeStatus.CLOSED_TP.value
        assert len(repo.list_open_trades()) == 0
    finally:
        os.unlink(path)


def test_breakeven_updates_sl_and_audit():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        repo = TradeRepository(path)
        t = repo.create_trade("XAU/USD", "BUY", 2650, 2640, 2675)
        updated = repo.set_breakeven(t.trade_id, new_sl=2650)
        assert updated.sl == 2650
        import json
        log = json.loads(updated.audit_log)
        assert any(e.get("event") == "breakeven" for e in log)
    finally:
        os.unlink(path)
