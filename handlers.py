"""
Telegram command handlers.
Only ADMIN_TELEGRAM_ID may create / update / close trades.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import Config
from db.models import TradeRepository, TradeStatus
from services.market_data import MarketDataService
from services.trade_manager import (
    parse_signal,
    evaluate_levels,
    format_signal_message,
    format_closed_message,
    pick_daily_quote,
)

logger = logging.getLogger(__name__)


def _is_admin(update: Update, config: Config) -> bool:
    user = update.effective_user
    if not user or not config.admin_telegram_id:
        return False
    return user.id == config.admin_telegram_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "TradeInfinity Gold Bot online.\n"
        "Use /help for commands.\n"
        "Admin-only for signal management."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*TradeInfinity · Commands*\n\n"
        "Public / test:\n"
        "  /test – connectivity check\n"
        "  /help – this message\n\n"
        "Admin only:\n"
        "  /signal XAUUSD BUY ENTRY SL TP\n"
        "  /signal XAUUSD SELL ENTRY SL TP\n"
        "  /status – bot + market status\n"
        "  /trades – open & recent trades\n"
        "  /close TRADE\\_ID – manual close\n"
        "  /breakeven TRADE\\_ID – move SL to entry (status only)\n"
        "  /market – market-data diagnostic\n\n"
        "Example:\n"
        "`/signal XAUUSD BUY 2650.50 2640.00 2675.00`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preserved working /test command."""
    config: Config = context.bot_data["config"]
    user = update.effective_user
    chat = update.effective_chat
    await update.message.reply_text(
        f"✅ TradeInfinity test OK\n"
        f"User: {user.id if user else '—'}\n"
        f"Chat: {chat.id if chat else '—'}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    repo: TradeRepository = context.bot_data["repo"]
    market: MarketDataService = context.bot_data["market"]

    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized. Admin only.")
        return

    if not config.telegram_group_id:
        await update.message.reply_text(
            "TELEGRAM_GROUP_ID is not configured. "
            "Add it in Replit Secrets so signals can be published to the group."
        )
        return

    result = parse_signal(context.args or [])
    if not result.ok:
        await update.message.reply_text(result.error)
        return

    # Create trade first (without message ids)
    trade = repo.create_trade(
        symbol=result.symbol,
        direction=result.direction,
        entry=result.entry,
        sl=result.sl,
        tp=result.tp,
    )

    current_price = None
    price_ts = None
    if market.is_configured:
        try:
            q = market.get_quote()
            current_price = q.price
            price_ts = q.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            repo.update_price(trade.trade_id, q.price, q.timestamp.isoformat())
        except Exception as e:
            logger.warning("Could not fetch price at signal time: %s", e)

    text = format_signal_message(trade, current_price, price_ts)

    try:
        msg = await context.bot.send_message(
            chat_id=config.telegram_group_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        repo.update_trade_message_ids(
            trade.trade_id, str(msg.chat_id), msg.message_id
        )
        await update.message.reply_text(
            f"Signal published · `{trade.trade_id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.exception("Failed to publish signal")
        await update.message.reply_text(
            f"Trade created ({trade.trade_id}) but Telegram publish failed: {e}"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized.")
        return

    repo: TradeRepository = context.bot_data["repo"]
    market: MarketDataService = context.bot_data["market"]
    open_trades = repo.list_open_trades()

    lines = [
        "*TradeInfinity Status*",
        config.status_report().replace("_", "\\_"),
        "",
        f"Open trades: {len(open_trades)}",
    ]
    if market.is_configured:
        diag = market.diagnostic()
        lines.append(f"Market: {diag['message']}")
    else:
        lines.append("Market: TWELVE_DATA_API_KEY not set — monitoring disabled")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized.")
        return

    repo: TradeRepository = context.bot_data["repo"]
    open_trades = repo.list_open_trades()
    recent = repo.list_recent_trades(10)

    if not open_trades and not recent:
        await update.message.reply_text("No trades yet.")
        return

    lines = ["*Open trades*"]
    if not open_trades:
        lines.append("  (none)")
    for t in open_trades:
        price_info = f" @ {t.last_price:.2f}" if t.last_price else ""
        lines.append(
            f"  `{t.trade_id}` {t.direction} E={t.entry:.2f} SL={t.sl:.2f} TP={t.tp:.2f}{price_info}"
        )

    lines.append("")
    lines.append("*Recent*")
    for t in recent:
        lines.append(f"  `{t.trade_id}` {t.status} {t.direction}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /close TRADE_ID")
        return

    trade_id = context.args[0].upper()
    repo: TradeRepository = context.bot_data["repo"]
    trade = repo.get_trade(trade_id)
    if not trade:
        await update.message.reply_text(f"Trade `{trade_id}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if trade.status != TradeStatus.OPEN.value:
        await update.message.reply_text(f"Trade already {trade.status}")
        return

    closed = repo.close_trade(
        trade_id,
        TradeStatus.CLOSED_MANUAL.value,
        close_price=trade.last_price,
        close_reason="manual_close",
        by=f"admin:{update.effective_user.id}",
    )
    await _edit_and_notify(context, closed)
    await update.message.reply_text(f"Closed `{trade_id}` manually.", parse_mode=ParseMode.MARKDOWN)


async def cmd_breakeven(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User-requested SL update to entry. Status / audit only — never a broker order.
    """
    config: Config = context.bot_data["config"]
    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /breakeven TRADE_ID")
        return

    trade_id = context.args[0].upper()
    repo: TradeRepository = context.bot_data["repo"]
    trade = repo.get_trade(trade_id)
    if not trade:
        await update.message.reply_text(f"Trade `{trade_id}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if trade.status != TradeStatus.OPEN.value:
        await update.message.reply_text(f"Trade is {trade.status}, cannot set breakeven.")
        return

    updated = repo.set_breakeven(
        trade_id, new_sl=trade.entry, by=f"admin:{update.effective_user.id}"
    )
    if updated and updated.telegram_chat_id and updated.telegram_message_id:
        text = format_signal_message(
            updated,
            current_price=updated.last_price,
            price_ts=updated.last_price_ts,
            extra_note="SL moved to entry (breakeven request — status only, not a broker order).",
        )
        try:
            await context.bot.edit_message_text(
                chat_id=updated.telegram_chat_id,
                message_id=updated.telegram_message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning("editMessageText failed: %s", e)

    await update.message.reply_text(
        f"Breakeven set for `{trade_id}` · new SL = {trade.entry:.2f}\n"
        "(Status update only — no broker order was sent.)",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_admin(update, config):
        await update.message.reply_text("Unauthorized.")
        return

    market: MarketDataService = context.bot_data["market"]
    diag = market.diagnostic()
    await update.message.reply_text(
        f"*Market diagnostic*\n{diag['message']}\n"
        f"Configured: {diag['configured']}\n"
        f"Calls this session: {diag['call_count']}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _edit_and_notify(context: ContextTypes.DEFAULT_TYPE, trade) -> None:
    if not trade:
        return
    text = format_signal_message(
        trade,
        current_price=trade.close_price or trade.last_price,
        extra_note=f"Closed: {trade.close_reason or trade.status}",
    )
    if trade.telegram_chat_id and trade.telegram_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=trade.telegram_chat_id,
                message_id=trade.telegram_message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning("Failed to edit original signal: %s", e)

    # Concise notification
    try:
        config: Config = context.bot_data["config"]
        chat = trade.telegram_chat_id or config.telegram_group_id
        if chat:
            await context.bot.send_message(
                chat_id=chat,
                text=format_closed_message(trade),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.warning("Failed to send close notification: %s", e)


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic job: poll price, evaluate open trades, close & edit when levels hit.
    """
    config: Config = context.bot_data["config"]
    repo: TradeRepository = context.bot_data["repo"]
    market: MarketDataService = context.bot_data["market"]

    if not market.is_configured:
        return

    open_trades = repo.list_open_trades()
    if not open_trades:
        return

    quote = market.safe_latest()
    if quote is None:
        logger.debug("No usable quote this cycle")
        return

    for trade in open_trades:
        result = evaluate_levels(
            trade.direction,
            trade.entry,
            trade.sl,
            trade.tp,
            quote,
            config.stale_quote_seconds,
        )
        repo.update_price(trade.trade_id, quote.price, quote.timestamp.isoformat())

        if not result.hit:
            # Still open — optionally refresh the message with latest price
            if trade.telegram_chat_id and trade.telegram_message_id:
                try:
                    text = format_signal_message(
                        trade,
                        current_price=quote.price,
                        price_ts=quote.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    )
                    await context.bot.edit_message_text(
                        chat_id=trade.telegram_chat_id,
                        message_id=trade.telegram_message_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    # Ignore "message is not modified" and similar
                    pass
            continue

        # Hit — close exactly once
        if result.kind == "TP":
            status = TradeStatus.CLOSED_TP.value
            reason = "tp_hit"
        elif result.kind == "SL":
            status = TradeStatus.CLOSED_SL.value
            reason = "sl_hit"
        else:
            status = TradeStatus.CLOSED_AMBIGUOUS.value
            reason = "ambiguous_price_jump"

        closed = repo.close_trade(
            trade.trade_id,
            status=status,
            close_price=result.price,
            close_reason=reason,
            by="monitor",
        )
        await _edit_and_notify(context, closed)
        logger.info("Trade %s closed: %s @ %s", trade.trade_id, reason, result.price)


async def daily_quote_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Post one motivational quote per calendar day in the configured timezone.
    Uses DB flag to avoid duplicates on restart.
    """
    config: Config = context.bot_data["config"]
    repo: TradeRepository = context.bot_data["repo"]

    if not config.telegram_group_id:
        return

    import pytz
    tz = pytz.timezone(config.quote_timezone)
    now_local = datetime.now(tz)
    day_key = now_local.strftime("%Y-%m-%d")

    last = repo.get_scheduler_flag("last_quote_day")
    if last == day_key:
        return

    quote = pick_daily_quote(day_key)
    try:
        await context.bot.send_message(
            chat_id=config.telegram_group_id,
            text=f"📖 *TradeInfinity Daily*\n\n_{quote}_",
            parse_mode=ParseMode.MARKDOWN,
        )
        repo.set_scheduler_flag("last_quote_day", day_key)
    except Exception as e:
        logger.warning("Failed to post daily quote: %s", e)
