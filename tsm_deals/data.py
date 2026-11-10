"""Shared data types, HTTP helper, and TSM region stats.

Realm auction data comes from Blizzard (hourly, see blizzard.py). Region-wide demand stats
(average *sale* price, sale rate, sold per day) come from TradeSkillMaster's public files:
https://tradeskillmaster.com/public-data. Prices are integer copper (1g = 10,000c).
"""

from __future__ import annotations

import csv
import io
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

TSM_BASE_URL = os.environ.get("TSM_BASE_URL", "https://public-data.tradeskillmaster.com/retail")
USER_AGENT = "tsm-deal-finder/2.0 (personal hourly scanner)"
COPPER_PER_GOLD = 10_000

ID_COLUMNS = {"item": "itemId", "pet": "petSpeciesId"}
FILE_FOR_KIND = {"item": "items.csv", "pet": "pets.csv"}


class FetchError(RuntimeError):
    pass


class RateLimited(FetchError):
    """Server asked us to back off for a long time; stop calling it this run."""


@dataclass
class AuctionHouse:
    """One connected-realm auction house (member realms share listings)."""

    id: int
    realms: list[dict]  # [{"slug": ..., "name": ...}]
    population: str = ""  # Blizzard population type, e.g. FULL / HIGH / MEDIUM / LOW

    @property
    def slug(self) -> str:
        return str(self.id)

    @property
    def slugs(self) -> set[str]:
        return {r["slug"] for r in self.realms}

    def display(self, preferred: str | None = None) -> str:
        names = [r["name"] for r in self.realms]
        for r in self.realms:
            if r["slug"] == preferred:
                names.remove(r["name"])
                names.insert(0, r["name"])
        if len(names) <= 3:
            return " / ".join(names)
        return f"{' / '.join(names[:2])} (+{len(names) - 2} connected)"


@dataclass
class RealmRow:
    min_buyout: int  # cheapest unit price listed
    next_buyout: int = 0  # second-cheapest unit price (0 = only one listing)
    count: int = 0  # number of listings


@dataclass
class RegionRow:
    name: str
    market_value: int
    avg_sale_price: int
    sale_rate: float
    sold_per_day: float


@dataclass
class Snapshot:
    rows: dict[tuple[str, int], RealmRow] = field(default_factory=dict)
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- HTTP

POLITE_DELAY = 0.2


def http_get(url: str, headers: dict | None = None, attempts: int = 4, blocked: threading.Event | None = None,
             max_retry_wait: float = 30.0):
    """GET returning (status, body_bytes, response_headers). 304 is returned, not raised."""
    delay = 3.0
    short = url.split("?")[0][-80:]
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(1, attempts + 1):
        if blocked is not None and blocked.is_set():
            raise RateLimited(f"skipped {short}: rate limited earlier this run")
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                time.sleep(POLITE_DELAY)
                return resp.status, body, resp.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, b"", exc.headers
            if exc.code in (400, 401, 403, 404):
                raise FetchError(f"HTTP {exc.code} for {short}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            print(f"  HTTP {exc.code} on {short} (Retry-After {retry_after}), attempt {attempt}", file=sys.stderr, flush=True)
            if exc.code == 429 and wait > max_retry_wait:
                if blocked is not None:
                    blocked.set()
                raise RateLimited(f"rate limited for {wait:.0f}s at {short}") from exc
            if attempt == attempts:
                raise FetchError(f"HTTP {exc.code} for {short}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise FetchError(f"{exc} for {short}") from exc
            wait = delay
        time.sleep(min(wait, max_retry_wait))
        delay = min(delay * 2, max_retry_wait)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- TSM region stats

def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ts(v: str) -> datetime | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return None


def parse_region_csv(text: str, kind: str, out: dict) -> datetime | None:
    id_col = ID_COLUMNS[kind]
    reader = csv.DictReader(io.StringIO(text))
    needed = {id_col, "avgSalePrice", "saleRate", "soldPerDay"}
    if not reader.fieldnames or not needed.issubset(reader.fieldnames):
        raise FetchError(f"Unexpected region {kind} CSV header: {reader.fieldnames}")
    updated = None
    for row in reader:
        key = (kind, _int(row[id_col]))
        if key[1] <= 0:
            continue
        updated = updated or _ts(row.get("updatedAt", ""))
        out[key] = RegionRow(
            name=row.get("name") or "",
            market_value=_int(row.get("marketValue")),
            avg_sale_price=_int(row.get("avgSalePrice")),
            sale_rate=_float(row.get("saleRate")),
            sold_per_day=_float(row.get("soldPerDay")),
        )
    return updated


def parse_commodity_ids(text: str) -> set[int]:
    return {_int(r.get("itemId")) for r in csv.DictReader(io.StringIO(text))} - {0}


class TSMRegion:
    """Region-wide sale stats from TSM (refreshed upstream about daily)."""

    def __init__(self, region: str):
        self.base = f"{TSM_BASE_URL}/{region}"
        self.blocked = threading.Event()

    def _get(self, path: str) -> str:
        return http_get(f"{self.base}/{path}", blocked=self.blocked)[1].decode("utf-8-sig")

    def region(self, kinds: list[str]):
        out: dict = {}
        updated = None
        for kind in kinds:
            updated = parse_region_csv(self._get(f"region/{FILE_FOR_KIND[kind]}"), kind, out) or updated
        return out, updated

    def commodity_ids(self) -> set[int]:
        return parse_commodity_ids(self._get("commodities.csv"))
