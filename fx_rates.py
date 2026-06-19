from __future__ import annotations

from typing import Any

USD_JPY_ENDPOINTS = (
    "https://api.frankfurter.dev/v2/rates?base=USD&quotes=JPY",
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols=JPY",
    "https://api.frankfurter.app/latest?from=USD&to=JPY",
)


def parse_usd_jpy_rate_response(data: Any) -> tuple[float | None, str]:
    try:
        if isinstance(data, list) and data:
            first = data[0]
            rate = float(first.get("rate"))
            return (rate, str(first.get("date", "latest"))) if rate > 0 else (None, "")
        if isinstance(data, dict):
            rates = data.get("rates") or {}
            rate = float(rates.get("JPY"))
            return (rate, str(data.get("date", "latest"))) if rate > 0 else (None, "")
    except Exception:
        return None, ""
    return None, ""


def fetch_usd_jpy_rate(timeout: int = 8) -> tuple[float | None, str, str]:
    import requests

    last_error = ""
    for endpoint in USD_JPY_ENDPOINTS:
        try:
            resp = requests.get(endpoint, timeout=timeout)
            resp.raise_for_status()
            rate, rate_date = parse_usd_jpy_rate_response(resp.json())
            if rate:
                return rate, rate_date, endpoint
            last_error = f"{endpoint}: unexpected response"
        except Exception as e:
            last_error = f"{endpoint}: {type(e).__name__}: {e}"
    return None, "", last_error
