#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build trade_episodes.csv from raw Bybit closed-trade rows.

Purpose
-------
trades_log.csv is a raw execution/closed-PnL log: one row can be a partial TP,
stop-loss close, BE stop, liquidation, or another closing event.

trade_episodes.csv is an analytical journal: one row = one trading idea/signal.
This is the file that should be used later for learning from scanner signals.

Typical usage
-------------
    python3 build_trade_episodes.py

Optional:
    python3 build_trade_episodes.py --trades-log trades_log.csv --output trade_episodes.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_TRADES_LOG = "trades_log.csv"
DEFAULT_SIGNALS_LOG = "signals_log.csv"
DEFAULT_SELECTED_SIGNALS = "selected_signals.csv"
DEFAULT_OUTPUT = "trade_episodes.csv"

DEFAULT_BE_R_THRESHOLD = 0.20


SIGNAL_CONTEXT_COLUMNS = [
    "score",
    "rr1",
    "rr2",
    "funding_pct",
    "btc_regime",
    "btc_score_delta",
    "oi_change_pct",
    "price_change_oi_window_pct",
    "oi_score_delta",
    "oi_signal",
    "smc_score_delta",
    "smc_bias",
    "smc_event",
    "smc_zone",
    "smc_reason",
    "market_regime",
    "fear_greed_value",
    "fear_greed_status",
    "altseason_index",
    "altseason_status",
    "btc_dominance",
    "eth_dominance",
    "cycle_mvrv_z",
    "cycle_puell",
    "cycle_mayer",
    "cycle_cbbi",
    "market_reason",
    "reason",
    "selected",
    "manual_comment",
    "entry",
    "turnover24h_m",
    "change24h_pct",
]


OUTPUT_COLUMNS = [
    "episode_id",
    "signal_id",
    "symbol",
    "side",
    "first_entry_at",
    "last_close_at",
    "duration_hours",
    "row_count",
    "trade_ids",
    "match_quality",
    "entry_price",
    "weighted_avg_exit_price",
    "total_qty",
    "total_closed_pnl",
    "stop",
    "tp1",
    "tp2",
    "episode_r_multiple",
    "final_result_type",
    "close_path",
    "has_partial_tp",
    "has_take_profit_close",
    "has_stop_loss_close",
    "has_protected_be",
    "has_liquidation",
    "liquidation_flag_source",
    "close_create_types",
    "close_exec_types",
    "close_order_types",
] + SIGNAL_CONTEXT_COLUMNS + [
    "source_rows_comment",
]


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        return [normalize_row(row) for row in reader]


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    return {
        (k or "").strip(): "" if v is None else str(v).strip()
        for k, v in row.items()
        if k is not None
    }


def write_csv_rows(path: Path, rows: List[Dict[str, object]], columns: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: format_value(row.get(col, "")) for col in columns})


def format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        # Keep enough precision for crypto prices, but avoid ugly binary tails.
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return str(value)


def first_non_empty(row: Dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name, "")
        if value not in ("", None):
            return str(value).strip()
    return ""


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    text = text.replace("\u00a0", "").replace(" ", "").replace("%", "")

    # Handle decimal comma from Russian/European CSV exports.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        # Assume commas are thousand separators.
        text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def parse_datetime(value: object) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да", "истина"}


def contains_any(text: str, needles: Iterable[str]) -> bool:
    low = text.lower()
    return any(needle in low for needle in needles)


def stable_short_hash(parts: Iterable[str]) -> str:
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:10]


def row_side(row: Dict[str, str]) -> str:
    side = first_non_empty(row, "position_side", "side")
    return side.lower()


def row_created_at(row: Dict[str, str]) -> Optional[datetime]:
    return parse_datetime(first_non_empty(row, "created_at", "createdTime", "created_time", "open_time"))


def row_closed_at(row: Dict[str, str]) -> Optional[datetime]:
    return parse_datetime(first_non_empty(row, "closed_at", "updatedTime", "closed_time", "exec_time"))


def row_sort_key(row: Dict[str, str]) -> Tuple[datetime, str]:
    dt = row_closed_at(row) or row_created_at(row) or datetime.min.replace(tzinfo=timezone.utc)
    return dt, first_non_empty(row, "trade_id", "order_id", "orderId")


def build_signal_maps(signals_rows: List[Dict[str, str]], selected_rows: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    signal_map: Dict[str, Dict[str, str]] = {}

    for source_rows in (signals_rows, selected_rows):
        for row in source_rows:
            signal_id = first_non_empty(row, "signal_id")
            if not signal_id:
                continue
            if signal_id not in signal_map:
                signal_map[signal_id] = {}
            for key, value in row.items():
                if value and not signal_map[signal_id].get(key):
                    signal_map[signal_id][key] = value

    return signal_map


def classify_raw_row(row: Dict[str, str], be_r_threshold: float = DEFAULT_BE_R_THRESHOLD) -> str:
    result_type = first_non_empty(row, "result_type")
    create_type = first_non_empty(row, "close_create_type", "createType")
    exec_type = first_non_empty(row, "close_exec_type", "execType")
    order_type = first_non_empty(row, "close_order_type", "orderType")
    comment = first_non_empty(row, "comment")
    liquidation_source = first_non_empty(row, "liquidation_flag_source")

    joined = " ".join([result_type, create_type, exec_type, order_type, comment, liquidation_source])

    if truthy(first_non_empty(row, "is_liquidation")):
        return "LIQUIDATION"
    if contains_any(joined, ["liquidation", "ликвидац", "adl", "bankruptcy"]):
        return "LIQUIDATION"

    if contains_any(create_type, ["partialtakeprofit", "partial_take_profit"]):
        return "PARTIAL_TP"
    if contains_any(result_type, ["partial_tp"]):
        return "PARTIAL_TP"

    if contains_any(create_type, ["takeprofit", "take_profit"]):
        return "TAKE_PROFIT"

    r_multiple = parse_float(first_non_empty(row, "r_multiple"))
    pnl = parse_float(first_non_empty(row, "closed_pnl"))

    if contains_any(create_type, ["stoploss", "stop_loss"]) or contains_any(result_type, ["sl_or_near_sl", "stop"]):
        if r_multiple is not None and abs(r_multiple) <= be_r_threshold:
            if pnl is None or pnl >= 0:
                return "PROTECTED_BE"
            return "BE_STOP"
        if r_multiple is not None and r_multiple <= -0.75:
            return "SL"
        return "STOP_CLOSE"

    if contains_any(result_type, ["liquidation"]):
        return "LIQUIDATION"
    if contains_any(result_type, ["be"]):
        return "BE"
    if contains_any(result_type, ["sl"]):
        return "SL"
    if contains_any(result_type, ["tp"]):
        return "TAKE_PROFIT"

    if r_multiple is not None:
        if abs(r_multiple) <= be_r_threshold:
            return "BE"
        if r_multiple >= 0.75:
            return "TAKE_PROFIT"
        if r_multiple <= -0.75:
            return "SL"
        if r_multiple > 0:
            return "PROFIT_CLOSE"
        return "LOSS_CLOSE"

    if pnl is not None:
        if pnl > 0:
            return "PROFIT_CLOSE"
        if pnl < 0:
            return "LOSS_CLOSE"

    return "UNKNOWN"


def episode_group_key(row: Dict[str, str]) -> str:
    signal_id = first_non_empty(row, "signal_id")
    if signal_id:
        return f"signal:{signal_id}"

    # Fallback for rows that cannot be matched to a scanner signal.
    symbol = first_non_empty(row, "symbol")
    side = row_side(row)
    created = row_created_at(row)
    created_bucket = created.strftime("%Y%m%d") if created else "unknown_date"
    entry = first_non_empty(row, "entry_price")
    return f"unmatched:{symbol}:{side}:{created_bucket}:{entry}"


def weighted_average(values_and_weights: Iterable[Tuple[Optional[float], Optional[float]]]) -> Optional[float]:
    numerator = 0.0
    denominator = 0.0
    for value, weight in values_and_weights:
        if value is None:
            continue
        w = 1.0 if weight is None or weight <= 0 else weight
        numerator += value * w
        denominator += w
    if denominator == 0:
        return None
    return numerator / denominator


def unique_join(values: Iterable[str], sep: str = " | ") -> str:
    result: List[str] = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return sep.join(result)


def final_episode_result(row_types: List[str], episode_r: Optional[float]) -> str:
    has_liq = "LIQUIDATION" in row_types
    has_partial = "PARTIAL_TP" in row_types
    has_tp = has_partial or "TAKE_PROFIT" in row_types
    has_protected_be = "PROTECTED_BE" in row_types
    has_be = has_protected_be or "BE" in row_types or "BE_STOP" in row_types
    has_sl = "SL" in row_types
    has_loss = "LOSS_CLOSE" in row_types
    has_profit = "PROFIT_CLOSE" in row_types or "TAKE_PROFIT" in row_types

    if has_liq:
        return "LIQUIDATION"
    if has_partial and has_protected_be:
        return "TP1_THEN_PROTECTED_BE"
    if has_partial and has_be:
        return "TP1_THEN_BE"
    if has_partial and has_sl:
        return "TP1_THEN_SL"
    if has_partial:
        return "PARTIAL_TP"
    if has_tp:
        return "TAKE_PROFIT"
    if has_protected_be:
        return "PROTECTED_BE"
    if has_be:
        return "BE"
    if has_sl:
        return "SL"
    if episode_r is not None:
        if abs(episode_r) <= DEFAULT_BE_R_THRESHOLD:
            return "BE"
        if episode_r > 0:
            return "PROFIT_CLOSE"
        return "LOSS_CLOSE"
    if has_profit:
        return "PROFIT_CLOSE"
    if has_loss:
        return "LOSS_CLOSE"
    return "UNKNOWN"


def enrich_from_signal_context(base: Dict[str, object], signal_context: Dict[str, str], source_rows: List[Dict[str, str]]) -> None:
    for col in SIGNAL_CONTEXT_COLUMNS:
        if base.get(col) not in ("", None):
            continue

        row_value = ""
        for row in source_rows:
            row_value = first_non_empty(row, col)
            if row_value:
                break

        if not row_value:
            row_value = signal_context.get(col, "")

        base[col] = row_value


def aggregate_episode(
    rows: List[Dict[str, str]],
    signal_map: Dict[str, Dict[str, str]],
    be_r_threshold: float,
) -> Dict[str, object]:
    rows = sorted(rows, key=row_sort_key)
    first = rows[0]

    signal_id = first_non_empty(first, "signal_id")
    symbol = first_non_empty(first, "symbol")
    side = row_side(first)
    trade_ids = [first_non_empty(row, "trade_id", "order_id", "orderId") for row in rows]

    if signal_id:
        episode_id = signal_id
    else:
        episode_id = f"UNMATCHED_{symbol}_{side}_{stable_short_hash(trade_ids)}"

    qty_values = [parse_float(first_non_empty(row, "qty", "closed_size", "size")) for row in rows]
    total_qty = sum(q for q in qty_values if q is not None)
    if total_qty == 0:
        total_qty = None

    total_pnl = sum(
        value for value in (parse_float(first_non_empty(row, "closed_pnl", "closedPnl")) for row in rows)
        if value is not None
    )

    entry_price = weighted_average(
        (
            (parse_float(first_non_empty(row, "entry_price", "avgEntryPrice")), parse_float(first_non_empty(row, "qty", "closed_size", "size")))
            for row in rows
        )
    )
    weighted_exit = weighted_average(
        (
            (parse_float(first_non_empty(row, "exit_price", "avgExitPrice")), parse_float(first_non_empty(row, "qty", "closed_size", "size")))
            for row in rows
        )
    )

    r_values_and_weights = [
        (parse_float(first_non_empty(row, "r_multiple")), parse_float(first_non_empty(row, "qty", "closed_size", "size")))
        for row in rows
    ]
    episode_r = weighted_average(r_values_and_weights)

    first_entry_at = min((dt for dt in (row_created_at(row) for row in rows) if dt is not None), default=None)
    last_close_at = max((dt for dt in (row_closed_at(row) for row in rows) if dt is not None), default=None)
    duration_hours = None
    if first_entry_at and last_close_at:
        duration_hours = (last_close_at - first_entry_at).total_seconds() / 3600

    row_types = [classify_raw_row(row, be_r_threshold=be_r_threshold) for row in rows]
    final_result = final_episode_result(row_types, episode_r)

    has_liq = final_result == "LIQUIDATION" or "LIQUIDATION" in row_types
    has_partial = "PARTIAL_TP" in row_types
    has_take_profit = has_partial or "TAKE_PROFIT" in row_types
    has_stop_loss = any(t in {"SL", "STOP_CLOSE", "BE_STOP", "PROTECTED_BE"} for t in row_types)
    has_protected_be = "PROTECTED_BE" in row_types or final_result in {"PROTECTED_BE", "TP1_THEN_PROTECTED_BE"}

    signal_context = signal_map.get(signal_id, {}) if signal_id else {}

    episode: Dict[str, object] = {
        "episode_id": episode_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": side,
        "first_entry_at": first_entry_at.isoformat() if first_entry_at else "",
        "last_close_at": last_close_at.isoformat() if last_close_at else "",
        "duration_hours": round(duration_hours, 2) if duration_hours is not None else "",
        "row_count": len(rows),
        "trade_ids": unique_join(trade_ids),
        "match_quality": unique_join(first_non_empty(row, "match_quality") for row in rows),
        "entry_price": entry_price,
        "weighted_avg_exit_price": weighted_exit,
        "total_qty": total_qty,
        "total_closed_pnl": total_pnl,
        "stop": next((first_non_empty(row, "stop") for row in rows if first_non_empty(row, "stop")), signal_context.get("stop", "")),
        "tp1": next((first_non_empty(row, "tp1") for row in rows if first_non_empty(row, "tp1")), signal_context.get("tp1", "")),
        "tp2": next((first_non_empty(row, "tp2") for row in rows if first_non_empty(row, "tp2")), signal_context.get("tp2", "")),
        "episode_r_multiple": episode_r,
        "final_result_type": final_result,
        "close_path": " -> ".join(row_types),
        "has_partial_tp": has_partial,
        "has_take_profit_close": has_take_profit,
        "has_stop_loss_close": has_stop_loss,
        "has_protected_be": has_protected_be,
        "has_liquidation": has_liq,
        "liquidation_flag_source": unique_join(first_non_empty(row, "liquidation_flag_source") for row in rows),
        "close_create_types": unique_join(first_non_empty(row, "close_create_type", "createType") for row in rows),
        "close_exec_types": unique_join(first_non_empty(row, "close_exec_type", "execType") for row in rows),
        "close_order_types": unique_join(first_non_empty(row, "close_order_type", "orderType") for row in rows),
        "source_rows_comment": unique_join(first_non_empty(row, "comment") for row in rows),
    }

    enrich_from_signal_context(episode, signal_context=signal_context, source_rows=rows)
    return episode


def build_trade_episodes(
    trades_log: Path,
    signals_log: Path,
    selected_signals: Path,
    output: Path,
    be_r_threshold: float = DEFAULT_BE_R_THRESHOLD,
) -> List[Dict[str, object]]:
    trades = read_csv_rows(trades_log)
    if not trades:
        raise FileNotFoundError(f"No rows found in {trades_log}")

    signals = read_csv_rows(signals_log)
    selected = read_csv_rows(selected_signals)
    signal_map = build_signal_maps(signals, selected)

    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in trades:
        groups[episode_group_key(row)].append(row)

    episodes = [
        aggregate_episode(rows, signal_map=signal_map, be_r_threshold=be_r_threshold)
        for _, rows in sorted(groups.items(), key=lambda item: row_sort_key(sorted(item[1], key=row_sort_key)[0]))
    ]

    write_csv_rows(output, episodes, OUTPUT_COLUMNS)
    return episodes


def print_summary(episodes: List[Dict[str, object]], output: Path) -> None:
    result_counts = Counter(str(row.get("final_result_type", "UNKNOWN")) for row in episodes)
    side_counts = Counter(str(row.get("side", "")).lower() or "unknown" for row in episodes)

    print(f"\nSaved {len(episodes)} trade episodes to {output}")

    print("\nFinal result distribution:")
    for result_type, count in result_counts.most_common():
        print(f"  {result_type}: {count}")

    print("\nSide distribution:")
    for side, count in side_counts.most_common():
        print(f"  {side}: {count}")

    protected = sum(1 for row in episodes if truthy(row.get("has_protected_be", "")))
    partial = sum(1 for row in episodes if truthy(row.get("has_partial_tp", "")))
    liquidation = sum(1 for row in episodes if truthy(row.get("has_liquidation", "")))

    print("\nFlags:")
    print(f"  partial TP episodes: {partial}")
    print(f"  protected BE episodes: {protected}")
    print(f"  liquidation episodes: {liquidation}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate raw Bybit closed-trade rows into one row per scanner trading idea."
    )
    parser.add_argument("--trades-log", default=DEFAULT_TRADES_LOG, help=f"Raw trades log CSV. Default: {DEFAULT_TRADES_LOG}")
    parser.add_argument("--signals-log", default=DEFAULT_SIGNALS_LOG, help=f"Signals log CSV. Default: {DEFAULT_SIGNALS_LOG}")
    parser.add_argument(
        "--selected-signals",
        default=DEFAULT_SELECTED_SIGNALS,
        help=f"Selected signals CSV. Default: {DEFAULT_SELECTED_SIGNALS}",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output episodes CSV. Default: {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--be-r-threshold",
        type=float,
        default=DEFAULT_BE_R_THRESHOLD,
        help="Absolute R threshold for BE/protected BE classification. Default: 0.20",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    trades_log = Path(args.trades_log)
    signals_log = Path(args.signals_log)
    selected_signals = Path(args.selected_signals)
    output = Path(args.output)

    episodes = build_trade_episodes(
        trades_log=trades_log,
        signals_log=signals_log,
        selected_signals=selected_signals,
        output=output,
        be_r_threshold=args.be_r_threshold,
    )
    print_summary(episodes, output)


if __name__ == "__main__":
    main()
