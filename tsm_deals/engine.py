"""Deal math. Pure functions, no I/O, so they are easy to test.

Pricing model (speed first, not maximum revenue):
  list price on sell realm = min(region avg *sale* price,
                                 region market value,
                                 current lowest listing on that realm minus an undercut)
  net after fees           = list price * (1 - AH cut)            # AH cut is 5%
  a deal qualifies when    buy price <= net * (1 - min_discount)  # 70% off -> buy <= 30% of net
                           and buy price <= max_buy_gold
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import COPPER_PER_GOLD, AuctionHouse, RegionRow, Snapshot


@dataclass
class Config:
    region: str = "us"
    max_buy_gold: float = 1000
    min_discount: float = 0.70
    ah_cut: float = 0.05
    undercut: float = 0.01
    min_profit_gold: float = 25
    min_sale_rate: float = 0.05
    min_sold_per_day: float = 0.25
    max_data_age_hours: float = 8
    include_pets: bool = True
    include_same_realm: bool = True
    sell_realms: list[str] = field(default_factory=lambda: ["illidan"])
    source_realms: list[str] = field(default_factory=list)
    max_results: int = 25
    dedupe_hours: float = 6
    concurrency: int = 2
    max_downloads_per_run: int = 60
    region_refresh_hours: float = 6
    sell_refresh_hours: float = 2
    source_refresh_hours: float = 2

    @property
    def max_buy(self) -> int:
        return int(self.max_buy_gold * COPPER_PER_GOLD)

    @property
    def min_profit(self) -> int:
        return int(self.min_profit_gold * COPPER_PER_GOLD)

    @property
    def kinds(self) -> list[str]:
        return ["item", "pet"] if self.include_pets else ["item"]


@dataclass
class SellOption:
    ah: AuctionHouse
    realm_slug: str  # the configured full realm (e.g. "illidan")
    tier: int  # 0 = top priority realm, 1 = other full realms
    snap: Snapshot

    @property
    def display(self) -> str:
        return next((r["name"] for r in self.ah.realms if r["slug"] == self.realm_slug), self.realm_slug)


@dataclass
class Deal:
    kind: str
    id: int
    name: str
    buy_ah: AuctionHouse
    buy_price: int
    sell: SellOption
    list_price: int
    net: int
    profit: int
    discount: float
    sale_rate: float
    sold_per_day: float
    avg_sale_price: int
    same_realm: bool
    other_sources: list[tuple[AuctionHouse, int]] = field(default_factory=list)
    cheap_on_full_realms: int = 0  # other full realms listing it at a deal price -> variant risk
    alt_sells: list[tuple[str, int]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.id)

    @property
    def speed(self) -> float:
        """Rough 'how fast will this move' score: share of listings that sell x sales/day."""
        return self.sale_rate * min(self.sold_per_day, 20.0)

    @property
    def dedupe_key(self) -> str:
        return f"{self.kind}:{self.id}:{self.buy_ah.slug}:{self.buy_price}"


def list_price(region: RegionRow, sell_min_buyout: int, undercut: float, full_median: int = 0) -> int:
    """Price to list at for a fast sale. 0 means 'no reliable price'.

    full_median: median lowest listing across the full (sell) realms. If busy realms already
    list it cheaply, it will not sell fast for more - and it usually means the expensive
    "average" comes from a better variant (item level / crafted quality) of the same item ID.
    """
    if region.avg_sale_price <= 0:
        return 0
    candidates = [region.avg_sale_price]
    if region.market_value > 0:
        candidates.append(region.market_value)
    if sell_min_buyout > 0:
        candidates.append(int(sell_min_buyout * (1 - undercut)))
    if full_median > 0:
        candidates.append(full_median)
    return max(0, min(candidates))


def full_realm_median(key, sells: list["SellOption"], min_listed: int = 3) -> int:
    prices = sorted(s.snap.rows[key].min_buyout for s in sells if key in s.snap.rows and s.snap.rows[key].min_buyout > 0)
    if len(prices) < min_listed:
        return 0
    return prices[len(prices) // 2]


def net_after_cut(price: int, ah_cut: float) -> int:
    return int(price * (1 - ah_cut))


def max_buy_for(net: int, cfg: Config) -> int:
    return min(cfg.max_buy, int(net * (1 - cfg.min_discount)))


def passes_demand(region: RegionRow | None, cfg: Config) -> bool:
    return (
        region is not None
        and region.avg_sale_price > 0
        and region.sale_rate >= cfg.min_sale_rate
        and region.sold_per_day >= cfg.min_sold_per_day
    )


def buy_thresholds(region: dict, sells: list[SellOption], commodity_ids: set[int], cfg: Config) -> dict:
    """Highest buy price worth keeping per item, so source realm files can be filtered on load."""
    out: dict[tuple[str, int], int] = {}
    for key, r in region.items():
        if key[0] not in cfg.kinds or not passes_demand(r, cfg):
            continue
        if key[0] == "item" and key[1] in commodity_ids:
            continue
        # Upper bound over every sell realm: price as if nobody were listed (region signals only).
        best = max_buy_for(net_after_cut(list_price(r, 0, cfg.undercut), cfg.ah_cut), cfg) if sells else 0
        if best > 0:
            out[key] = best
    return out


def find_deals(
    region: dict,
    sells: list[SellOption],
    sources: dict[str, tuple[AuctionHouse, Snapshot]],
    commodity_ids: set[int],
    cfg: Config,
) -> list[Deal]:
    """One best deal per item.

    Choice per item: the highest-priority sell realm (sell list order) that has any qualifying
    source; cross-realm sources beat same-realm ones, then the cheapest source wins.
    """
    # item -> [(ah, price)] for every source listing under the hard cap
    listings: dict[tuple[str, int], list[tuple[AuctionHouse, int]]] = {}
    names: dict[tuple[str, int], str] = {}
    for ah, snap in sources.values():
        for key, row in snap.rows.items():
            if 0 < row.min_buyout <= cfg.max_buy:
                listings.setdefault(key, []).append((ah, row.min_buyout))
                if key in snap.names:
                    names.setdefault(key, snap.names[key])

    deals: list[Deal] = []
    for key, offers in listings.items():
        kind, item_id = key
        if kind not in cfg.kinds or (kind == "item" and item_id in commodity_ids):
            continue
        r = region.get(key)
        if not passes_demand(r, cfg):
            continue

        chosen: Deal | None = None
        alt_sells: list[tuple[str, int]] = []
        median = full_realm_median(key, sells)
        for s in sells:
            row = s.snap.rows.get(key)
            cross_price = list_price(r, row.min_buyout if row else 0, cfg.undercut, median)
            # Same-realm flip: the realm's lowest listing IS the one we'd buy, so price the
            # resale off the realm's market value instead of that listing.
            same_price = list_price(r, row.market_value if row else 0, cfg.undercut, median)
            ok = []
            for ah, p in offers:
                same = ah.slug == s.ah.slug
                if same and not cfg.include_same_realm:
                    continue
                price = same_price if same else cross_price
                net = net_after_cut(price, cfg.ah_cut)
                if p <= max_buy_for(net, cfg) and net - p >= cfg.min_profit:
                    ok.append((ah, p, price, net))
            if not ok:
                continue
            if chosen is not None:
                alt_sells.append((s.display, cross_price))
                continue
            ok.sort(key=lambda o: (o[0].slug == s.ah.slug, o[1]))
            buy_ah, buy, price, net = ok[0]
            sell_slugs = {x.ah.slug for x in sells}
            cheap_full = sum(1 for o in ok if o[0].slug in sell_slugs and o[0].slug != s.ah.slug)
            chosen = Deal(
                kind=kind,
                id=item_id,
                name=names.get(key) or r.name or f"{kind} {item_id}",
                buy_ah=buy_ah,
                buy_price=buy,
                sell=s,
                list_price=price,
                net=net,
                profit=net - buy,
                discount=1 - buy / net,
                sale_rate=r.sale_rate,
                sold_per_day=r.sold_per_day,
                avg_sale_price=r.avg_sale_price,
                same_realm=buy_ah.slug == s.ah.slug,
                other_sources=[(o[0], o[1]) for o in sorted(ok[1:], key=lambda o: o[1])[:3]],
                cheap_on_full_realms=cheap_full,
            )
        if chosen:
            chosen.alt_sells = alt_sells[:2]
            deals.append(chosen)

    deals.sort(key=rank_key)
    return deals


def rank_key(d: Deal):
    # cross-realm first, variant-risk items last within it, then Illidan/top realm,
    # then fastest movers, then profit
    return (d.same_realm, d.cheap_on_full_realms >= 2, d.sell.tier, -round(d.speed, 3), -d.profit)
