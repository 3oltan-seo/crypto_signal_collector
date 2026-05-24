import os

import pandas as pd


TRADES_LOG = "trades_log.csv"
SIGNALS_LOG = "signals_log.csv"
SELECTED_SIGNALS_LOG = "selected_signals.csv"

ALLOW_WEAK_MATCH_AFTER_ENTRY = False


SIGNAL_COLUMNS_TO_COPY = [
    "score",
    "entry",
    "stop",
    "tp1",
    "tp2",
    "rr1",
    "rr2",
    "turnover24h_m",
    "change24h_pct",
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
]


OUTPUT_COLUMNS = [
    "trade_id",
    "signal_id",
    "match_quality",
    "symbol",
    "position_side",
    "bybit_close_side",
    "close_order_type",
    "close_create_type",
    "close_exec_type",
    "is_liquidation",
    "liquidation_flag_source",
    "side",
    "entry_price",
    "exit_price",
    "qty",
    "closed_pnl",
    "stop",
    "tp1",
    "tp2",
    "r_multiple",
    "result_type",
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
    "created_at",
    "closed_at",
    "source",
    "comment",
]


def parse_dt(value):
    return pd.to_datetime(value, errors="coerce", utc=True)


def parse_entry_zone(value):
    if not isinstance(value, str) or "-" not in value:
        return None, None

    left, right = value.split("-", 1)

    try:
        return float(left.strip()), float(right.strip())
    except Exception:
        return None, None


def position_side_from_bybit(close_side):
    close_side = str(close_side).strip().lower()

    if close_side == "sell":
        return "long"

    if close_side == "buy":
        return "short"

    if close_side in {"long", "short"}:
        return close_side

    return ""


def load_csv(path):
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame()


def load_signals():
    signals = load_csv(SIGNALS_LOG)
    selected = load_csv(SELECTED_SIGNALS_LOG)

    frames = []

    if not signals.empty:
        frames.append(signals)

    if not selected.empty:
        frames.append(selected)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["signal_id"], keep="last")
    df["created_at_dt"] = parse_dt(df["created_at"])
    return df


def signal_match_score(signal, entry_price):
    score = 0

    if str(signal.get("selected", "")).strip().lower() in {"yes", "y", "1", "true", "да", "д"}:
        score += 1000

    entry_low, entry_high = parse_entry_zone(signal.get("entry", ""))

    if entry_low is not None and entry_high is not None:
        tolerance = max(abs(entry_high - entry_low), abs(entry_price) * 0.003)
        if entry_low - tolerance <= entry_price <= entry_high + tolerance:
            score += 100

    created_at = signal.get("created_at_dt")

    if pd.notna(created_at):
        score += created_at.value / 1e18

    return score


def find_matching_signal(trade, signals_df):
    if signals_df.empty:
        return None, "no_signals_log"

    symbol = trade.get("symbol", "")
    position_side = trade.get("position_side", "")
    entry_price = float(trade.get("entry_price", 0) or 0)
    entry_dt = parse_dt(trade.get("created_at", ""))
    close_dt = parse_dt(trade.get("closed_at", ""))

    df = signals_df.copy()
    df = df[df["symbol"].astype(str) == symbol]
    df = df[df["side"].astype(str).str.lower() == position_side.lower()]

    if df.empty:
        return None, "no_symbol_side_match"

    strict_df = df[df["created_at_dt"] <= entry_dt]

    if not strict_df.empty:
        strict_df = strict_df.copy()
        strict_df["match_score"] = strict_df.apply(
            lambda row: signal_match_score(row, entry_price),
            axis=1,
        )
        return strict_df.sort_values(["match_score", "created_at_dt"]).iloc[-1], "strict_before_entry"

    if ALLOW_WEAK_MATCH_AFTER_ENTRY:
        weak_df = df[df["created_at_dt"] <= close_dt]

        if not weak_df.empty:
            weak_df = weak_df.copy()
            weak_df["match_score"] = weak_df.apply(
                lambda row: signal_match_score(row, entry_price),
                axis=1,
            )
            return weak_df.sort_values(["match_score", "created_at_dt"]).iloc[-1], "weak_after_entry_before_close"

    return None, "no_signal_before_entry"


def calculate_r_multiple(position_side, entry_price, exit_price, stop):
    try:
        entry_price = float(entry_price)
        exit_price = float(exit_price)
        stop = float(stop)
    except Exception:
        return ""

    if position_side == "long":
        risk = entry_price - stop
        if risk <= 0:
            return ""
        return round((exit_price - entry_price) / risk, 4)

    if position_side == "short":
        risk = stop - entry_price
        if risk <= 0:
            return ""
        return round((entry_price - exit_price) / risk, 4)

    return ""


def classify_result(r_multiple):
    if r_multiple == "":
        return ""

    try:
        r = float(r_multiple)
    except Exception:
        return ""

    if r <= -0.8:
        return "SL_or_near_SL"
    if -0.2 <= r <= 0.2:
        return "BE"
    if r >= 1.8:
        return "TP2_or_better"
    if r >= 0.8:
        return "TP1_or_profit"
    if r > 0.2:
        return "manual_profit"
    return "manual_loss"


def has_manual_liquidation_correction(trade):
    result_type = str(trade.get("result_type", "")).strip().upper()
    is_liquidation = str(trade.get("is_liquidation", "")).strip().lower()
    source = str(trade.get("liquidation_flag_source", "")).strip().lower()
    comment = str(trade.get("comment", "")).strip().lower()

    if result_type == "LIQUIDATION":
        return True

    if is_liquidation in {"true", "1", "yes", "да"}:
        return True

    if source == "manual_user_correction":
        return True

    if "ликвидац" in comment or "liquidation" in comment:
        return True

    return False


def classify_result_with_existing_meta(r_multiple, trade):
    if has_manual_liquidation_correction(trade):
        return "LIQUIDATION"

    is_liquidation = str(trade.get("is_liquidation", "")).strip().lower()
    create_type = str(trade.get("close_create_type", "")).lower()
    exec_type = str(trade.get("close_exec_type", "")).lower()
    source = str(trade.get("liquidation_flag_source", "")).lower()

    if is_liquidation in {"true", "1", "yes"}:
        return "LIQUIDATION"

    if any(token in create_type for token in ["liq", "adl", "adminclosing"]):
        return "LIQUIDATION"

    if any(token in exec_type for token in ["liq", "adl", "bust"]):
        return "LIQUIDATION"

    if any(token in source for token in ["liq", "adl", "bust", "adminclosing"]):
        return "LIQUIDATION"

    return classify_result(r_multiple)


def enrich_trade(row, signals_df):
    trade = row.to_dict()
    manual_liquidation = has_manual_liquidation_correction(trade)

    if not trade.get("position_side"):
        trade["position_side"] = position_side_from_bybit(trade.get("side", trade.get("bybit_close_side", "")))

    if not trade.get("bybit_close_side"):
        trade["bybit_close_side"] = trade.get("side", "")

    signal, match_quality = find_matching_signal(trade, signals_df)
    trade["match_quality"] = match_quality

    if signal is not None:
        trade["signal_id"] = signal.get("signal_id", "")
        for col in SIGNAL_COLUMNS_TO_COPY:
            trade[col] = signal.get(col, "")

    r_multiple = calculate_r_multiple(
        trade.get("position_side", ""),
        trade.get("entry_price", ""),
        trade.get("exit_price", ""),
        trade.get("stop", ""),
    )

    trade["r_multiple"] = r_multiple
    trade["result_type"] = classify_result_with_existing_meta(r_multiple, trade)

    if manual_liquidation:
        trade["is_liquidation"] = True
        if not str(trade.get("liquidation_flag_source", "")).strip():
            trade["liquidation_flag_source"] = "manual_user_correction"
        if not str(trade.get("comment", "")).strip():
            trade["comment"] = "Ликвидация, подтверждено вручную"

    return trade


def main():
    if not os.path.exists(TRADES_LOG):
        raise RuntimeError(f"Файл {TRADES_LOG} не найден.")

    trades_df = pd.read_csv(TRADES_LOG)
    signals_df = load_signals()

    enriched = pd.DataFrame([enrich_trade(row, signals_df) for _, row in trades_df.iterrows()])

    for col in OUTPUT_COLUMNS:
        if col not in enriched.columns:
            enriched[col] = ""

    remaining_cols = [c for c in enriched.columns if c not in OUTPUT_COLUMNS]
    enriched = enriched[OUTPUT_COLUMNS + remaining_cols]

    backup = "trades_log.backup.csv"
    trades_df.to_csv(backup, index=False, encoding="utf-8-sig")
    enriched.to_csv(TRADES_LOG, index=False, encoding="utf-8-sig")

    print(f"Бэкап старого лога: {backup}")
    print(f"Обновлено: {TRADES_LOG}")
    print("")
    print(enriched[[
        "symbol",
        "position_side",
        "entry_price",
        "exit_price",
        "closed_pnl",
        "signal_id",
        "match_quality",
        "r_multiple",
        "result_type",
    ]])


if __name__ == "__main__":
    main()
