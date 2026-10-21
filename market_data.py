"""
Twelve Data market-data service for XAU/USD.
Uses the official /quote and /price endpoints.
Respects rate limits; never claims monitoring works without a successful live call.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"


@dataclass
class Quote:
    symbol: str
    price: float
    timestamp: datetime  # UTC
    bid: Optional[float] = None
    ask: Optional[float] = None
    raw: Optional[dict] = None

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    def is_stale(self, max_age: int) -> bool:
        return self.age_seconds > max_age


class MarketDataService:
    """
    Lightweight client for Twelve Data XAU/USD quotes.
    Documented convention: price is the spot price in USD per troy ounce.
    User pip convention (for display only): ~30–40 XAUUSD “pips” ≈ $3–$4 move.
    """

    def __init__(
        self,
        api_key: Optional[str],
        symbol: str = "XAU/USD",
        stale_seconds: int = 120,
        timeout: float = 10.0,
    ):
        self.api_key = api_key
        self.symbol = symbol
        self.stale_seconds = stale_seconds
        self.timeout = timeout
        self._last_quote: Optional[Quote] = None
        self._last_error: Optional[str] = None
        self._last_success_ts: Optional[float] = None
        self._call_count = 0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.api_key:
            raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
        params = dict(params or {})
        params["apikey"] = self.api_key
        url = f"{BASE_URL}/{endpoint}"
        self._call_count += 1
        resp = requests.get(url, params=params, timeout=self.timeout)
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {data}")
        if data.get("status") == "error" or "code" in data and data.get("code", 0) >= 400:
            msg = data.get("message") or data.get("status") or str(data)
            raise RuntimeError(f"Twelve Data error: {msg}")
        return data

    def get_quote(self) -> Quote:
        """
        Fetch latest quote via /quote endpoint.
        Returns a Quote object with price and UTC timestamp.
        """
        data = self._request("quote", {"symbol": self.symbol})
        # Typical response fields: symbol, name, exchange, currency, datetime,
        # timestamp, open, high, low, close, volume, previous_close, change, percent_change
        price_raw = data.get("close") or data.get("price")
        if price_raw is None:
            raise RuntimeError(f"No price in quote response for {self.symbol}")
        price = float(price_raw)

        ts = None
        if "timestamp" in data and data["timestamp"]:
            try:
                ts = datetime.fromtimestamp(int(data["timestamp"]), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                pass
        if ts is None and data.get("datetime"):
            try:
                # Twelve Data often returns "2024-... 12:34:56"
                raw = data["datetime"].replace(" ", "T")
                if not raw.endswith("Z") and "+" not in raw:
                    raw += "Z"
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        if ts is None:
            ts = datetime.now(timezone.utc)

        quote = Quote(
            symbol=data.get("symbol", self.symbol),
            price=price,
            timestamp=ts,
            bid=float(data["bid"]) if data.get("bid") not in (None, "") else None,
            ask=float(data["ask"]) if data.get("ask") not in (None, "") else None,
            raw=data,
        )
        self._last_quote = quote
        self._last_success_ts = time.time()
        self._last_error = None
        return quote

    def get_price(self) -> float:
        """Lightweight /price endpoint."""
        data = self._request("price", {"symbol": self.symbol})
        price = float(data["price"])
        self._last_quote = Quote(
            symbol=self.symbol,
            price=price,
            timestamp=datetime.now(timezone.utc),
        )
        self._last_success_ts = time.time()
        self._last_error = None
        return price

    def diagnostic(self) -> dict:
        """
        Safe diagnostic for admin /market command.
        Never returns the API key.
        """
        result = {
            "configured": self.is_configured,
            "symbol": self.symbol,
            "last_error": self._last_error,
            "call_count": self._call_count,
            "last_success_age_s": None,
            "last_price": None,
            "last_price_ts": None,
            "stale": None,
            "ok": False,
            "message": "",
        }
        if not self.is_configured:
            result["message"] = "TWELVE_DATA_API_KEY is not set"
            return result

        try:
            q = self.get_quote()
            result["ok"] = True
            result["last_price"] = q.price
            result["last_price_ts"] = q.timestamp.isoformat()
            result["last_success_age_s"] = round(q.age_seconds, 1)
            result["stale"] = q.is_stale(self.stale_seconds)
            result["message"] = (
                f"Connected. {self.symbol} = {q.price:.2f} "
                f"(ts {q.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC, "
                f"age {result['last_success_age_s']}s)"
            )
        except Exception as e:
            self._last_error = str(e)
            result["last_error"] = str(e)
            result["message"] = f"API call failed: {e}"
            logger.exception("Market data diagnostic failed")
        return result

    def safe_latest(self) -> Optional[Quote]:
        """Return last good quote if still fresh, else try a new fetch."""
        if self._last_quote and not self._last_quote.is_stale(self.stale_seconds):
            return self._last_quote
        try:
            return self.get_quote()
        except Exception as e:
            self._last_error = str(e)
            logger.warning("Failed to refresh quote: %s", e)
            if self._last_quote and not self._last_quote.is_stale(self.stale_seconds * 2):
                return self._last_quote
            return None
