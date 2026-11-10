"""Render deals as Markdown (GitHub run summary) and Telegram HTML, and send to Telegram."""

from __future__ import annotations

import html
import os
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


def tsm_import_strings(deals: list[Deal]) -> dict[str, str]:
    """Per buy realm, a TSM group-import string (i:<itemId> / p:<speciesId>)."""
    by_realm: dict[str, list[str]] = defaultdict(list)
    for d in deals:
        by_realm[d.buy_ah.display()].append(f"{'p' if d.kind == 'pet' else 'i'}:{d.id}")
    return {k: ",".join(v) for k, v in by_realm.items()}


def _facts(d: Deal) -> dict:
    tags = []
    if d.kind == "pet":
        tags.append("pet")
    elif d.item_class:
        tags.append(d.item_class.lower())
    if d.same_realm:
        tags.append("same-realm")
    competition = "none listed there" if d.sell_listed == 0 else f"{d.sell_listed} listed there"
    return {
        "tags": " · ".join(tags),
        "buy": f"{d.buy_ah.display()} for {gold(d.buy_price)}",
        "sell": f"{d.sell.display} at {gold(d.list_price)}",
        "stats": f"net {gold(d.net)} · profit {gold(d.profit)} · {d.discount:.0%} under net",
        "speed": f"~{d.sold_per_day:.2f} sold/day/realm · sale rate {d.sale_rate:.0%} · {competition}",
        "also": ", ".join(f"{ah.display()} {gold(p)}" for ah, p in d.other_sources),
    }


def header(n: int, stats: dict, now: datetime) -> str:
    return (
        f"{n} new deal{'s' if n != 1 else ''} · {now:%H:%M} UTC · auction data {stats.get('realm_age', '?')} old · "
        f"{stats.get('ahs_scanned', 0)}/{stats.get('ahs_total', '?')} realms"
    )


def markdown(deals: list[Deal], stats: dict, now: datetime) -> str:
    out = ["## TSM deals", f"_{header(len(deals), stats, now)}_", ""]
    if not deals:
        out.append("No new deals at 70%+ under net this hour.")
    for i, d in enumerate(deals, 1):
        f = _facts(d)
        out.append(f"**{i}. [{d.name}]({wowhead(d)})** {f['tags']}")
        out.append(f"- BUY on **{f['buy']}** → SELL on **{f['sell']}**")
        out.append(f"- {f['stats']}")
        out.append(f"- {f['speed']}")
        if f["also"]:
            out.append(f"- also cheap on: {f['also']}")
        out.append("")
    if deals:
        out.append("**TSM group import by buy realm:**")
        out += [f"- {realm}: `{s}`" for realm, s in tsm_import_strings(deals).items()]
    if stats.get("notes"):
        out += ["", *(f"_{n}_" for n in stats["notes"])]
    return "\n".join(out)


def telegram_messages(deals: list[Deal], stats: dict, now: datetime, limit: int = 3900) -> list[str]:
    """Telegram HTML, split into messages under the 4096-char limit, never mid-deal."""
    e = html.escape
    blocks = [f"<b>💰 {e(header(len(deals), stats, now))}</b>"]
    for i, d in enumerate(deals, 1):
        f = _facts(d)
        lines = [
            f'<b>{i}. <a href="{wowhead(d)}">{e(d.name)}</a></b>' + (f" <i>{e(f['tags'])}</i>" if f["tags"] else ""),
            f"🛒 BUY <b>{e(f['buy'])}</b>",
            f"📈 SELL <b>{e(f['sell'])}</b>",
            f"💵 {e(f['stats'])}",
            f"⏱ {e(f['speed'])}",
        ]
        if f["also"]:
            lines.append(f"↪ also: {e(f['also'])}")
        blocks.append("\n".join(lines))
    imports = tsm_import_strings(deals)
    if imports:
        blocks.append("<b>TSM import</b>\n" + "\n".join(f"{e(r)}: <code>{e(s)}</code>" for r, s in imports.items()))
    for n in stats.get("notes", []):
        blocks.append(f"<i>{e(n)}</i>")

    messages, buf = [], ""
    for b in blocks:
        b = b[:limit]
        if buf and len(buf) + len(b) + 2 > limit:
            messages.append(buf)
            buf = ""
        buf = f"{buf}\n\n{b}" if buf else b
    if buf:
        messages.append(buf)
    return messages


def send_telegram(token: str, chat_id: str, messages: list[str]) -> None:
    url = f"{os.environ.get('TELEGRAM_API_URL', 'https://api.telegram.org')}/bot{token}/sendMessage"
    for text in messages:
        body = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        for attempt in range(4):
            try:
                urllib.request.urlopen(req, timeout=30).close()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                detail = exc.read().decode(errors="replace")[:300]
                raise RuntimeError(f"Telegram send failed: HTTP {exc.code} {detail}") from exc
        time.sleep(1)
