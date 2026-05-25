"""Read-only check for stale open Bybit linear positions.

Pulls current open positions from Bybit, matches them against scanner signals
in selected_signals.csv / signals_log.csv, and classifies their age. Does NOT
place, cancel, or close any orders.
"""

import argparse
import json
import os
import re
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

    If no candidate exists at or before position_dt (or position_dt is
    missing / unreliable), fall back to the most recent signal overall for
    that symbol+side. Returning the latest candidate even when it is newer
    than position_dt is intentional: Bybit's position createdTime is often
    an old technical timestamp for the position slot, not the real open
    time, so a strict position_dt cutoff would miss the actual signal.
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


AUTH_ERROR_PATTERNS = [
    (re.compile(r"\bErrCode:\s*10004\b", re.IGNORECASE), "10004 signature error"),
    (re.compile(r"\berror sign\b", re.IGNORECASE), "signature error"),
    (re.compile(r"\bsignature\b", re.IGNORECASE), "signature error"),
    (re.compile(r"\bErrCode:\s*10003\b", re.IGNORECASE), "10003 invalid api key"),
    (re.compile(r"\binvalid api[- ]?key\b", re.IGNORECASE), "invalid api key"),
    (re.compile(r"\bErrCode:\s*10005\b", re.IGNORECASE), "10005 permission denied"),
    (re.compile(r"\bpermission denied\b", re.IGNORECASE), "permission denied"),
    (re.compile(r"\bErrCode:\s*10002\b", re.IGNORECASE), "10002 timestamp/expired"),
    (re.compile(r"\bexpired\b", re.IGNORECASE), "expired timestamp"),
    (re.compile(r"\bunauthorized\b", re.IGNORECASE), "unauthorized"),
]


def classify_error(message):
    """Return short tag for known auth/signature issues, or None."""
    if not message:
        return None
    for pattern, tag in AUTH_ERROR_PATTERNS:
        if pattern.search(message):
            return tag
    return None


def print_auth_troubleshooting():
    """Print bilingual actionable hints for ErrCode 10004 / auth failures."""
    print("", file=sys.stderr)
    print("[hint] Bybit auth/signature failure (ErrCode 10004 и подобные). Проверьте:",
          file=sys.stderr)
    print("  - BYBIT_API_KEY и BYBIT_API_SECRET соответствуют одной и той же паре "
          "(key/secret mismatch).",
          file=sys.stderr)
    print("  - Ключ создан как System-generated (HMAC), а не Self-generated (RSA). "
          "pybit ждёт HMAC.",
          file=sys.stderr)
    print("  - В .env нет скрытых пробелов, кавычек, переносов строк или BOM "
          "(hidden spaces/quotes/newlines).",
          file=sys.stderr)
    print("  - BYBIT_TESTNET соответствует среде, где создан ключ "
          "(testnet vs mainnet).",
          file=sys.stderr)
    print("  - Ключ не удалён, не истёк и не отозван на Bybit.",
          file=sys.stderr)
    print("  - У ключа включены права на чтение Derivatives/Unified Trading "
          "(read positions).",
          file=sys.stderr)
    print("  - Системное время не уехало (если используется свой timestamp).",
          file=sys.stderr)
    print("  Run with --debug-auth-env для безопасной диагностики (без секретов).",
          file=sys.stderr)


def fetch_open_positions(session):
    """Fetch all open linear positions across settle coins.

    Returns (positions, status_by_settle) where status_by_settle maps each
    settle coin to a dict {"ok": bool, "error": str|None, "tag": str|None}.
    """
    positions = []
    seen_keys = set()
    status_by_settle = {}

    settle_coins = ["USDT", "USDC"]
    for settle in settle_coins:
        cursor = ""
        settle_ok = False
        settle_error = None
        settle_tag = None
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
                msg = str(exc)
                settle_error = msg
                settle_tag = classify_error(msg)
                print(f"[warn] get_positions({settle}) failed: {msg}", file=sys.stderr)
                break

            ret_code = response.get("retCode")
            ret_msg = response.get("retMsg") or ""
            if ret_code not in (0, None):
                combined = f"retCode={ret_code} retMsg={ret_msg}"
                settle_error = combined
                settle_tag = classify_error(combined) or classify_error(ret_msg)
                print(f"[warn] get_positions({settle}) returned error: {combined}",
                      file=sys.stderr)
                break

            settle_ok = True
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

        status_by_settle[settle] = {
            "ok": settle_ok,
            "error": settle_error,
            "tag": settle_tag,
        }

    return positions, status_by_settle


def mask_value(value):
    """Return masked representation: length + prefix/suffix, never the full value."""
    if value is None:
        return "<missing>"
    if value == "":
        return "<empty>"
    length = len(value)
    if length <= 8:
        return f"len={length} value=<too short to show prefix>"
    return f"len={length} prefix={value[:4]}... suffix=...{value[-2:]}"


def looks_quoted(value):
    if not value:
        return False
    if len(value) < 2:
        return False
    first, last = value[0], value[-1]
    return (first == last) and first in ('"', "'")


def has_whitespace_issues(value):
    if value is None:
        return False
    return value != value.strip() or "\n" in value or "\r" in value


def print_auth_env_debug():
    """Print masked diagnostics about BYBIT_* env vars. Never prints full values."""
    raw_key = os.environ.get("BYBIT_API_KEY")
    raw_secret = os.environ.get("BYBIT_API_SECRET")
    raw_testnet = os.environ.get("BYBIT_TESTNET")

    print("[debug-auth-env] Masked diagnostics (no secrets printed):",
          file=sys.stderr)
    print(f"  BYBIT_API_KEY: present={raw_key is not None}", file=sys.stderr)
    if raw_key is not None:
        print(f"    {mask_value(raw_key)}", file=sys.stderr)
        print(f"    looks_quoted={looks_quoted(raw_key)} "
              f"whitespace_issues={has_whitespace_issues(raw_key)}",
              file=sys.stderr)
    print(f"  BYBIT_API_SECRET: present={raw_secret is not None}", file=sys.stderr)
    if raw_secret is not None:
        length = len(raw_secret)
        print(f"    len={length}", file=sys.stderr)
        print(f"    looks_quoted={looks_quoted(raw_secret)} "
              f"whitespace_issues={has_whitespace_issues(raw_secret)}",
              file=sys.stderr)
    testnet_norm = (raw_testnet or "").strip().lower()
    print(f"  BYBIT_TESTNET: raw_present={raw_testnet is not None} "
          f"normalized={'true' if testnet_norm == 'true' else 'false'}",
          file=sys.stderr)


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


POSITION_TS_TOLERANCE_HOURS = 12.0


def build_row(item, signals_df, watch_days, review_days, time_stop_days, now,
              prefer_signal_age=False):
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
    tolerance_seconds = POSITION_TS_TOLERANCE_HOURS * 3600.0

    if prefer_signal_age and signal_created_dt is not None:
        age_days = (now - signal_created_dt).total_seconds() / 86400.0
        age_basis = "signal_created_at_forced"
    elif position_dt is not None and signal_created_dt is not None:
        delta_seconds = (signal_created_dt - position_dt).total_seconds()
        if delta_seconds > tolerance_seconds:
            age_days = (now - signal_created_dt).total_seconds() / 86400.0
            age_basis = "signal_created_at_position_ts_unreliable"
        else:
            age_days = (now - position_dt).total_seconds() / 86400.0
            age_basis = "position_created_at"
    elif position_dt is not None:
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
    parser.add_argument("--debug-auth-env", action="store_true",
                        help="Print masked diagnostics about BYBIT_* env vars "
                             "(no secrets shown).")
    parser.add_argument("--prefer-signal-age", action="store_true",
                        help="When a matching scanner signal is found, always "
                             "use its created_at as the age basis instead of "
                             "the Bybit position createdTime. Useful when "
                             "Bybit returns an old technical timestamp for "
                             "the position slot.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.watch_days > args.review_days or args.review_days > args.time_stop_days:
        print("[warn] thresholds should satisfy watch <= review <= time-stop", file=sys.stderr)

    load_env()

    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    testnet = os.getenv("BYBIT_TESTNET", "false").strip().lower() == "true"

    if args.debug_auth_env:
        print_auth_env_debug()

    if not api_key or not api_secret:
        print("Missing BYBIT_API_KEY or BYBIT_API_SECRET in .env", file=sys.stderr)
        sys.exit(1)

    session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)

    signals_df = load_signals()
    items, settle_status = fetch_open_positions(session)

    ok_settles = [s for s, st in settle_status.items() if st["ok"]]
    failed_settles = [s for s, st in settle_status.items() if not st["ok"]]

    if not ok_settles and failed_settles:
        print("", file=sys.stderr)
        print(f"[error] All Bybit get_positions calls failed "
              f"({', '.join(failed_settles)}). Open positions are UNKNOWN, "
              f"not zero.",
              file=sys.stderr)
        auth_failure = False
        for settle in failed_settles:
            st = settle_status[settle]
            tag = st.get("tag")
            err = st.get("error") or ""
            print(f"  - {settle}: {err}"
                  + (f"  [{tag}]" if tag else ""),
                  file=sys.stderr)
            if tag:
                auth_failure = True
        if auth_failure:
            print_auth_troubleshooting()
            if not args.debug_auth_env:
                print("  Tip: rerun with --debug-auth-env to inspect env vars "
                      "safely.",
                      file=sys.stderr)
        sys.exit(2)

    if failed_settles:
        print(f"[warn] Partial failure: settle coins failed = "
              f"{', '.join(failed_settles)}; succeeded = "
              f"{', '.join(ok_settles)}. Results may be incomplete.",
              file=sys.stderr)

    now = datetime.now(timezone.utc)
    rows = []
    for item in items:
        row = build_row(item, signals_df, args.watch_days, args.review_days,
                        args.time_stop_days, now,
                        prefer_signal_age=args.prefer_signal_age)
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
