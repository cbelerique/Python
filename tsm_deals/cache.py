"""On-disk snapshot cache so each hourly run only downloads what is stale.

TSM refreshes realm files every few hours and region stats about daily, and it rate-limits
heavy downloaders. So we keep parsed snapshots between runs (GitHub Actions cache) and spend
a small download budget per run on the oldest ones.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .data import RealmRow, RegionRow, Snapshot


def _key(k: tuple[str, int]) -> str:
    return f"{k[0]}:{k[1]}"


def _unkey(s: str) -> tuple[str, int]:
    kind, i = s.split(":")
    return kind, int(i)


class SnapshotCache:
    def __init__(self, root: Path):
        self.root = root
        (root / "ah").mkdir(parents=True, exist_ok=True)

    # --- realm snapshots -------------------------------------------------
    def _ah_path(self, slug: str) -> Path:
        return self.root / "ah" / f"{slug}.json"

    def fetched_at(self, slug: str) -> datetime | None:
        try:
            return datetime.fromisoformat(json.loads(self._ah_path(slug).read_text())["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None

    def load_ah(self, slug: str) -> Snapshot | None:
        try:
            raw = json.loads(self._ah_path(slug).read_text())
        except (OSError, ValueError):
            return None
        snap = Snapshot(updated_at=datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None)
        for k, (mb, mv) in raw["rows"].items():
            snap.rows[_unkey(k)] = RealmRow(mb, mv)
        snap.names = {_unkey(k): v for k, v in raw.get("names", {}).items()}
        return snap

    def save_ah(self, slug: str, snap: Snapshot, fetched_at: datetime) -> None:
        raw = {
            "fetched_at": fetched_at.isoformat(),
            "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            "rows": {_key(k): [r.min_buyout, r.market_value] for k, r in snap.rows.items()},
            "names": {_key(k): v for k, v in snap.names.items() if k in snap.rows},
        }
        self._ah_path(slug).write_text(json.dumps(raw, separators=(",", ":")))

    # --- region stats + commodity ids ------------------------------------
    def load_region(self):
        try:
            raw = json.loads((self.root / "region.json").read_text())
        except (OSError, ValueError):
            return None
        region = {_unkey(k): RegionRow(*v) for k, v in raw["rows"].items()}
        upd = datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None
        return region, upd, set(raw["commodities"]), datetime.fromisoformat(raw["fetched_at"])

    def save_region(self, region: dict, updated_at, commodities: set[int], fetched_at: datetime) -> None:
        raw = {
            "fetched_at": fetched_at.isoformat(),
            "updated_at": updated_at.isoformat() if updated_at else None,
            "commodities": sorted(commodities),
            "rows": {
                _key(k): [r.name, r.market_value, r.avg_sale_price, r.sale_rate, r.sold_per_day]
                for k, r in region.items()
            },
        }
        (self.root / "region.json").write_text(json.dumps(raw, separators=(",", ":")))
