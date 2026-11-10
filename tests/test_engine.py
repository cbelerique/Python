import unittest

from tsm_deals.blizzard import snapshot_from_auctions
from tsm_deals.data import AuctionHouse, RealmRow, RegionRow, Snapshot, parse_region_csv
from tsm_deals.engine import Config, SellOption, find_deals, list_price
from tsm_deals.report import gold, markdown

G = 10_000  # copper per gold


_ids = iter(range(1, 10_000))


def ah(slug):
    return AuctionHouse(next(_ids), [{"slug": slug, "name": slug.title()}])


def snap(rows):
    s = Snapshot()
    for key, mb in rows.items():
        s.rows[key] = RealmRow(min_buyout=mb, count=1)
    return s


ILLIDAN, AREA52, SMALL, OTHER = ah("illidan"), ah("area-52"), ah("small-realm"), ah("other-realm")
KEY = ("item", 100)
REGION = {KEY: RegionRow("Thing", 1000 * G, 1000 * G, 0.30, 3.0)}


def sells(illidan_rows, area_rows=None):
    return [
        SellOption(ILLIDAN, "illidan", 0, snap(illidan_rows)),
        SellOption(AREA52, "area-52", 1, snap(area_rows or {})),
    ]


class Pricing(unittest.TestCase):
    def test_list_price_takes_lowest_signal(self):
        r = RegionRow("x", 1200 * G, 1000 * G, 0.3, 3)
        self.assertEqual(list_price(r, 0, 0.01), 1000 * G)  # nobody listed: sell at avg sale price
        self.assertEqual(list_price(r, 800 * G, 0.01), 792 * G)  # undercut a cheaper listing

    def test_no_sales_data_means_no_price(self):
        self.assertEqual(list_price(RegionRow("x", 100, 0, 0.3, 3), 100, 0.01), 0)


class Deals(unittest.TestCase):
    cfg = Config(sell_realms=["illidan", "area-52"], min_profit_gold=25)

    def test_cross_realm_70_percent_after_fees(self):
        # net on Illidan = 1000g * 0.95 = 950g -> must buy at or under 285g
        sources = {"small-realm": (SMALL, snap({KEY: 285 * G}))}
        deals = find_deals(REGION, sells({}), sources, set(), self.cfg)
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual(d.sell.realm_slug, "illidan")
        self.assertIs(d.buy_ah, SMALL)
        self.assertEqual(d.net, 950 * G)
        self.assertFalse(d.same_realm)

    def test_not_deep_enough_discount(self):
        sources = {"small-realm": (SMALL, snap({KEY: 286 * G}))}
        self.assertEqual(find_deals(REGION, sells({}), sources, set(), self.cfg), [])

    def test_buy_cap_1000g(self):
        region = {KEY: RegionRow("Thing", 10_000 * G, 10_000 * G, 0.3, 3)}
        sources = {"small-realm": (SMALL, snap({KEY: 1001 * G}))}
        self.assertEqual(find_deals(region, sells({}), sources, set(), self.cfg), [])
        sources = {"small-realm": (SMALL, snap({KEY: 1000 * G}))}
        self.assertEqual(len(find_deals(region, sells({}), sources, set(), self.cfg)), 1)

    def test_commodities_excluded(self):
        sources = {"small-realm": (SMALL, snap({KEY: 10 * G}))}
        self.assertEqual(find_deals(REGION, sells({}), sources, {100}, self.cfg), [])

    def test_slow_movers_excluded(self):
        region = {KEY: RegionRow("Thing", 1000 * G, 1000 * G, 0.01, 0.05)}
        sources = {"small-realm": (SMALL, snap({KEY: 10 * G}))}
        self.assertEqual(find_deals(region, sells({}), sources, set(), self.cfg), [])

    def test_falls_back_to_next_full_realm_when_illidan_is_flooded(self):
        # Illidan already has it listed at 100g -> net too low; Area 52 has none listed.
        sources = {"small-realm": (SMALL, snap({KEY: 200 * G}))}
        deals = find_deals(REGION, sells({KEY: 100 * G}, {}), sources, set(), self.cfg)
        self.assertEqual(deals[0].sell.realm_slug, "area-52")

    def test_same_realm_flip_priced_off_second_listing(self):
        # Illidan's lowest listing is 50g but it normally trades ~1000g: buy it and relist there.
        ill = Snapshot()
        ill.rows[KEY] = RealmRow(min_buyout=50 * G, next_buyout=1000 * G)
        cfg = Config(sell_realms=["illidan"])
        s = [SellOption(ILLIDAN, "illidan", 0, ill)]
        deals = find_deals(REGION, s, {"illidan": (ILLIDAN, ill)}, set(), cfg)
        self.assertEqual(len(deals), 1)
        self.assertTrue(deals[0].same_realm)
        self.assertEqual(deals[0].list_price, 990 * G)

    def test_cross_realm_preferred_and_ranked_first(self):
        k2 = ("item", 200)
        region = dict(REGION)
        region[k2] = RegionRow("Other", 1000 * G, 1000 * G, 0.9, 10)  # faster seller
        ill = Snapshot()
        ill.rows[KEY] = RealmRow(min_buyout=40 * G, next_buyout=1000 * G)
        ill.rows[k2] = RealmRow(min_buyout=40 * G, next_buyout=1000 * G)
        sources = {"illidan": (ILLIDAN, ill), "small-realm": (SMALL, snap({KEY: 60 * G}))}
        cfg = Config(sell_realms=["illidan"])
        deals = find_deals(region, [SellOption(ILLIDAN, "illidan", 0, ill)], sources, set(), cfg)
        by_id = {d.id: d for d in deals}
        # item 100: Illidan has a 40g listing, so a cross-realm buy at 60g can only be resold
        # at ~39.6g there -> not a deal; the same-realm 40g buy is.
        self.assertTrue(by_id[100].same_realm)
        self.assertTrue(by_id[200].same_realm)
        # faster seller ranks first among same-realm
        self.assertEqual(deals[0].id, 200)

    def test_cross_realm_ranked_before_same_realm(self):
        k2 = ("item", 200)
        region = dict(REGION)
        region[k2] = RegionRow("Other", 1000 * G, 1000 * G, 0.9, 10)
        ill = Snapshot()
        ill.rows[k2] = RealmRow(min_buyout=40 * G, next_buyout=1000 * G)
        sources = {"illidan": (ILLIDAN, ill), "small-realm": (SMALL, snap({KEY: 60 * G}))}
        cfg = Config(sell_realms=["illidan"])
        deals = find_deals(region, [SellOption(ILLIDAN, "illidan", 0, ill)], sources, set(), cfg)
        self.assertEqual([d.id for d in deals], [100, 200])
        self.assertFalse(deals[0].same_realm)

    def test_same_realm_can_be_disabled(self):
        cfg = Config(sell_realms=["illidan"], include_same_realm=False)
        sources = {"illidan": (ILLIDAN, snap({KEY: 50 * G}))}
        s = [SellOption(ILLIDAN, "illidan", 0, snap({KEY: 50 * G}))]
        self.assertEqual(find_deals(REGION, s, sources, set(), cfg), [])

    def test_report_renders(self):
        sources = {"small-realm": (SMALL, snap({KEY: 100 * G}))}
        deals = find_deals(REGION, sells({}), sources, set(), self.cfg)
        from datetime import datetime, timezone

        text = markdown(deals, {"ahs_scanned": 3}, datetime.now(timezone.utc))
        self.assertIn("BUY on **Small-Realm for 100g**", text)
        self.assertIn("SELL on **Illidan at", text)
        self.assertIn("i:100", text)
        self.assertEqual(gold(12_345), "1.23g")


class VariantGuard(unittest.TestCase):
    def test_cheap_on_most_full_realms_caps_price(self):
        # Busy realms list it at ~50g; only Illidan shows 900g -> not a fast 70%-off flip at 40g.
        cfg = Config(sell_realms=["illidan", "a", "b", "c"])
        s = [SellOption(ILLIDAN, "illidan", 0, snap({KEY: 900 * G}))] + [
            SellOption(ah(x), x, 1, snap({KEY: 50 * G})) for x in ("a", "b", "c")
        ]
        sources = {"small-realm": (SMALL, snap({KEY: 40 * G}))}
        self.assertEqual(find_deals(REGION, s, sources, set(), cfg), [])


class Parsing(unittest.TestCase):
    def test_blizzard_auctions_to_snapshot(self):
        payload = {"auctions": [
            {"id": 1, "item": {"id": 500}, "buyout": 3000, "quantity": 1},
            {"id": 2, "item": {"id": 500}, "buyout": 1000, "quantity": 1},
            {"id": 3, "item": {"id": 500}, "buyout": 2000, "quantity": 1},
            {"id": 4, "item": {"id": 600}, "bid": 5, "quantity": 1},  # bid-only: ignored
            {"id": 5, "item": {"id": 700}, "buyout": 900, "quantity": 3},  # stack: unit 300
            {"id": 6, "item": {"id": 82800, "pet_species_id": 3530, "pet_level": 1}, "buyout": 7000, "quantity": 1},
        ]}
        s = snapshot_from_auctions(payload)
        self.assertEqual(s.rows[("item", 500)], RealmRow(1000, 2000, 3))
        self.assertNotIn(("item", 600), s.rows)
        self.assertEqual(s.rows[("item", 700)].min_buyout, 300)
        self.assertEqual(s.rows[("pet", 3530)].min_buyout, 7000)
        self.assertNotIn(("item", 82800), s.rows)

    def test_region_csv(self):
        region = {}
        parse_region_csv(
            "itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n"
            "1214,Gnoll Punisher,208322203,312890250,10000000,0.005,0,2026-11-09T08:26:08Z\n",
            "item",
            region,
        )
        self.assertEqual(region[("item", 1214)].avg_sale_price, 10000000)


if __name__ == "__main__":
    unittest.main()
