"""Small helpers for postal pending-order UI feedback."""
from __future__ import annotations

import re


def summarize_pending_read_logs(logs: list[str]) -> dict[str, str]:
    """Extract the useful pending-order read summary from diagnostic log lines."""
    summary = {
        "base_count": "-",
        "completed_filter": "-",
        "dedup_filter": "-",
        "final_count": "-",
        "elapsed": "-",
    }
    for line in logs or []:
        text = str(line)
        if "篩選後（未打單+必填）" in text:
            match = re.search(r"：(\d+)\s*筆", text)
            if match:
                summary["base_count"] = match.group(1)
        elif "雙重過濾" in text:
            match = re.search(r"：(\d+\s*→\s*\d+)\s*筆?", text)
            if match:
                summary["completed_filter"] = re.sub(r"\s*→\s*", " → ", match.group(1))
        elif "來源內同注文番号去重" in text:
            match = re.search(r"：(\d+\s*→\s*\d+)\s*筆?", text)
            if match:
                summary["dedup_filter"] = re.sub(r"\s*→\s*", " → ", match.group(1))
        elif "最終可打單" in text:
            count_match = re.search(r"最終可打單：(\d+)\s*筆", text)
            elapsed_match = re.search(r"總讀取耗時\s*([0-9.]+s)", text)
            if count_match:
                summary["final_count"] = count_match.group(1)
            if elapsed_match:
                summary["elapsed"] = elapsed_match.group(1)
    return summary
