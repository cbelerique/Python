# TSM cross-realm deal finder

Every hour this scans every US auction house using TradeSkillMaster's free public pricing data
(<https://tradeskillmaster.com/public-data>, no API key). It sends you a list of items to
**buy cheap on one realm and resell fast on Illidan or another full realm**.

## The rules it applies

| Rule | Default (`config.json`) |
| --- | --- |
| Discount | buy price ≤ **30% of what you net after the 5% AH cut** (`min_discount: 0.70`) |
| Max buy | **1,000g** per item (`max_buy_gold`) |
| No commodities | only realm (non-commodity) auction data is read; anything on TSM's commodity list is dropped |
| Sell where | `sell_realms`, in priority order: **Illidan first**, then other full realms (Area 52, Stormrage, Tichondrius, Mal'Ganis, Zul'jin, Moon Guard, Sargeras, Thrall, …) |
| Speed over revenue | resale price = the **lowest** of the region's average *sale* price, the region market value, and the sell realm's current cheapest listing minus 1%. Items must sell regularly: sale rate ≥ 5% and ≥ 0.25 sold/day/realm |
| Cross-realm first | cross-realm deals rank above same-realm flips. Then Illidan beats other full realms, then the fastest seller ranks first |
| Min profit | 25g after fees (`min_profit_gold`) |
| No repeat pings | a deal (same item, realm and price) isn't resent for 6 hours (`dedupe_hours`) |
| Variant guard | resale price is capped at the median lowest listing across the full realms. Deals that are equally cheap on 2+ other full realms get a ⚠ and rank lower |

Battle pets are included (`include_pets`). They are usually the best cross-realm items.

## What a message looks like

```
1. Some Pet
  BUY on Kil'jaeden for 120g → SELL on Illidan at 900g
  Net after 5% cut 855g · profit 735g · 86% under net · sells ~1.20/day, sale rate 18.0%
  Also cheap on: Dalaran 150g
```

Each report ends with **TSM group-import strings** for each buy realm (`i:123,p:456`), so you can
import them as a TSM group and shop them directly.

## Setup (once)

1. **Discord webhook.** In Discord: Server Settings → Integrations → Webhooks → New Webhook → copy URL.
2. **GitHub secret.** In this repo: Settings → Secrets and variables → Actions → New repository secret,
   with name `DISCORD_WEBHOOK_URL` and the URL as its value.
3. **Turn it on.** The workflow `.github/workflows/tsm-deals.yml` runs at minute 17 of every hour.
   GitHub only runs scheduled workflows from the **default branch**, so merge this into `main`.
   Then go to Actions → *TSM deal scan* → *Run workflow* to test it right away. Every run also
   writes the report to its Actions run summary, even without Discord.

## Run locally

```bash
python -m tsm_deals --dry-run          # print the report only
DISCORD_WEBHOOK_URL=... python -m tsm_deals
python -m unittest discover -s tests   # tests
```

Python 3.10+ and the standard library only.

## Tuning

Everything lives in `config.json`:

- `sell_realms`: realm slugs in priority order. The first one gets top priority. Connected realms
  share one auction house, so listing either member works.
- `source_realms`: leave empty to buy anywhere. To only buy on realms where you have characters,
  list them, e.g. `["kiljaeden", "dalaran"]`.
- `min_sale_rate` / `min_sold_per_day`: raise them for faster sales but fewer deals.
- `include_same_realm`: set to `false` for cross-realm only.
- `max_data_age_hours`: skip auction houses whose TSM snapshot is older than this.
- `max_downloads_per_run` (60): see the rate-limit section below.

## Rate limits and rotation

TSM's public file host rate-limits heavy downloaders. After a burst of a few hundred files it
returned `429` with `Retry-After` of about 30 minutes. So each run:

- reuses cached snapshots (kept between runs in the GitHub Actions cache, about 20 MB),
- re-downloads at most `max_downloads_per_run` files: region stats every 6h, the 18 full realms
  every 2h, and the other auction houses, oldest first, with whatever budget is left. All 83 US
  auction houses are refreshed about every 3 hours, which matches how often TSM updates them,
- stops downloading immediately on a long `Retry-After`. It reports from the cache and retries
  next hour.

`data/us_realms.json` maps all 246 US realm slugs into 83 connected-realm auction houses. It was
built by downloading every realm's file and grouping identical ones. If Blizzard merges realms,
regenerate it.

## Things to know

- **The data is hours old.** TSM's public realm files refresh every few hours, and the region
  sales stats about once a day. Cheap listings get bought, so check before you buy.
- **Gear is priced by base item ID.** Different item levels, crafted-quality ranks and variants
  share one price, so a "deal" on a weapon, armor piece or profession tool may be a lower-quality
  copy. The variant guard catches many of these, not all. Check it in-game. Pets, mounts, toys
  and recipes don't have this problem.
- **Moving items between realms:** use the Warband bank (BoE or unbound items and caged pets
  only). You need a character on the buy realm and one on the sell realm.
- **Realm population** isn't in TSM's data. The "full" realm list is a config you maintain.
- Unofficial personal tool; it doesn't automate anything in the game.
