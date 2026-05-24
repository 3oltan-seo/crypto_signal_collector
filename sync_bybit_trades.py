import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP


TRADES_LOG = "trades_log.csv"
SIGNALS_LOG = "signals_log.csv"
SELECTED_SIGNALS_LOG = "selected_signals.csv"

ALLOW_WEAK_MATCH_AFTER_ENTRY = False


load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

if not API_KEY or not API_SECRET:
    raise RuntimeError("Нет BYBIT_API_KEY или BYBIT_API_SECRET в .env")

session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET,
)


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


def ms_to_dt(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


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

    if "signal_id" not in df.columns:
        return pd.DataFrame()

    df = df.drop_duplicates(subset=["signal_id"], keep="last")

    if "created_at" in df.columns:
        df["created_at_dt"] = parse_dt(df["created_at"])

    return df


def get_closed_pnl(limit=100):
    response = session.get_closed_pnl(
        category="linear",
        limit=limit,
    )

    return response.get("result", {}).get("list", [])


def get_close_order_meta(symbol, order_id):
    meta = {
        "close_order_type": "",
        "close_create_type": "",
        "close_exec_type": "",
        "is_liquidation": False,
        "liquidation_flag_source": "",
    }

    if not order_id:
        return meta

    try:
        order_response = session.get_order_history(
            category="linear",
            symbol=symbol,
            orderId=order_id,
            limit=1,
        )
        orders = order_response.get("result", {}).get("list", [])

        if orders:
            order = orders[0]
            meta["close_order_type"] = order.get("orderType", "")
            meta["close_create_type"] = order.get("createType", "")

            create_type = str(meta["close_create_type"]).lower()
            if any(token in create_type for token in ["liq", "adl", "adminclosing"]):
                meta["is_liquidation"] = True
                meta["liquidation_flag_source"] = f"order_history.createType={meta['close_create_type']}"
    except Exception:
        pass

    try:
        exec_response = session.get_executions(
            category="linear",
            symbol=symbol,
            orderId=order_id,
            limit=50,
        )
        executions = exec_response.get("result", {}).get("list", [])

        exec_types = sorted({
            str(execution.get("execType", ""))
            for execution in executions
            if execution.get("execType", "") != ""
        })

        if exec_types:
            meta["close_exec_type"] = "|".join(exec_types)

        exec_types_lower = "|".join(exec_types).lower()
        if any(token in exec_types_lower for token in ["adl", "liq", "bust"]):
            meta["is_liquidation"] = True
            source = f"executions.execType={meta['close_exec_type']}"
            if meta["liquidation_flag_source"]:
                meta["liquidation_flag_source"] += f"; {source}"
            else:
                meta["liquidation_flag_source"] = source
    except Exception:
        pass

    return meta


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


def find_matching_signal(symbol, position_side, entry_price, trade_created_at, trade_closed_at, signals_df):
    if signals_df.empty:
        return None, "no_signals_log"

    required = {"symbol", "side", "signal_id", "created_at_dt"}

    if not required.issubset(set(signals_df.columns)):
        return None, "signals_log_missing_columns"

    df = signals_df.copy()
    df = df[df["symbol"].astype(str) == symbol]
    df = df[df["side"].astype(str).str.lower() == position_side.lower()]

    if df.empty:
        return None, "no_symbol_side_match"

    entry_dt = parse_dt(trade_created_at)
    close_dt = parse_dt(trade_closed_at)

    strict_df = df[df["created_at_dt"] <= entry_dt]

    if not strict_df.empty:
        strict_df = strict_df.copy()
        strict_df["match_score"] = strict_df.apply(
            lambda row: signal_match_score(row, entry_price),
            axis=1,
        )
        row = strict_df.sort_values(["match_score", "created_at_dt"]).iloc[-1]
        return row, "strict_before_entry"

    if ALLOW_WEAK_MATCH_AFTER_ENTRY:
        weak_df = df[df["created_at_dt"] <= close_dt]

        if not weak_df.empty:
            weak_df = weak_df.copy()
            weak_df["match_score"] = weak_df.apply(
                lambda row: signal_match_score(row, entry_price),
                axis=1,
            )
            row = weak_df.sort_values(["match_score", "created_at_dt"]).iloc[-1]
            return row, "weak_after_entry_before_close"

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


def has_manual_liquidation_correction(row):
    result_type = str(row.get("result_type", "")).strip().upper()
    is_liquidation = str(row.get("is_liquidation", "")).strip().lower()
    source = str(row.get("liquidation_flag_source", "")).strip().lower()
    comment = str(row.get("comment", "")).strip().lower()

    if result_type == "LIQUIDATION":
        return True

    if is_liquidation in {"true", "1", "yes", "да"}:
        return True

    if source == "manual_user_correction":
        return True

    if "ликвидац" in comment or "liquidation" in comment:
        return True

    return False


def classify_result_with_close_meta(r_multiple, close_meta):
    if close_meta.get("is_liquidation"):
        return "LIQUIDATION"

    return classify_result(r_multiple)


def normalize_closed_trade(row, signals_df):
    symbol = row.get("symbol", "")
    bybit_close_side = row.get("side", "")
    position_side = position_side_from_bybit(bybit_close_side)

    entry_price = float(row.get("avgEntryPrice", 0) or 0)
    exit_price = float(row.get("avgExitPrice", 0) or 0)
    qty = float(row.get("qty", 0) or 0)
    closed_pnl = float(row.get("closedPnl", 0) or 0)
    trade_id = row.get("orderId") or row.get("execId") or f"{symbol}_{row.get('updatedTime')}"
    close_meta = get_close_order_meta(symbol, trade_id)

    created_at = ms_to_dt(row.get("createdTime"))
    closed_at = ms_to_dt(row.get("updatedTime"))

    signal, match_quality = find_matching_signal(
        symbol=symbol,
        position_side=position_side,
        entry_price=entry_price,
        trade_created_at=created_at,
        trade_closed_at=closed_at,
        signals_df=signals_df,
    )

    signal_id = ""
    enriched = {}

    if signal is not None:
        signal_id = signal.get("signal_id", "")
        for col in SIGNAL_COLUMNS_TO_COPY:
            enriched[col] = signal.get(col, "")
    else:
        for col in SIGNAL_COLUMNS_TO_COPY:
            enriched[col] = ""

    stop = enriched.get("stop", "")
    r_multiple = calculate_r_multiple(position_side, entry_price, exit_price, stop)

    trade = {
        "trade_id": trade_id,
        "signal_id": signal_id,
        "match_quality": match_quality,
        "symbol": symbol,
        "position_side": position_side,
        "bybit_close_side": bybit_close_side,
        "close_order_type": close_meta.get("close_order_type", ""),
        "close_create_type": close_meta.get("close_create_type", ""),
        "close_exec_type": close_meta.get("close_exec_type", ""),
        "is_liquidation": close_meta.get("is_liquidation", False),
        "liquidation_flag_source": close_meta.get("liquidation_flag_source", ""),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "qty": qty,
        "closed_pnl": closed_pnl,
        "stop": stop,
        "tp1": enriched.get("tp1", ""),
        "tp2": enriched.get("tp2", ""),
        "r_multiple": r_multiple,
        "result_type": classify_result_with_close_meta(r_multiple, close_meta),
        "created_at": created_at,
        "closed_at": closed_at,
        "source": "bybit_closed_pnl",
        "comment": "",
    }

    for col in SIGNAL_COLUMNS_TO_COPY:
        if col not in {"stop", "tp1", "tp2"}:
            trade[col] = enriched.get(col, "")

    return trade


def merge_with_existing(existing, new_df):
    if existing.empty:
        return new_df

    for col in OUTPUT_COLUMNS:
        if col not in existing.columns:
            existing[col] = ""
        if col not in new_df.columns:
            new_df[col] = ""

    existing_by_id = {}

    if "trade_id" in existing.columns:
        for _, row in existing.iterrows():
            trade_id = str(row.get("trade_id", ""))
            if trade_id:
                existing_by_id[trade_id] = row.to_dict()

    protected_rows = []

    for _, row in new_df.iterrows():
        trade = row.to_dict()
        trade_id = str(trade.get("trade_id", ""))
        old_trade = existing_by_id.get(trade_id)

        if old_trade and has_manual_liquidation_correction(old_trade):
            trade["result_type"] = "LIQUIDATION"
            trade["is_liquidation"] = True
            trade["liquidation_flag_source"] = old_trade.get(
                "liquidation_flag_source",
                "manual_user_correction",
            ) or "manual_user_correction"

            if old_trade.get("comment", ""):
                trade["comment"] = old_trade.get("comment", "")
            elif not trade.get("comment", ""):
                trade["comment"] = "Ликвидация, подтверждено вручную"

        protected_rows.append(trade)

    protected_new_df = pd.DataFrame(protected_rows)

    for col in OUTPUT_COLUMNS:
        if col not in protected_new_df.columns:
            protected_new_df[col] = ""

    final = pd.concat([existing[OUTPUT_COLUMNS], protected_new_df[OUTPUT_COLUMNS]], ignore_index=True)
    final = final.drop_duplicates(subset=["trade_id"], keep="last")
    return final


def main():
    existing = load_csv(TRADES_LOG)
    signals = load_signals()
    rows = get_closed_pnl(limit=100)

    if not rows:
        print("Закрытых сделок не найдено.")
        return

    normalized = [normalize_closed_trade(row, signals) for row in rows]
    new_df = pd.DataFrame(normalized)

    for col in OUTPUT_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = ""

    new_df = new_df[OUTPUT_COLUMNS]
    final_df = merge_with_existing(existing, new_df)
    final_df.to_csv(TRADES_LOG, index=False, encoding="utf-8-sig")

    print(f"Синхронизировано сделок из Bybit: {len(new_df)}")
    print(f"Всего строк в {TRADES_LOG}: {len(final_df)}")
    print("")
    print(final_df[[
        "symbol",
        "position_side",
        "entry_price",
        "exit_price",
        "closed_pnl",
        "signal_id",
        "match_quality",
        "r_multiple",
        "result_type",
    ]].tail(20))


if __name__ == "__main__":
    main()
