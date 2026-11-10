"""Blizzard Game Data API: connected realms (with population) and hourly realm auctions.

Needs a free API client from https://develop.battle.net/access/clients
(env BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET).

The connected-realm auctions endpoint only returns non-commodity auctions. Commodities live
in a separate region-wide endpoint that this tool never reads.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

from .data import USER_AGENT, AuctionHouse, FetchError, RealmRow, Snapshot, http_get

PET_CAGE_ITEM_ID = 82800


def snapshot_from_auctions(payload: dict, keep=None) -> Snapshot:
    """Reduce a raw auctions payload to cheapest/second-cheapest unit price per item."""
    best: dict[tuple[str, int], list[int]] = {}
    counts: dict[tuple[str, int], int] = {}
    for a in payload.get("auctions", []):
        item = a.get("item") or {}
        species = item.get("pet_species_id")
        if species:
            key = ("pet", int(species))
        elif item.get("id"):
            key = ("item", int(item["id"]))
        else:
            continue
        qty = max(1, int(a.get("quantity") or 1))
        unit = a.get("unit_price") or (int(a["buyout"]) // qty if a.get("buyout") else 0)
        if unit <= 0:
            continue  # bid-only auction
        counts[key] = counts.get(key, 0) + 1
        lows = best.setdefault(key, [])
        lows.append(int(unit))
        if len(lows) > 2:
            lows.sort()
            del lows[2:]
    snap = Snapshot()
    for key, lows in best.items():
        lows.sort()
        if keep is not None and not keep(key, lows[0]):
            continue
        snap.rows[key] = RealmRow(min_buyout=lows[0], next_buyout=lows[1] if len(lows) > 1 else 0, count=counts[key])
    return snap


class Blizzard:
    def __init__(self, region: str, client_id: str, client_secret: str, concurrency: int = 4):
        self.region = region
        self.api = os.environ.get("BLIZZARD_API_URL", f"https://{region}.api.blizzard.com")
        self.oauth = os.environ.get("BLIZZARD_OAUTH_URL", "https://oauth.battle.net/token")
        self.client_id = client_id
        self.client_secret = client_secret
        self.concurrency = max(1, concurrency)
        self.blocked = threading.Event()
        self._token: str | None = None

    # ---- auth --------------------------------------------------------------
    def token(self) -> str:
        if self._token:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.oauth,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._token = json.loads(resp.read())["access_token"]
        except Exception as exc:  # noqa: BLE001 - surface any auth failure clearly
            raise FetchError(f"Blizzard OAuth failed (check BLIZZARD_CLIENT_ID/SECRET): {exc}") from exc
        return self._token

    def _get(self, path: str, namespace: str, extra_headers: dict | None = None, **params):
        q = urllib.parse.urlencode({"namespace": f"{namespace}-{self.region}", "locale": "en_US", **params})
        headers = {"Authorization": f"Bearer {self.token()}", **(extra_headers or {})}
        return http_get(f"{self.api}{path}?{q}", headers=headers, blocked=self.blocked)

    def _json(self, path: str, namespace: str):
        return json.loads(self._get(path, namespace)[1])

    # ---- realms ------------------------------------------------------------
    def connected_realms(self) -> list[AuctionHouse]:
        index = self._json("/data/wow/connected-realm/index", "dynamic")
        ids = sorted({int(m) for h in index.get("connected_realms", []) for m in re.findall(r"connected-realm/(\d+)", h["href"])})

        def one(cid: int) -> AuctionHouse:
            doc = self._json(f"/data/wow/connected-realm/{cid}", "dynamic")
            realms = [{"slug": r["slug"], "name": _name(r.get("name"), r["slug"])} for r in doc.get("realms", [])]
            realms.sort(key=lambda r: r["name"])
            return AuctionHouse(id=cid, realms=realms, population=(doc.get("population") or {}).get("type", ""))

        with ThreadPoolExecutor(self.concurrency) as pool:
            return list(pool.map(one, ids))

    # ---- auctions ----------------------------------------------------------
    def auctions(self, ah: AuctionHouse, since: datetime | None, keep=None):
        """Returns (snapshot or None if unchanged since `since`, last_modified)."""
        headers = {"If-Modified-Since": format_datetime(since, usegmt=True)} if since else None
        status, body, resp_headers = self._get(f"/data/wow/connected-realm/{ah.id}/auctions", "dynamic", headers)
        lm = resp_headers.get("Last-Modified") if resp_headers else None
        modified = parsedate_to_datetime(lm).astimezone(timezone.utc) if lm else datetime.now(timezone.utc)
        if status == 304:
            return None, since
        snap = snapshot_from_auctions(json.loads(body), keep)
        snap.updated_at = modified
        return snap, modified

    def auctions_many(self, jobs: list[tuple[AuctionHouse, datetime | None, object]]):
        """jobs: (ah, since, keep). Returns {ah.slug: (snap|None, last_modified)}, errors."""
        results, errors = {}, {}

        def work(job):
            ah, since, keep = job
            t = time.monotonic()
            try:
                snap, lm = self.auctions(ah, since, keep)
                what = "unchanged" if snap is None else f"{len(snap.rows)} items"
                print(f"  {ah.display()}: {what} ({time.monotonic() - t:.1f}s)", file=sys.stderr, flush=True)
                return ah.slug, (snap, lm), None
            except FetchError as exc:
                print(f"  {ah.display()}: FAILED {exc}", file=sys.stderr, flush=True)
                return ah.slug, None, str(exc)

        with ThreadPoolExecutor(self.concurrency) as pool:
            for slug, res, err in pool.map(work, jobs):
                if err:
                    errors[slug] = err
                else:
                    results[slug] = res
        return results, errors

    # ---- items -------------------------------------------------------------
    def item_info(self, item_id: int) -> dict:
        """{'name', 'class_id', 'class'}; raises FetchError."""
        doc = self._json(f"/data/wow/item/{item_id}", "static")
        cls = doc.get("item_class") or {}
        return {"name": _name(doc.get("name"), str(item_id)), "class_id": cls.get("id"), "class": _name(cls.get("name"), "")}


def _name(v, default: str) -> str:
    if isinstance(v, dict):  # localized object when no locale is passed
        return v.get("en_US") or next(iter(v.values()), default)
    return v or default
