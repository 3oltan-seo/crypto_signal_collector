"""Read-only check for stale open Bybit linear positions.

Pulls current open positions from Bybit, matches them against scanner signals
in selected_signals.csv / signals_log.csv, and classifies their age. Does NOT
place, cancel, or close any orders.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from pybit.unified_trading import HTTP


SELECTED_SIGNALS_LOG = "selected_signals.csv"
SIGNALS_LOG = "signals_log.csv"
DEFAULT_CSV_OUTPUT = "stale_positions.csv"


OUTPUT_COLUMNS = [
    "symbol",
    "side",
    "size",
    "entry_price",
    "mark_price",
    "unrealised_pnl",
    "leverage",
    "position_created_at",
    "position_age_days",
    "age_basis",
    "matched_signal_id",
    "signal_created_at",
    "status",
    "action_note",
]


def fallback_load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_env():
    if load_dotenv is not None:
        load_dotenv()
    else:
        fallback_load_dotenv()


def to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except Exception:
            return None
        return f
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def ms_to_dt(value):
    f = to_float(value)
    if f is None or f <= 0:
        return None
    try:
        return datetime.fromtimestamp(f / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def parse_dt(value):
    if value in (None, "", "nan"):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def fmt_dt(dt):
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def load_csv_safe(path):
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:
        print(f"[warn] could not read {path}: {exc}", file=sys.stderr)
        return pd.DataFrame()


def load_signals():
    """Load signals from selected_signals.csv first, then signals_log.csv.

    Returns a DataFrame deduplicated on signal_id with a created_at_dt column.
    """
    frames = []
    selected = load_csv_safe(SELECTED_SIGNALS_LOG)
    if not selected.empty:
        selected["__source"] = "selected"
        frames.append(selected)
    log = load_csv_safe(SIGNALS_LOG)
    if not log.empty:
        log["__source"] = "log"
        frames.append(log)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)

    if "signal_id" not in df.columns:
        return pd.DataFrame()

    if "symbol" not in df.columns or "side" not in df.columns:
        return pd.DataFrame()

    df = df.drop_duplicates(subset=["signal_id"], keep="first")

    if "created_at" in df.columns:
        df["created_at_dt"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
    else:
        df["created_at_dt"] = pd.NaT

    df["symbol"] = df["symbol"].astype(str)
    df["side"] = df["side"].astype(str).str.lower()
    return df


def find_matching_signal(symbol, side, position_dt, signals_df):
    """Pick the latest signal before position_dt for matching symbol+side.

    If no position_dt is known, return the most recent signal overall for
    that symbol+side.
    """
    if signals_df.empty:
        return None

    df = signals_df[
        (signals_df["symbol"].astype(str) == str(symbol))
        & (signals_df["side"].astype(str).str.lower() == side.lower())
    ]
    if df.empty:
        return None

    df = df.dropna(subset=["created_at_dt"])
    if df.empty:
        return None

    if position_dt is not None:
        before = df[df["created_at_dt"] <= pd.Timestamp(position_dt)]
        if not before.empty:
            return before.sort_values("created_at_dt").iloc[-1]

    return df.sort_values("created_at_dt").iloc[-1]


def bybit_side_to_position_side(bybit_side):
    s = str(bybit_side).strip().lower()
    if s == "buy":
        return "long"
    if s == "sell":
        return "short"
    return s or ""


def fetch_open_positions(session):
    """Fetch all open linear positions across settle coins. Returns list of dicts."""
    positions = []
    seen_keys = set()

    settle_coins = ["USDT", "USDC"]
    for settle in settle_coins:
        cursor = ""
        while True:
            kwargs = {
                "category": "linear",
                "settleCoin": settle,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor
            try:
                response = session.get_positions(**kwargs)
            except Exception as exc:
                print(f"[warn] get_positions({settle}) failed: {exc}", file=sys.stderr)
                break

            result = response.get("result") or {}
            items = result.get("list") or []
            for item in items:
                key = (
                    item.get("symbol", ""),
                    item.get("side", ""),
                    item.get("positionIdx", ""),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                positions.append(item)

            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break

    return positions


def extract_position_dt(item):
    """Try several Bybit fields for position open time. Returns (dt, basis_hint)."""
    for field in ("createdTime", "createdAt", "updatedTime"):
        value = item.get(field)
        dt = ms_to_dt(value)
        if dt is not None:
            return dt, field
    return None, None


def classify_status(age_days, watch_days, review_days, time_stop_days):
    if age_days is None:
        return "UNKNOWN_AGE", "Add a manual note: no reliable age available."
    if age_days < watch_days:
        return "OK", "Within plan, hold."
    if age_days < review_days:
        return "WATCH", "Approaching review window — keep an eye on price action."
    if age_days < time_stop_days:
        return "REVIEW", "Re-check the thesis: still valid? Tighten stop if not."
    return "TIME_STOP_REVIEW", "Past time-stop window — review manually for close."


def build_row(item, signals_df, watch_days, review_days, time_stop_days, now):
    symbol = item.get("symbol", "")
    bybit_side = item.get("side", "")
    side = bybit_side_to_position_side(bybit_side)

    size = to_float(item.get("size"))
    if size is None or size == 0:
        return None

    entry_price = to_float(item.get("avgPrice")) or to_float(item.get("entryPrice"))
    mark_price = to_float(item.get("markPrice"))
    unrealised_pnl = to_float(item.get("unrealisedPnl"))
    leverage = to_float(item.get("leverage"))

    position_dt, _basis_hint = extract_position_dt(item)

    signal = find_matching_signal(symbol, side, position_dt, signals_df)

    signal_id = ""
    signal_created_dt = None
    if signal is not None:
        signal_id = str(signal.get("signal_id", "") or "")
        sdt = signal.get("created_at_dt")
        if sdt is not None and not pd.isna(sdt):
            signal_created_dt = sdt.to_pydatetime() if hasattr(sdt, "to_pydatetime") else sdt

    age_days = None
    age_basis = ""
    if position_dt is not None:
        age_days = (now - position_dt).total_seconds() / 86400.0
        age_basis = "position_created_at"
    elif signal_created_dt is not None:
        age_days = (now - signal_created_dt).total_seconds() / 86400.0
        age_basis = "signal_created_at"
    else:
        age_basis = "none"

    status, action_note = classify_status(age_days, watch_days, review_days, time_stop_days)

    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "entry_price": entry_price if entry_price is not None else "",
        "mark_price": mark_price if mark_price is not None else "",
        "unrealised_pnl": unrealised_pnl if unrealised_pnl is not None else "",
        "leverage": leverage if leverage is not None else "",
        "position_created_at": fmt_dt(position_dt),
        "position_age_days": round(age_days, 2) if age_days is not None else "",
        "age_basis": age_basis,
        "matched_signal_id": signal_id,
        "signal_created_at": fmt_dt(signal_created_dt),
        "status": status,
        "action_note": action_note,
    }


def print_table(rows):
    if not rows:
        print("No open positions to show.")
        return

    try:
        from tabulate import tabulate
        compact = [
            {
                "symbol": r["symbol"],
                "side": r["side"],
                "size": r["size"],
                "entry": r["entry_price"],
                "mark": r["mark_price"],
                "uPnL": r["unrealised_pnl"],
                "lev": r["leverage"],
                "age_d": r["position_age_days"],
                "basis": r["age_basis"],
                "signal_id": r["matched_signal_id"],
                "status": r["status"],
                "note": r["action_note"],
            }
            for r in rows
        ]
        print(tabulate(compact, headers="keys", tablefmt="github", showindex=False))
        return
    except ImportError:
        pass

    headers = [
        "symbol", "side", "size", "entry", "mark", "uPnL", "lev",
        "age_d", "basis", "signal_id", "status", "note",
    ]
    widths = {h: len(h) for h in headers}
    text_rows = []
    for r in rows:
        text = {
            "symbol": str(r["symbol"]),
            "side": str(r["side"]),
            "size": str(r["size"]),
            "entry": str(r["entry_price"]),
            "mark": str(r["mark_price"]),
            "uPnL": str(r["unrealised_pnl"]),
            "lev": str(r["leverage"]),
            "age_d": str(r["position_age_days"]),
            "basis": str(r["age_basis"]),
            "signal_id": str(r["matched_signal_id"]),
            "status": str(r["status"]),
            "note": str(r["action_note"]),
        }
        for h in headers:
            widths[h] = max(widths[h], len(text[h]))
        text_rows.append(text)

    header_line = "  ".join(h.ljust(widths[h]) for h in headers)
    print(header_line)
    print("-" * len(header_line))
    for tr in text_rows:
        print("  ".join(tr[h].ljust(widths[h]) for h in headers))


STALE_STATUSES = {"WATCH", "REVIEW", "TIME_STOP_REVIEW", "UNKNOWN_AGE"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only stale-position check against Bybit linear positions.",
    )
    parser.add_argument("--watch-days", type=float, default=3.0,
                        help="Age in days from which status becomes WATCH (default 3).")
    parser.add_argument("--review-days", type=float, default=5.0,
                        help="Age in days from which status becomes REVIEW (default 5).")
    parser.add_argument("--time-stop-days", type=float, default=7.0,
                        help="Age in days from which status becomes TIME_STOP_REVIEW (default 7).")
    parser.add_argument("--csv-output", default=DEFAULT_CSV_OUTPUT,
                        help=f"CSV path to save the report (default {DEFAULT_CSV_OUTPUT}).")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not write the CSV report.")
    parser.add_argument("--only-stale", action="store_true",
                        help="Only show WATCH/REVIEW/TIME_STOP_REVIEW/UNKNOWN_AGE rows.")
    parser.add_argument("--json", action="store_true",
                        help="Also print rows as JSON to stdout.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.watch_days > args.review_days or args.review_days > args.time_stop_days:
        print("[warn] thresholds should satisfy watch <= review <= time-stop", file=sys.stderr)

    load_env()

    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    testnet = os.getenv("BYBIT_TESTNET", "false").strip().lower() == "true"

    if not api_key or not api_secret:
        print("Missing BYBIT_API_KEY or BYBIT_API_SECRET in .env", file=sys.stderr)
        sys.exit(1)

    session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)

    signals_df = load_signals()
    items = fetch_open_positions(session)

    now = datetime.now(timezone.utc)
    rows = []
    for item in items:
        row = build_row(item, signals_df, args.watch_days, args.review_days,
                        args.time_stop_days, now)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: (r["position_age_days"] if isinstance(r["position_age_days"], (int, float)) else -1),
              reverse=True)

    display_rows = [r for r in rows if r["status"] in STALE_STATUSES] if args.only_stale else rows

    print(f"Open positions found: {len(rows)}")
    if args.only_stale:
        print(f"Showing stale rows only: {len(display_rows)} of {len(rows)}")
    print("")

    print_table(display_rows)

    if args.json:
        print("")
        print(json.dumps(display_rows, default=str, ensure_ascii=False, indent=2))

    if not args.no_save and rows:
        df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
        try:
            df.to_csv(args.csv_output, index=False, encoding="utf-8-sig")
            print("")
            print(f"Saved report: {args.csv_output} ({len(df)} rows)")
        except Exception as exc:
            print(f"[warn] could not write {args.csv_output}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
