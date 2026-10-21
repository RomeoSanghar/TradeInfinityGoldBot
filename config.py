"""
Configuration loader. Reads from environment / Replit Secrets.
Never logs or prints secret values.
"""
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: Optional[str]
    telegram_group_id: Optional[str]
    admin_telegram_id: Optional[int]
    twelve_data_api_key: Optional[str]
    poll_interval_seconds: int
    quote_timezone: str
    motivational_quote_hour: int
    motivational_quote_minute: int
    db_path: str
    symbol: str
    stale_quote_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is required. "
                "Set it in Replit Secrets (or .env for local)."
            )

        admin_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        admin_id = int(admin_raw) if admin_raw.isdigit() else None

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
            telegram_group_id=os.getenv("TELEGRAM_GROUP_ID", "").strip() or None,
            admin_telegram_id=admin_id,
            twelve_data_api_key=os.getenv("TWELVE_DATA_API_KEY", "").strip() or None,
            poll_interval_seconds=max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "60"))),
            quote_timezone=os.getenv("QUOTE_TIMEZONE", "Asia/Karachi"),
            motivational_quote_hour=int(os.getenv("MOTIVATIONAL_QUOTE_HOUR", "9")),
            motivational_quote_minute=int(os.getenv("MOTIVATIONAL_QUOTE_MINUTE", "0")),
            db_path=os.getenv("DB_PATH", "trades.db"),
            symbol=os.getenv("SYMBOL", "XAU/USD"),
            stale_quote_seconds=max(30, int(os.getenv("STALE_QUOTE_SECONDS", "120"))),
        )

    def missing_secrets(self) -> list[str]:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_group_id:
            missing.append("TELEGRAM_GROUP_ID (required to publish signals to the group)")
        if not self.admin_telegram_id:
            missing.append("ADMIN_TELEGRAM_ID (required for /signal and admin commands)")
        if not self.twelve_data_api_key:
            missing.append("TWELVE_DATA_API_KEY (required for live price monitoring)")
        return missing

    def status_report(self) -> str:
        lines = [
            "TradeInfinityGoldBot configuration status:",
            f"  TELEGRAM_BOT_TOKEN: {'set' if self.telegram_bot_token else 'MISSING'}",
            f"  TELEGRAM_CHAT_ID (private test): {'set' if self.telegram_chat_id else 'not set'}",
            f"  TELEGRAM_GROUP_ID (signals): {'set' if self.telegram_group_id else 'MISSING'}",
            f"  ADMIN_TELEGRAM_ID: {'set' if self.admin_telegram_id else 'MISSING'}",
            f"  TWELVE_DATA_API_KEY: {'set' if self.twelve_data_api_key else 'MISSING'}",
            f"  Poll interval: {self.poll_interval_seconds}s",
            f"  Timezone: {self.quote_timezone}",
            f"  Symbol: {self.symbol}",
            f"  Stale quote threshold: {self.stale_quote_seconds}s",
            f"  DB path: {self.db_path}",
        ]
        return "\n".join(lines)
