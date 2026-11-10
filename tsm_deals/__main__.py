"""Hourly entry point: python -m tsm_deals [--config config.json] [--dry-run]

Env:
  DISCORD_WEBHOOK_URL  post the report to this Discord channel webhook (optional)
  GITHUB_STEP_SUMMARY  set by GitHub Actions; the report is also written there
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cache import SnapshotCache
from .data import FetchError, TSMClient, load_auction_houses, rate_limited
from .engine import Config, SellOption, buy_thresholds, find_deals
from .report import markdown, send_discord

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text())
    known = {f.name for f in dataclasses.fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise SystemExit(f"Unknown config keys: {sorted(unknown)}")
    return Config(**raw)


def age(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "?"
    return f"{(now - ts).total_seconds() / 3600:.1f}h"


def load_sent(path: Path, now: datetime, hours: float) -> dict[str, str]:
    try:
        sent = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = now - timedelta(hours=hours)
    return {k: v for k, v in sent.items() if datetime.fromisoformat(v) > cutoff}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--realms", default=str(ROOT / "data" / "us_realms.json"))
    ap.add_argument("--state", default=str(ROOT / "state"), help="cache + sent-deal history directory")
    ap.add_argument("--dry-run", action="store_true", help="print only; no Discord, no sent-history update")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    now = datetime.now(timezone.utc)
    state = Path(args.state)
    cache = SnapshotCache(state)
    ahs = load_auction_houses(Path(args.realms))
    by_slug = {s: ah for ah in ahs for s in ah.slugs}
    client = TSMClient(cfg.region, cfg.concurrency)
    max_age = timedelta(hours=cfg.max_data_age_hours)
    errors: dict[str, str] = {}
    budget = cfg.max_downloads_per_run
    per_ah = len(cfg.kinds)

    def stale(fetched: datetime | None, hours: float) -> bool:
        return fetched is None or now - fetched > timedelta(hours=hours)

    # 1) Region sales stats + commodity list (refreshed daily upstream).
    cached = cache.load_region()
    if cached is None or stale(cached[3], cfg.region_refresh_hours):
        try:
            print("Downloading region sales data + commodity list...", file=sys.stderr, flush=True)
            region, region_ts = client.region(cfg.kinds)
            commodity_ids = client.commodity_ids()
            cache.save_region(region, region_ts, commodity_ids, now)
            budget -= per_ah + 1
        except FetchError as exc:
            if cached is None:
                raise SystemExit(f"No region data available: {exc}")
            errors["region"] = str(exc)
            region, region_ts, commodity_ids, _ = cached
    else:
        region, region_ts, commodity_ids, _ = cached

    # 2) Sell realms: full snapshots (we need their current lowest price for every item).
    sell_ahs = []
    for slug in cfg.sell_realms:
        if slug not in by_slug:
            raise SystemExit(f"Unknown sell realm slug: {slug}")
        if all(by_slug[slug] is not ah for _, ah in sell_ahs):
            sell_ahs.append((slug, by_slug[slug]))
    sell_slugs = {ah.slug for _, ah in sell_ahs}
    due = sorted(
        (ah for _, ah in sell_ahs if stale(cache.fetched_at(ah.slug), cfg.sell_refresh_hours)),
        key=lambda ah: cache.fetched_at(ah.slug) or datetime.min.replace(tzinfo=timezone.utc),
    )[: max(0, budget // per_ah)]
    if due:
        print(f"Downloading {len(due)} sell-realm auction houses...", file=sys.stderr, flush=True)
        got, errs = client.realms(due, cfg.kinds)
        errors.update(errs)
        for slug, snap in got.items():
            cache.save_ah(slug, snap, now)
        budget -= per_ah * len(due)
    sell_snaps = {ah.slug: cache.load_ah(ah.slug) for _, ah in sell_ahs}
    sells = [
        SellOption(ah=ah, realm_slug=slug, tier=0 if i == 0 else 1, snap=sell_snaps[ah.slug])
        for i, (slug, ah) in enumerate(sell_ahs)
        if sell_snaps[ah.slug] is not None
    ]

    # 3) Source realms: oldest first, within what is left of the budget. Only rows cheap
    #    enough to possibly be a deal are stored.
    thresholds = buy_thresholds(region, sells, commodity_ids, cfg)
    sources_wanted = [by_slug[s] for s in cfg.source_realms] if cfg.source_realms else ahs
    rotating = [ah for ah in sources_wanted if ah.slug not in sell_slugs]
    rotating.sort(key=lambda ah: cache.fetched_at(ah.slug) or datetime.min.replace(tzinfo=timezone.utc))
    due = [ah for ah in rotating if stale(cache.fetched_at(ah.slug), cfg.source_refresh_hours)]
    due = due[: max(0, budget // per_ah)] if not rate_limited() else []
    if due:
        print(f"Downloading {len(due)} source auction houses ({len(thresholds)} items in play)...", file=sys.stderr, flush=True)
        # keep a margin so cached rows stay useful as prices drift between refreshes
        keep = lambda key, mb: 0 < mb <= min(cfg.max_buy, int(thresholds.get(key, 0) * 1.5))  # noqa: E731
        got, errs = client.realms(due, cfg.kinds, keep)
        errors.update(errs)
        for slug, snap in got.items():
            cache.save_ah(slug, snap, now)

    sources = {}
    oldest = None
    for ah in sources_wanted:
        snap = sell_snaps.get(ah.slug) if ah.slug in sell_slugs else cache.load_ah(ah.slug)
        if snap is None:
            continue
        if snap.updated_at and now - snap.updated_at > max_age:
            errors.setdefault(ah.slug, f"stale data ({age(snap.updated_at, now)})")
            continue
        if snap.updated_at and (oldest is None or snap.updated_at < oldest):
            oldest = snap.updated_at
        sources[ah.slug] = (ah, snap)
    sells = [s for s in sells if not (s.snap.updated_at and now - s.snap.updated_at > max_age)]
    if not sells:
        raise SystemExit(f"No fresh sell-realm data. Errors: {errors}")

    deals = find_deals(region, sells, sources, commodity_ids, cfg)

    sent_path = state / "sent.json"
    sent = load_sent(sent_path, now, cfg.dedupe_hours)
    fresh = [d for d in deals if d.dedupe_key not in sent][: cfg.max_results]

    stats = {
        "realm_age": age(oldest, now),
        "region_age": age(region_ts, now),
        "ahs_scanned": len(sources),
        "ahs_total": len(sources_wanted),
        "errors": errors,
        "rate_limited": rate_limited(),
    }
    report = markdown(fresh, stats, now)
    print(report)
    print(
        f"\n[{len(deals)} qualifying deals, {len(fresh)} new after dedupe; "
        f"{len(sources)}/{len(sources_wanted)} AHs with data; errors: {errors}]",
        file=sys.stderr,
    )

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(report + "\n")

    if args.dry_run:
        return 0

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook and fresh:
        send_discord(webhook, report)
    elif not webhook:
        print("DISCORD_WEBHOOK_URL not set; report only printed.", file=sys.stderr)

    for d in fresh:
        sent[d.dedupe_key] = now.isoformat()
    sent_path.write_text(json.dumps(sent, indent=0, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
