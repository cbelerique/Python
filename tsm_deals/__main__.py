"""Hourly entry point: python -m tsm_deals [--config config.json] [--dry-run]

Env:
  BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET  free API client from develop.battle.net (required)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID         where to send deals (optional; otherwise print only)
  GITHUB_STEP_SUMMARY                          set by GitHub Actions; report is written there too
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .blizzard import Blizzard
from .cache import StateCache
from .data import FetchError, TSMRegion
from .engine import Config, SellOption, buy_thresholds, find_deals
from .report import markdown, send_telegram, telegram_messages

ROOT = Path(__file__).resolve().parent.parent
MAX_ITEM_LOOKUPS = 400


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
    mins = (now - ts).total_seconds() / 60
    return f"{mins:.0f}m" if mins < 120 else f"{mins / 60:.1f}h"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--state", default=str(ROOT / "state"), help="cache + sent-deal history directory")
    ap.add_argument("--dry-run", action="store_true", help="print only; no Telegram, no sent-history update")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    now = datetime.now(timezone.utc)
    cache = StateCache(Path(args.state))
    notes: list[str] = []
    errors: dict[str, str] = {}

    cid, secret = os.environ.get("BLIZZARD_CLIENT_ID", ""), os.environ.get("BLIZZARD_CLIENT_SECRET", "")
    if not cid or not secret:
        raise SystemExit("Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET (free at develop.battle.net).")
    bz = Blizzard(cfg.region, cid, secret, cfg.concurrency)

    def stale(fetched: datetime | None, hours: float) -> bool:
        return fetched is None or now - fetched > timedelta(hours=hours)

    # 1) Connected realms + population (changes rarely).
    ahs, fetched = cache.load_realms()
    if ahs is None or stale(fetched, cfg.realm_list_refresh_hours):
        print("Loading connected-realm list from Blizzard...", file=sys.stderr, flush=True)
        ahs = bz.connected_realms()
        cache.save_realms(ahs, now)
    by_slug = {s: ah for ah in ahs for s in ah.slugs}

    # 2) Region demand stats from TSM (what actually sells, how often).
    cached = cache.load_region()
    if cached is None or stale(cached[3], cfg.region_refresh_hours):
        try:
            print("Downloading TSM region sales stats...", file=sys.stderr, flush=True)
            tsm = TSMRegion(cfg.region)
            region, region_ts = tsm.region(cfg.kinds)
            commodity_ids = tsm.commodity_ids()
            cache.save_region(region, region_ts, commodity_ids, now)
        except FetchError as exc:
            if cached is None:
                raise SystemExit(f"No TSM region data available: {exc}")
            notes.append(f"TSM region stats refresh failed; using cached copy ({exc}).")
            region, region_ts, commodity_ids, _ = cached
    else:
        region, region_ts, commodity_ids, _ = cached

    # 3) Which auction houses we sell on: configured priority list, then any FULL realm.
    sell_list: list[tuple[str, object]] = []
    for slug in cfg.sell_realms:
        if slug not in by_slug:
            raise SystemExit(f"Unknown sell realm slug: {slug}")
        if all(by_slug[slug] is not a for _, a in sell_list):
            sell_list.append((slug, by_slug[slug]))
    if cfg.include_full_realms:
        for ah in sorted(ahs, key=lambda a: a.display()):
            if ah.population.upper() == "FULL" and all(ah is not a for _, a in sell_list):
                sell_list.append((ah.realms[0]["slug"], ah))
    sell_ids = {a.slug for _, a in sell_list}
    sources_wanted = [by_slug[s] for s in cfg.source_realms] if cfg.source_realms else ahs

    # 4) Hourly auctions for every AH; unchanged ones answer 304 and we reuse the cache.
    thresholds = buy_thresholds(region, commodity_ids, cfg)
    keep = lambda key, mb: 0 < mb <= thresholds.get(key, 0)  # noqa: E731

    def cache_name(ah) -> str:
        return f"full-{ah.slug}" if ah.slug in sell_ids else f"src-{ah.slug}"

    wanted = {a.slug: a for a in sources_wanted} | {a.slug: a for _, a in sell_list}
    jobs = []
    for ah in wanted.values():
        old = cache.load_ah(cache_name(ah))
        jobs.append((ah, old.updated_at if old else None, None if ah.slug in sell_ids else keep))
    print(f"Fetching auctions for {len(jobs)} auction houses ({len(sell_ids)} sell realms)...", file=sys.stderr, flush=True)
    results, errs = bz.auctions_many(jobs)
    errors.update(errs)
    for ah in wanted.values():
        res = results.get(ah.slug)
        if res and res[0] is not None:
            cache.save_ah(cache_name(ah), res[0])

    max_age = timedelta(hours=cfg.max_data_age_hours)
    snaps, oldest, stale_n = {}, None, 0
    for ah in wanted.values():
        snap = cache.load_ah(cache_name(ah))
        if snap is None:
            continue
        if snap.updated_at and now - snap.updated_at > max_age:
            stale_n += 1
            continue
        snaps[ah.slug] = snap
        if snap.updated_at and (oldest is None or snap.updated_at < oldest):
            oldest = snap.updated_at
    if stale_n:
        notes.append(f"{stale_n} realms skipped: Blizzard data older than {cfg.max_data_age_hours:g}h.")
    if errors:
        notes.append(f"{len(errors)} realms failed to download this hour.")

    sells = [
        SellOption(ah=a, realm_slug=slug, tier=0 if i == 0 else 1, snap=snaps[a.slug])
        for i, (slug, a) in enumerate(sell_list)
        if a.slug in snaps
    ]
    if not sells:
        raise SystemExit(f"No fresh sell-realm data. Errors: {errors}")
    sources = {a.slug: (a, snaps[a.slug]) for a in sources_wanted if a.slug in snaps}

    # 5) Deals, then drop gear (weapons, armor, profession equipment) via Blizzard item class.
    deals = find_deals(region, sells, sources, commodity_ids, cfg)
    items = cache.load_items()
    lookups = unknown = 0
    kept = []
    excluded = set(cfg.exclude_item_classes)
    for d in deals:
        if d.kind == "item":
            info = items.get(str(d.id))
            if info is None and lookups < MAX_ITEM_LOOKUPS:
                lookups += 1
                try:
                    info = items[str(d.id)] = bz.item_info(d.id)
                except FetchError:
                    info = None
            if info is None:
                unknown += 1  # fail closed: never send something that might be gear
                continue
            if info.get("class_id") in excluded:
                continue
            d.name = info.get("name") or d.name
            d.item_class = info.get("class") or ""
        kept.append(d)
    cache.save_items(items)
    if unknown:
        notes.append(f"{unknown} candidate items skipped: item type lookup unavailable.")

    sent = {k: v for k, v in cache.load_sent().items() if datetime.fromisoformat(v) > now - timedelta(hours=cfg.dedupe_hours)}
    fresh = [d for d in kept if d.dedupe_key not in sent][: cfg.max_results]

    stats = {
        "realm_age": age(oldest, now),
        "ahs_scanned": len(sources),
        "ahs_total": len(sources_wanted),
        "notes": notes,
    }
    print(markdown(fresh, stats, now))
    print(
        f"\n[{len(deals)} candidates, {len(kept)} after gear filter, {len(fresh)} new; "
        f"sell realms: {[s.display for s in sells]}; errors: {errors}]",
        file=sys.stderr,
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(markdown(fresh, stats, now) + "\n")

    if args.dry_run:
        return 0

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        if fresh:
            send_telegram(token, chat, telegram_messages(fresh, stats, now))
    else:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; report only printed.", file=sys.stderr)

    for d in fresh:
        sent[d.dedupe_key] = now.isoformat()
    cache.save_sent(sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
