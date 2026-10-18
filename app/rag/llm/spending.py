"""Claude prices, spend records and the monthly cap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from ..config import PACKAGE_DIR
from ..db import Database
from ..i18n import t

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.0


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass(frozen=True)
class Tier:
    up_to: int | None
    input: float
    cache_write: float
    cache_read: float
    output: float


class PriceTable:
    def __init__(self, data: dict):
        self.models: dict[str, list[Tier]] = {}
        for model, info in (data.get("models") or {}).items():
            self.models[model] = [
                Tier(t.get("up_to_input_tokens"), float(t["input"]), float(t["cache_write_5m"]),
                     float(t["cache_read"]), float(t["output"]))
                for t in info["tiers"]
            ]

    @classmethod
    def load(cls, path: Path) -> "PriceTable":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    def _tiers(self, model: str) -> list[Tier]:
        if model in self.models:
            return self.models[model]
        log.warning("No prices for %s in prices.yaml: using the most expensive model", model)
        return max(self.models.values(), key=lambda tiers: tiers[-1].output)

    def _tier(self, model: str, prompt_tokens: int) -> Tier:
        tiers = self._tiers(model)
        for tier in tiers:
            if tier.up_to is None or prompt_tokens <= tier.up_to:
                return tier
        return tiers[-1]

    def cost(self, model: str, usage: Usage) -> float:
        prompt = usage.input_tokens + usage.cache_write + usage.cache_read
        tier = self._tier(model, prompt)
        return (
            usage.input_tokens * tier.input
            + usage.cache_write * tier.cache_write
            + usage.cache_read * tier.cache_read
            + usage.output_tokens * tier.output
        ) / 1_000_000

    def estimate(self, model: str, input_chars: int, max_tokens: int) -> float:
        """Worst case: no cache and the full allowed output."""
        usage = Usage(input_tokens=int(input_chars / CHARS_PER_TOKEN) + 300, output_tokens=max_tokens)
        return self.cost(model, usage)


@lru_cache
def default_prices() -> PriceTable:
    return PriceTable.load(PACKAGE_DIR / "prices.yaml")


def usage_by_model(message) -> list[tuple[str, Usage]]:
    """Billable usage of a response. With server-side fallback, each attempt comes in usage.iterations."""

    def of(u) -> Usage:
        return Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        )

    iterations = getattr(message.usage, "iterations", None) or []
    if iterations:
        return [(getattr(it, "model", None) or message.model, of(it)) for it in iterations]
    return [(message.model, of(message.usage))]


class SpendTracker:
    """Before each call: if the worst case exceeds what is left of the monthly cap, the call is not made."""

    def __init__(self, db: Database, cap_usd: float, tz: str = "UTC", prices: PriceTable | None = None,
                 language: str = "en"):
        self.db = db
        self.cap = cap_usd
        self.zone = ZoneInfo(tz)
        self.prices = prices or default_prices()
        self.language = language

    def month_start(self, now: datetime | None = None) -> datetime:
        now = (now or datetime.now(self.zone)).astimezone(self.zone)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def month_total(self) -> float:
        since = self.month_start().astimezone(timezone.utc)
        with self.db.conn() as c:
            row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM spend WHERE ts >= %s", (since,)).fetchone()
        return float(row["total"])

    def check(self, model: str, input_chars: int, max_tokens: int, purpose: str) -> None:
        estimated = self.prices.estimate(model, input_chars, max_tokens)
        available = self.cap - self.month_total()
        if estimated > available:
            raise BudgetExhausted(t(self.language, "over_budget", purpose=purpose, estimated=estimated,
                                    available=max(available, 0), cap=self.cap))

    def record(self, message, purpose: str, simulated: bool = False) -> float:
        total = 0.0
        for model, usage in usage_by_model(message):
            cost = 0.0 if simulated else self.prices.cost(model, usage)
            total += cost
            self.record_usage(model, purpose, usage, cost, simulated)
        return total

    def record_usage(self, model: str, purpose: str, usage: Usage, cost: float, simulated: bool) -> None:
        with self.db.conn() as c:
            c.execute(
                """INSERT INTO spend (model, purpose, input_tokens, output_tokens, cache_write, cache_read,
                                      cost_usd, simulated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (model, purpose, usage.input_tokens, usage.output_tokens, usage.cache_write, usage.cache_read,
                 cost, simulated),
            )

    def summary(self) -> dict:
        since = self.month_start().astimezone(timezone.utc)
        with self.db.conn() as c:
            row = c.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS calls,
                          COALESCE(SUM(cache_read), 0) AS cache_read,
                          COALESCE(SUM(input_tokens + cache_write + cache_read), 0) AS prompt_tokens
                   FROM spend WHERE ts >= %s AND NOT simulated""",
                (since,),
            ).fetchone()
        spent = float(row["cost"])
        return {
            "month_usd": round(spent, 4),
            "cap_usd": self.cap,
            "remaining_usd": round(max(self.cap - spent, 0), 4),
            "calls": row["calls"],
            "cache_hit_ratio": round(row["cache_read"] / row["prompt_tokens"], 3) if row["prompt_tokens"] else None,
        }
