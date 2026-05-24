import os
from datetime import datetime, timezone

import pandas as pd


XLSX_FILE = "scanner_results.xlsx"
SIGNALS_LOG = "signals_log.csv"
SELECTED_SIGNALS_LOG = "selected_signals.csv"


def is_yes(value):
    return str(value).strip().lower() in {"yes", "y", "1", "true", "да", "д"}


def append_csv_dedup(path, new_df, subset):
    if new_df.empty:
        return pd.DataFrame()

    if os.path.exists(path):
        old_df = pd.read_csv(path)
        final_df = pd.concat([old_df, new_df], ignore_index=True)
    else:
        final_df = new_df.copy()

    final_df = final_df.drop_duplicates(subset=subset, keep="last")
    final_df.to_csv(path, index=False, encoding="utf-8-sig")
    return final_df


def update_signals_log(selected_df):
    if not os.path.exists(SIGNALS_LOG):
        return

    signals_df = pd.read_csv(SIGNALS_LOG)

    if "signal_id" not in signals_df.columns:
        return

    updates = selected_df.set_index("signal_id")

    for col in ["selected", "manual_comment"]:
        if col not in signals_df.columns:
            signals_df[col] = ""

        if col in updates.columns:
            signals_df[col] = signals_df.apply(
                lambda row: updates.loc[row["signal_id"], col]
                if row["signal_id"] in updates.index
                else row.get(col, ""),
                axis=1,
            )

    signals_df.to_csv(SIGNALS_LOG, index=False, encoding="utf-8-sig")


def main():
    if not os.path.exists(XLSX_FILE):
        raise RuntimeError(f"Файл {XLSX_FILE} не найден.")

    df = pd.read_excel(XLSX_FILE, engine="openpyxl")

    if "selected" not in df.columns:
        raise RuntimeError("В Excel нет колонки selected.")

    if "signal_id" not in df.columns:
        raise RuntimeError("В Excel нет колонки signal_id.")

    selected_df = df[df["selected"].apply(is_yes)].copy()

    if selected_df.empty:
        print("Нет строк с selected=yes.")
        return

    selected_df["selected_saved_at"] = datetime.now(timezone.utc).isoformat()

    append_csv_dedup(SELECTED_SIGNALS_LOG, selected_df, ["signal_id"])
    update_signals_log(selected_df)

    print(f"Сохранено выбранных сигналов: {len(selected_df)}")
    print(f"Файл: {SELECTED_SIGNALS_LOG}")
    print("")
    print(selected_df[["signal_id", "symbol", "side", "selected", "manual_comment"]])


if __name__ == "__main__":
    main()
