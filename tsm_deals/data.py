"""Download and parse TradeSkillMaster public CSV pricing files.

Source: https://tradeskillmaster.com/public-data (no API key; files update every few hours).
All prices in the files are integer copper (1 gold = 10,000 copper).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = os.environ.get("TSM_BASE_URL", "https://public-data.tradeskillmaster.com/retail")
USER_AGENT = "tsm-deal-finder/1.0 (personal hourly scanner)"
COPPER_PER_GOLD = 10_000

# kind -> CSV id column. Items and battle pets use separate id spaces.
ID_COLUMNS = {"item": "itemId", "pet": "petSpeciesId"}
FILE_FOR_KIND = {"item": "items.csv", "pet": "pets.csv"}


class FetchError(RuntimeError):
    pass


@dataclass
class AuctionHouse:
    """One connected-realm auction house (all member realms share listings)."""

    realms: list[dict]  # [{"slug": ..., "name": ...}]

    @property
    def slug(self) -> str:
        return self.realms[0]["slug"]

    @property
    def slugs(self) -> set[str]:
        return {r["slug"] for r in self.realms}

    def display(self, preferred: str | None = None) -> str:
        names = [r["name"] for r in self.realms]
        if preferred:
            for r in self.realms:
                if r["slug"] == preferred:
                    names.remove(r["name"])
                    names.insert(0, r["name"])
        if len(names) == 1:
            return names[0]
        return f"{names[0]} (connected: {', '.join(names[1:])})"


@dataclass
class RealmRow:
    min_buyout: int
    market_value: int


@dataclass
class RegionRow:
    name: str
    market_value: int
    avg_sale_price: int
    sale_rate: float
    sold_per_day: float


@dataclass
class Snapshot:
    """Parsed realm file: (kind, id) -> row, plus names and source timestamp."""

    rows: dict[tuple[str, int], RealmRow] = field(default_factory=dict)
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    updated_at: datetime | None = None


def load_auction_houses(path: Path) -> list[AuctionHouse]:
    return [AuctionHouse(g["realms"]) for g in json.loads(path.read_text())]


class RateLimited(FetchError):
    """Server asked us to back off for a long time; stop downloading this run."""


# Set once the server sends a long Retry-After; all later downloads in this run are skipped.
_blocked = threading.Event()
MAX_RETRY_WAIT = 30.0
POLITE_DELAY = 0.5  # seconds between requests per worker


def http_get(url: str, attempts: int = 4) -> str:
    """GET with short backoff. Gives up for the whole run on a long Retry-After."""
    delay = 3.0
    short = url.rsplit("/retail/", 1)[-1]
    for attempt in range(1, attempts + 1):
        if _blocked.is_set():
            raise RateLimited(f"skipped {short}: rate limited earlier this run")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read().decode("utf-8-sig")
            time.sleep(POLITE_DELAY)
            return body
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise FetchError(f"HTTP {exc.code} for {short}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            print(f"  HTTP {exc.code} on {short} (Retry-After {retry_after}), attempt {attempt}", file=sys.stderr, flush=True)
            if exc.code == 429 and wait > MAX_RETRY_WAIT:
                _blocked.set()
                raise RateLimited(f"rate limited for {wait:.0f}s at {short}") from exc
            if attempt == attempts:
                raise FetchError(f"HTTP {exc.code} for {short}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise FetchError(f"{exc} for {short}") from exc
            wait = delay
        time.sleep(wait)
        delay = min(delay * 2, MAX_RETRY_WAIT)
    raise AssertionError("unreachable")


def rate_limited() -> bool:
    return _blocked.is_set()


def _int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _float(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ts(v: str) -> datetime | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return None


def parse_realm_csv(text: str, kind: str, snap: Snapshot, keep=None) -> Snapshot:
    """Parse a realm items/pets CSV into `snap`.

    `keep(key, min_buyout)` optionally filters rows to save memory on source realms.
    """
    id_col = ID_COLUMNS[kind]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or id_col not in reader.fieldnames or "minBuyout" not in reader.fieldnames:
        raise FetchError(f"Unexpected {kind} CSV header: {reader.fieldnames}")
    for row in reader:
        key = (kind, _int(row[id_col]))
        if key[1] <= 0:
            continue
        mb = _int(row.get("minBuyout"))
        if snap.updated_at is None:
            snap.updated_at = _ts(row.get("updatedAt", ""))
        if keep is not None and not keep(key, mb):
            continue
        snap.rows[key] = RealmRow(min_buyout=mb, market_value=_int(row.get("marketValue")))
        if row.get("name"):
            snap.names[key] = row["name"]
    return snap


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
    reader = csv.DictReader(io.StringIO(text))
    return {_int(r.get("itemId")) for r in reader} - {0}


class TSMClient:
    def __init__(self, region: str, concurrency: int = 4):
        self.base = f"{BASE_URL}/{region}"
        self.concurrency = max(1, concurrency)

    def region(self, kinds: list[str]) -> tuple[dict, datetime | None]:
        out: dict = {}
        updated = None
        for kind in kinds:
            updated = parse_region_csv(http_get(f"{self.base}/region/{FILE_FOR_KIND[kind]}"), kind, out) or updated
        return out, updated

    def commodity_ids(self) -> set[int]:
        return parse_commodity_ids(http_get(f"{self.base}/commodities.csv"))

    def realm(self, ah: AuctionHouse, kinds: list[str], keep=None) -> Snapshot:
        snap = Snapshot()
        for kind in kinds:
            text = self._get_any(ah, kind)
            parse_realm_csv(text, kind, snap, keep)
        return snap

    def _get_any(self, ah: AuctionHouse, kind: str) -> str:
        """Connected realms share one AH; fall back to another member slug on 404."""
        last: Exception | None = None
        for r in ah.realms:
            try:
                return http_get(f"{self.base}/realm/{r['slug']}/{FILE_FOR_KIND[kind]}")
            except FetchError as exc:
                last = exc
                if "HTTP 404" not in str(exc):
                    break
        raise FetchError(str(last))

    def realms(self, ahs: list[AuctionHouse], kinds: list[str], keep=None):
        """Fetch many AHs with bounded concurrency. Returns (results, errors)."""
        results: dict[str, Snapshot] = {}
        errors: dict[str, str] = {}

        def work(ah):
            t = time.monotonic()
            try:
                snap = self.realm(ah, kinds, keep)
                print(f"  {ah.slug}: {len(snap.rows)} rows in {time.monotonic() - t:.1f}s", file=sys.stderr, flush=True)
                return ah.slug, snap, None
            except FetchError as exc:
                print(f"  {ah.slug}: FAILED {exc}", file=sys.stderr, flush=True)
                return ah.slug, None, str(exc)

        with ThreadPoolExecutor(self.concurrency) as pool:
            for slug, snap, err in pool.map(work, ahs):
                if err:
                    errors[slug] = err
                else:
                    results[slug] = snap
        return results, errors
