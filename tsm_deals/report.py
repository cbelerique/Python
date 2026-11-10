"""Turn deals into a Markdown report and Discord messages."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

from .data import COPPER_PER_GOLD, USER_AGENT
from .engine import Deal


def gold(copper: int) -> str:
    g = copper / COPPER_PER_GOLD
    if g >= 100:
        return f"{g:,.0f}g"
    return f"{g:,.2f}".rstrip("0").rstrip(".") + "g"


def wowhead(d: Deal) -> str:
    return f"https://www.wowhead.com/battle-pet/{d.id}" if d.kind == "pet" else f"https://www.wowhead.com/item={d.id}"


def realm_name(d: Deal) -> str:
    return d.sell.display


def tsm_import_strings(deals: list[Deal]) -> dict[str, str]:
    """Per buy realm, a TSM group-import string (i:<itemId> / p:<speciesId>)."""
    by_realm: dict[str, list[str]] = defaultdict(list)
    for d in deals:
        by_realm[d.buy_ah.display()].append(f"{'p' if d.kind == 'pet' else 'i'}:{d.id}")
    return {k: ",".join(v) for k, v in by_realm.items()}


def deal_line(i: int, d: Deal) -> str:
    tag = " · same-realm" if d.same_realm else ""
    if d.cheap_on_full_realms >= 2:
        tag += f" · ⚠ also this cheap on {d.cheap_on_full_realms} other full realms: check item level/quality"
    lines = [
        f"**{i}. [{d.name}]({wowhead(d)})**{tag}",
        f"  BUY on **{d.buy_ah.display()}** for **{gold(d.buy_price)}**"
        f" → SELL on **{realm_name(d)}** at **{gold(d.list_price)}**",
        f"  Net after 5% cut {gold(d.net)} · profit **{gold(d.profit)}** · "
        f"{d.discount:.0%} under net · sells ~{d.sold_per_day:.2f}/day, sale rate {d.sale_rate:.1%}",
    ]
    if d.other_sources:
        alts = ", ".join(f"{ah.display()} {gold(p)}" for ah, p in d.other_sources)
        lines.append(f"  Also cheap on: {alts}")
    if d.alt_sells:
        lines.append("  Other full realms: " + ", ".join(f"{s} {gold(p)}" for s, p in d.alt_sells))
    return "\n".join(lines)


def markdown(deals: list[Deal], stats: dict, now: datetime) -> str:
    out = [
        f"## TSM deals: {len(deals)} new",
        f"_{now:%Y-%m-%d %H:%M} UTC · realm data age {stats.get('realm_age', '?')} · "
        f"region sales data age {stats.get('region_age', '?')} · "
        f"{stats.get('ahs_scanned', 0)}/{stats.get('ahs_total', '?')} auction houses with data_",
        "",
    ]
    if not deals:
        out.append("No new deals at 70%+ under net with these filters this hour.")
    for i, d in enumerate(deals, 1):
        out.append(deal_line(i, d))
        out.append("")
    if deals:
        out.append("**TSM import strings by buy realm** (TSM → Groups → Import):")
        for realm, s in tsm_import_strings(deals).items():
            out.append(f"- {realm}: `{s}`")
    if stats.get("rate_limited"):
        out.append("")
        out.append("_TSM rate-limited this run; some realms were skipped and will be retried next hour._")
    if stats.get("errors"):
        out.append("")
        out.append(f"_Skipped {len(stats['errors'])} auction houses (download errors or stale data)._")
    out.append("")
    out.append("_Prices are from TSM snapshots taken hours ago. Check the listing and item level in-game before buying._")
    return "\n".join(out)


def _chunks(text: str, limit: int = 1900):
    buf = ""
    for block in text.split("\n\n"):
        piece = block + "\n\n"
        while len(piece) > limit:
            if buf:
                yield buf
                buf = ""
            yield piece[:limit]
            piece = piece[limit:]
        if len(buf) + len(piece) > limit:
            yield buf
            buf = ""
        buf += piece
    if buf.strip():
        yield buf


def send_discord(webhook: str, text: str) -> None:
    for chunk in _chunks(text):
        body = json.dumps({"content": chunk, "flags": 4}).encode()  # 4 = suppress link embeds
        req = urllib.request.Request(
            webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        for attempt in range(4):
            try:
                urllib.request.urlopen(req, timeout=30).close()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(2 + attempt * 2)
                    continue
                raise
        time.sleep(0.5)
