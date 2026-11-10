# WoW cross-realm deal finder

Every hour this scans every US auction house. It sends you a Telegram message listing items to
**buy cheap on one realm and resell fast on Illidan or another full realm**.

- **Realm prices** come from Blizzard's auction API, which updates hourly.
- **What actually sells** (average sale price, sale rate, sales per day) comes from
  TradeSkillMaster's free region data (<https://tradeskillmaster.com/public-data>).

## The rules it applies

| Rule | Default (`config.json`) |
| --- | --- |
| Discount | buy price ≤ **30% of what you net after the 5% AH cut** (`min_discount: 0.70`) |
| Max buy | **1,000g** per item (`max_buy_gold`) |
| No commodities | it only reads Blizzard's per-realm auctions, which never include commodities. TSM's commodity list is also excluded |
| No gear | weapons, armor and profession equipment are dropped using Blizzard's item class (`exclude_item_classes: [2, 4, 19]`). Unknown items are dropped too |
| Sell where | Illidan first, then the realms in `sell_realms`, then **any realm Blizzard currently reports as FULL** (`include_full_realms`) |
| Speed over revenue | resale price = the **lowest** of the region's average *sale* price, the region market value, the sell realm's cheapest listing minus 1%, and the typical cheapest listing across full realms. Items must sell regularly: sale rate ≥ 5% and ≥ 0.25 sold/day/realm |
| Ranking | cross-realm before same-realm, then Illidan before other full realms, then fastest seller |
| Fresh data | realms whose Blizzard data is over 2h old are skipped (`max_data_age_hours`) |
| No repeat pings | the same deal (item, realm, price) isn't resent for 6h (`dedupe_hours`) |

Battle pets are included (`include_pets`).

## What a Telegram message looks like

```
1. Recipe: Some Feast  recipe
🛒 BUY Kil'jaeden / Spinebreaker for 120g
📈 SELL Illidan at 900g
💵 net 855g · profit 735g · 86% under net
⏱ ~1.20 sold/day/realm · sale rate 18% · 2 listed there
↪ also: Dalaran 150g
```

Each message ends with **TSM group-import strings** for each buy realm (`i:123,p:456`). Paste
them into TSM → Groups → Import to shop them.

## Setup (once, about 10 minutes)

1. **Blizzard API client (free).** Go to <https://develop.battle.net/access/clients> and choose
   *Create Client*. Any name works; leave the redirect URL empty. Copy the **Client ID** and
   **Client Secret**.
2. **Telegram bot.**
   - In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Copy the **bot token**.
   - Open a chat with your new bot and send it any message (bots can't message you first).
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
     `"chat":{"id": ...}`. That's your **chat ID**.
3. **GitHub secrets.** In this repo, go to Settings → Secrets and variables → Actions → *New
   repository secret* and add `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`,
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Turn it on.** GitHub only runs scheduled workflows from the default branch, so merge into
   `master`. Then go to Actions → *TSM deal scan* → *Run workflow* to test right away. It then runs
   at minute 17 of every hour. Every run also writes its report to the Actions run summary.

## Run locally

```bash
export BLIZZARD_CLIENT_ID=... BLIZZARD_CLIENT_SECRET=...
python -m tsm_deals --dry-run          # print only
python -m unittest discover -s tests   # tests (includes a full fake-server run)
```

Python 3.10+, standard library only.

## Tuning (`config.json`)

- `sell_realms`: realm slugs in priority order. The first one (Illidan) gets top priority.
- `include_full_realms`: also sell on whatever Blizzard marks FULL right now.
- `source_realms`: empty means buy anywhere. To only buy where you have characters, list them,
  e.g. `["kiljaeden", "dalaran"]`.
- `min_sale_rate` / `min_sold_per_day`: raise them for faster sales but fewer deals.
- `include_same_realm`: set to `false` for cross-realm only.
- `exclude_item_classes`: Blizzard item class IDs to skip.

## How a run works

1. The connected-realm list and population come from Blizzard (refreshed daily, cached).
2. TSM region stats are refreshed every 6h (TSM updates them about daily).
3. Auctions are fetched for every connected realm with `If-Modified-Since`, so a realm that
   hasn't changed costs nothing. Snapshots are kept in the GitHub Actions cache.
4. Deals are filtered by item class and deduplicated, then sent.

## Things to know

- **Speed is relative.** Blizzard's data is up to about an hour old when you read it, so
  very cheap listings may already be gone. Check before you buy.
- **Moving items:** you need a character on the buy realm and one on the sell realm. Move items
  through the Warband bank (unbound items and caged pets only).
- Unofficial personal tool; it doesn't automate anything in the game.
