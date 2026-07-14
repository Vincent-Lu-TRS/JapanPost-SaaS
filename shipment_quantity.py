from decimal import Decimal, InvalidOperation


def parse_shipment_quantity(value, item_index: int) -> int:
    text = "" if value is None else str(value).strip().replace(",", "")
    if not text:
        return 0

    try:
        quantity = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"內容物{item_index}的數量格式錯誤：{text}") from exc

    if not quantity.is_finite() or quantity != quantity.to_integral_value():
        raise ValueError(f"內容物{item_index}的數量格式錯誤：{text}")
    return int(quantity)
