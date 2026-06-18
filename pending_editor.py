import pandas as pd


SHIPPING_COL = "郵局運送方式(複數商品請自行確認是否走小包)"
SHIPPING_OPTIONS = ["EMS", "國際小包", "ePacket"]

EDITABLE_PENDING_COLUMNS = [
    "注文番号(貼上原始資料)",
    "Shipping Name",
    "收件人國家",
    SHIPPING_COL,
    "郵局申告金額(USD)",
    "內容物1",
    "申告金額1",
    "數量1",
    "內容物2",
    "申告金額2",
    "數量2",
    "內容物3",
    "申告金額3",
    "數量3",
    "內容物4",
    "申告金額4",
    "數量4",
    "內容物5",
    "申告金額5",
    "數量5",
    "訂單合計申告金額(JPY)",
    "HSCode",
]


def build_pending_editor_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame()
    for column in EDITABLE_PENDING_COLUMNS:
        if column == "HSCode":
            frame[column] = ""
        elif column in df.columns:
            frame[column] = df[column].fillna("").astype(str)
        else:
            frame[column] = ""
    return frame


def coerce_pending_editor_values(df: pd.DataFrame) -> pd.DataFrame:
    coerced = df.copy()
    if SHIPPING_COL in coerced.columns:
        coerced[SHIPPING_COL] = coerced[SHIPPING_COL].fillna("").astype(str).str.strip()
    for column in [
        "郵局申告金額(USD)",
        "申告金額1",
        "申告金額2",
        "申告金額3",
        "申告金額4",
        "申告金額5",
        "訂單合計申告金額(JPY)",
    ]:
        if column in coerced.columns:
            coerced[column] = coerced[column].fillna("").astype(str).str.strip()
    return coerced


def apply_pending_editor_values(original: pd.DataFrame, edited: pd.DataFrame) -> pd.DataFrame:
    applied = original.copy()
    coerced = coerce_pending_editor_values(edited)
    target_index = applied.index[:len(coerced)]
    for column in EDITABLE_PENDING_COLUMNS:
        if column == "HSCode":
            continue
        if column in coerced.columns:
            applied.loc[target_index, column] = coerced[column].values
    return applied
