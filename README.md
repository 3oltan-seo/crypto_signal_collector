# Bybit futures scanner system

## Workflow

1. Run:

```bash
python3 scanner.py
```

2. Open `scanner_results.xlsx`.
3. Put `yes` in `selected` for signals where you placed limit orders.
4. Optionally write a note in `manual_comment`.
5. Save the Excel file.
6. Run:

```bash
python3 sync_selected_signals.py
```

7. Between opening and closing, you can batch-check for stale swing positions at any time. This is read-only and never closes anything:

```bash
python3 check_stale_positions.py
```

It prints a table of open Bybit linear positions, classifies each by age (`OK` / `WATCH` / `REVIEW` / `TIME_STOP_REVIEW` / `UNKNOWN_AGE`), matches them against local scanner signals, and writes `stale_positions.csv`. Useful CLI options: `--only-stale`, `--watch-days`, `--review-days`, `--time-stop-days`, `--no-save`, `--csv-output PATH`, `--json`.

8. After trades close on Bybit, run:

```bash
python3 sync_bybit_trades.py
```

9. If you already have old rows in `trades_log.csv`, run once:

```bash
python3 enrich_existing_trades.py
```

10. Build analytical trade episodes:

```bash
python3 build_trade_episodes.py
```

This creates `trade_episodes.csv`, where one row means one scanner trading idea, not one Bybit closing execution.

## Files

- `scanner.py` — creates current long/short signals, appends all signals to `signals_log.csv`, and writes `scanner_results.xlsx`.
- `sync_selected_signals.py` — saves rows marked `selected=yes` into `selected_signals.csv`.
- `check_stale_positions.py` — read-only check of current open Bybit linear positions; classifies each by age and matches against scanner signals. Never closes orders. Saves `stale_positions.csv`.
- `sync_bybit_trades.py` — pulls closed PnL from Bybit, matches trades to signals, and enriches `trades_log.csv`.
- `enrich_existing_trades.py` — enriches already existing `trades_log.csv` from local signal logs.
- `build_trade_episodes.py` — groups raw `trades_log.csv` rows into analytical trade episodes in `trade_episodes.csv`.

## Important behavior

- `scanner_results.xlsx` is a temporary current-output file and can be overwritten.
- Before overwriting, `scanner.py` tries to archive selected rows from the previous `scanner_results.xlsx`.
- `signals_log.csv`, `selected_signals.csv`, and `trades_log.csv` are persistent logs.
- `trade_episodes.csv` is an analytical derived file. It can be regenerated from `trades_log.csv`, `signals_log.csv`, and `selected_signals.csv`.
- Matching is strict by default: a signal must exist before the trade entry time.
- `ALLOW_WEAK_MATCH_AFTER_ENTRY = False` prevents accidental learning from signals that appeared after a trade was already opened.
- Long and short signals are both shown in every BTC regime.
- BTC regime changes scoring, not visibility: bearish BTC penalizes longs and boosts shorts; supportive BTC boosts longs and penalizes shorts.
- `scanner_results.xlsx` shows separate top candidates per side via `TOP_N_PER_SIDE`.
- Bybit closed `Sell` is treated as closing a long; closed `Buy` is treated as closing a short.
- `sync_bybit_trades.py` also checks Bybit order/execution history for liquidation/ADL/admin-close markers. If found, `result_type` becomes `LIQUIDATION` instead of `SL_or_near_SL`.
- Manual liquidation corrections are preserved. If a row already has `result_type=LIQUIDATION`, `is_liquidation=TRUE`, `liquidation_flag_source=manual_user_correction`, or a liquidation note in `comment`, enrichment and sync will not downgrade it back to `BE` or `SL_or_near_SL`.
- `build_trade_episodes.py` keeps raw executions separate from analytical results:
  - `PARTIAL_TP` means Bybit closed part of the position by partial take-profit.
  - `PROTECTED_BE` means the stop was likely moved to breakeven or small profit after the trade moved in your favor.
  - `TP1_THEN_PROTECTED_BE` means the idea first took partial profit and then the remainder closed around protected breakeven.
  - `LIQUIDATION` always overrides mathematical BE/SL classification when liquidation markers are present.
- The scanner includes a Python adaptation of the Smart Money Concepts Pine script:
  - confirmed internal/swing BOS and CHoCH;
  - premium/discount/equilibrium zone;
  - recent bullish/bearish fair value gaps;
  - SMC score columns: `smc_score_delta`, `smc_bias`, `smc_event`, `smc_zone`, `smc_reason`.

This is not a 1:1 visual TradingView port. Drawing-only objects such as boxes, lines, and labels are converted into numeric scanner factors.

## Market-regime context

The scanner prints a market-regime summary in the terminal at the start of each run. These fields do **not** change `score` yet and are no longer repeated in every row of `scanner_results.xlsx`.

The same snapshot is saved once per run to `market_regime_log.csv`:

- `created_at`
- `market_regime`
- `market_bias`
- `market_summary`
- `fear_greed_value`
- `fear_greed_status`
- `altseason_index`
- `altseason_status`
- `btc_dominance`
- `eth_dominance`
- `cycle_mvrv_z`
- `cycle_puell`
- `cycle_mayer`
- `cycle_cbbi`
- `market_reason`

Data sources:

- Fear & Greed: public Alternative.me API.
- BTC/ETH dominance: public CoinGecko global API.
- CMC cycle indicators: best-effort scrape of CoinMarketCap cycle page. If unavailable, fields stay blank.

Optional manual override:

1. Copy `market_regime_manual.example.json` to `market_regime_manual.json`.
2. Fill any values manually.
3. `scanner.py` will use your manual values when available.

Terminal output includes a human-readable market summary, for example:

```text
Market summary: Рынок выглядит скорее risk-off: short-сигналы приоритетнее, а long по альтам требуют сильного подтверждения и меньшего размера.
```

## Requirements

```bash
python3 -m pip install pybit pandas python-dotenv tabulate openpyxl
```

Your `.env` file should contain:

```env
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
BYBIT_TESTNET=false
```

Do not share `.env` or API secrets.

## Troubleshooting

### Bybit `ErrCode: 10004` "Error sign, please check your signature generation algorithm"

`check_stale_positions.py` and other Bybit-calling scripts will print this warning when authentication fails. If **all** `get_positions` calls fail, the script now exits non-zero instead of misleadingly printing `Open positions found: 0`.

Likely causes:

- **Key/secret mismatch** — `BYBIT_API_KEY` and `BYBIT_API_SECRET` are not from the same key pair (e.g. secret copied from a different key).
- **RSA key used instead of HMAC** — `pybit` expects a **System-generated (HMAC)** key. If you created a **Self-generated (RSA)** key on Bybit, signing will fail. Re-create as System-generated.
- **Hidden characters in `.env`** — stray spaces, surrounding quotes (`"..."` / `'...'`), trailing newlines, or a BOM in the API key / secret value. Re-paste cleanly without quotes.
- **Wrong testnet flag** — `BYBIT_TESTNET=true` while the key was created on mainnet, or vice versa. The flag must match the environment where the key was issued.
- **Old / deleted / expired key** — keys can be deleted or auto-expire on Bybit; the secret then no longer validates. Issue a new key.
- **Insufficient permissions** — the key exists but does not have read access for **Derivatives / Unified Trading positions**. Enable the right permissions on Bybit.

Safe diagnostics (no secrets printed):

```bash
python3 check_stale_positions.py --debug-auth-env
```

This prints whether `BYBIT_API_KEY` / `BYBIT_API_SECRET` are present, their lengths, whether they look wrapped in quotes, whether they contain trailing whitespace or newlines, and the normalized `BYBIT_TESTNET` value. The full key and secret are **never** printed.
