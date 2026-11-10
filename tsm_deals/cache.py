"""State kept between hourly runs (GitHub Actions cache): auction snapshots, realm map,
item classes, region stats, and which deals were already sent."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .data import AuctionHouse, RealmRow, RegionRow, Snapshot


def _key(k: tuple[str, int]) -> str:
    return f"{k[0]}:{k[1]}"


def _unkey(s: str) -> tuple[str, int]:
    kind, i = s.split(":")
    return kind, int(i)


def _dt(v):
    return datetime.fromisoformat(v) if v else None


class StateCache:
    def __init__(self, root: Path):
        self.root = root
        (root / "ah").mkdir(parents=True, exist_ok=True)

    def _read(self, name: str):
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return None

    def _write(self, name: str, data) -> None:
        (self.root / name).write_text(json.dumps(data, separators=(",", ":")))

    # ---- auction snapshots -------------------------------------------------
    def load_ah(self, slug: str) -> Snapshot | None:
        raw = self._read(f"ah/{slug}.json")
        if not raw:
            return None
        snap = Snapshot(updated_at=_dt(raw.get("updated_at")))
        for k, v in raw["rows"].items():
            snap.rows[_unkey(k)] = RealmRow(*v)
        return snap

    def save_ah(self, slug: str, snap: Snapshot) -> None:
        self._write(f"ah/{slug}.json", {
            "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            "rows": {_key(k): [r.min_buyout, r.next_buyout, r.count] for k, r in snap.rows.items()},
        })

    # ---- connected realms --------------------------------------------------
    def load_realms(self):
        raw = self._read("realms.json")
        if not raw:
            return None, None
        return [AuctionHouse(**a) for a in raw["ahs"]], _dt(raw["fetched_at"])

    def save_realms(self, ahs: list[AuctionHouse], fetched_at: datetime) -> None:
        self._write("realms.json", {
            "fetched_at": fetched_at.isoformat(),
            "ahs": [{"id": a.id, "realms": a.realms, "population": a.population} for a in ahs],
        })

    # ---- item info (class never changes) -----------------------------------
    def load_items(self) -> dict[str, dict]:
        return self._read("items.json") or {}

    def save_items(self, items: dict[str, dict]) -> None:
        self._write("items.json", items)

    # ---- region stats ------------------------------------------------------
    def load_region(self):
        raw = self._read("region.json")
        if not raw:
            return None
        region = {_unkey(k): RegionRow(*v) for k, v in raw["rows"].items()}
        return region, _dt(raw.get("updated_at")), set(raw["commodities"]), _dt(raw["fetched_at"])

    def save_region(self, region: dict, updated_at, commodities: set[int], fetched_at: datetime) -> None:
        self._write("region.json", {
            "fetched_at": fetched_at.isoformat(),
            "updated_at": updated_at.isoformat() if updated_at else None,
            "commodities": sorted(commodities),
            "rows": {_key(k): [r.name, r.market_value, r.avg_sale_price, r.sale_rate, r.sold_per_day] for k, r in region.items()},
        })

    # ---- sent deals --------------------------------------------------------
    def load_sent(self) -> dict[str, str]:
        return self._read("sent.json") or {}

    def save_sent(self, sent: dict[str, str]) -> None:
        self._write("sent.json", sent)
