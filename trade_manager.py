"""
Trade lifecycle: validation, TP/SL evaluation, ambiguous-jump handling,
status formatting, and Telegram message templates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from db.models import Trade, TradeStatus, Direction
from services.market_data import Quote

logger = logging.getLogger(__name__)


@dataclass
class SignalParseResult:
    ok: bool
    symbol: str = ""
    direction: str = ""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    error: str = ""


def parse_signal(args: list[str]) -> SignalParseResult:
    """
    Parse: XAUUSD BUY ENTRY SL TP   or   XAUUSD SELL ENTRY SL TP
    Accepts XAUUSD or XAU/USD.
    """
    if len(args) != 5:
        return SignalParseResult(
            ok=False,
            error=(
                "Invalid format.\n"
                "Usage:\n"
                "  /signal XAUUSD BUY 2650.50 2640.00 2675.00\n"
                "  /signal XAUUSD SELL 2650.50 2660.00 2625.00"
            ),
        )

    symbol_raw, direction_raw, entry_s, sl_s, tp_s = args
    symbol = symbol_raw.upper().replace("XAUUSD", "XAU/USD").replace("XAU/USD", "XAU/USD")
    if symbol not in ("XAU/USD", "XAUUSD"):
        # normalize
        if "XAU" in symbol and "USD" in symbol:
            symbol = "XAU/USD"
        else:
            return SignalParseResult(ok=False, error="Only XAUUSD / XAU/USD is supported.")

    direction = direction_raw.upper()
    if direction not in ("BUY", "SELL"):
        return SignalParseResult(
            ok=False,
            error="Direction must be BUY or SELL.",
        )

    try:
        entry = float(entry_s)
        sl = float(sl_s)
        tp = float(tp_s)
    except ValueError:
        return SignalParseResult(
            ok=False,
            error="ENTRY, SL and TP must be numeric (e.g. 2650.50).",
        )

    if entry <= 0 or sl <= 0 or tp <= 0:
        return SignalParseResult(ok=False, error="Prices must be positive.")

    # Validation rules
    if direction == "BUY":
        if not (sl < entry < tp):
            return SignalParseResult(
                ok=False,
                error=(
                    "For BUY: SL must be below ENTRY and TP above ENTRY.\n"
                    f"Received: SL={sl} ENTRY={entry} TP={tp}"
                ),
            )
    else:  # SELL
        if not (tp < entry < sl):
            return SignalParseResult(
                ok=False,
                error=(
                    "For SELL: SL must be above ENTRY and TP below ENTRY.\n"
                    f"Received: SL={sl} ENTRY={entry} TP={tp}"
                ),
            )

    return SignalParseResult(
        ok=True,
        symbol="XAU/USD",
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
    )


@dataclass
class HitResult:
    hit: bool
    kind: Optional[str] = None  # "TP" | "SL" | "AMBIGUOUS" | None
    price: Optional[float] = None
    reason: str = ""


def evaluate_levels(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    quote: Optional[Quote],
    stale_seconds: int,
) -> HitResult:
    """
    Determine whether TP or SL has been hit.
    - Missing / stale / invalid price → no hit
    - Price jumps across both levels → AMBIGUOUS
    """
    if quote is None:
        return HitResult(hit=False, reason="no_quote")
    if quote.is_stale(stale_seconds):
        return HitResult(hit=False, reason="stale_quote")
    price = quote.price
    if price is None or price <= 0:
        return HitResult(hit=False, reason="invalid_price")

    if direction == "BUY":
        tp_hit = price >= tp
        sl_hit = price <= sl
    else:  # SELL
        tp_hit = price <= tp
        sl_hit = price >= sl

    if tp_hit and sl_hit:
        return HitResult(
            hit=True,
            kind="AMBIGUOUS",
            price=price,
            reason="price_jumped_across_both_levels",
        )
    if tp_hit:
        return HitResult(hit=True, kind="TP", price=price, reason="tp_reached")
    if sl_hit:
        return HitResult(hit=True, kind="SL", price=price, reason="sl_reached")
    return HitResult(hit=False, price=price, reason="levels_not_reached")


def format_signal_message(
    trade: Trade,
    current_price: Optional[float] = None,
    price_ts: Optional[str] = None,
    extra_note: Optional[str] = None,
) -> str:
    """
    Canonical TradeInfinity signal template.
    """
    direction_emoji = "🟢" if trade.direction == "BUY" else "🔴"
    status_line = f"Status: {trade.status}"
    if trade.status == TradeStatus.OPEN.value and current_price is not None:
        status_line = f"Status: OPEN · Monitoring @ {current_price:.2f}"
        if price_ts:
            status_line += f"\nData time: {price_ts}"

    lines = [
        "⚡ *TradeInfinity* · XAUUSD Signal",
        "",
        f"{direction_emoji} *{trade.direction}*  {trade.symbol}",
        f"Trade ID: `{trade.trade_id}`",
        "",
        f"Entry:  `{trade.entry:.2f}`",
        f"SL:     `{trade.sl:.2f}`",
        f"TP:     `{trade.tp:.2f}`",
        "",
        status_line,
        "",
        "⚠️ Risk management: only risk capital you can afford to lose.",
        "External data may differ from your broker’s execution price, spread and fills.",
    ]
    if extra_note:
        lines.append("")
        lines.append(extra_note)
    return "\n".join(lines)


def format_closed_message(trade: Trade) -> str:
    emoji = {
        TradeStatus.CLOSED_TP.value: "✅ TP Hit",
        TradeStatus.CLOSED_SL.value: "🛑 SL Hit",
        TradeStatus.CLOSED_MANUAL.value: "🔒 Closed manually",
        TradeStatus.CLOSED_AMBIGUOUS.value: "⚠️ Ambiguous close",
        TradeStatus.CLOSED_BREAKEVEN.value: "⚖️ Closed at breakeven",
    }.get(trade.status, "Closed")

    lines = [
        f"{emoji} · `{trade.trade_id}`",
        f"{trade.direction} {trade.symbol}",
        f"Entry `{trade.entry:.2f}` → Close `{trade.close_price or '—'}`",
        f"Reason: {trade.close_reason or trade.status}",
    ]
    return "\n".join(lines)


# Original short motivational quotes (no copyrighted material)
MOTIVATIONAL_QUOTES = [
    "Patience is a position. Discipline is the edge.",
    "Risk first. Reward second. Ego never.",
    "Consistency compounds. One good process beats ten lucky trades.",
    "The market pays for waiting, not for forcing.",
    "Protect capital. Opportunity is abundant; capital is finite.",
    "A plan without risk limits is just a wish.",
    "Stay flat when unclear. Clarity is also a trade.",
    "Process over outcome. Review every session.",
    "The best trade is sometimes the one you don’t take.",
    "Size small enough that you can think clearly.",
    "Emotion is noise. Rules are signal.",
    "Gold moves. Your edge is how you respond.",
]


def pick_daily_quote(day_key: str) -> str:
    """Deterministic pick so restarts don’t repost a different quote the same day."""
    idx = sum(ord(c) for c in day_key) % len(MOTIVATIONAL_QUOTES)
    return MOTIVATIONAL_QUOTES[idx]
