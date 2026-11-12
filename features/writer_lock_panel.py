"""共用製單鎖的手動解除按鈕（操作人員用，放在登出鈕右側）。

製單途中關閉或重新整理畫面，會讓處理被中斷、鎖來不及釋放。系統刻意不依時間自動
解鎖（無法證明寫入者已死亡），因此提供人工解除：只把卡住的鎖「改名封存」，
不刪任何資料；鎖建立未滿最低時間（可能仍有人在製單）一律拒絕。
平常不會讀取 Google Sheets，只有按下按鈕才呼叫，不耗用配額。
"""
from __future__ import annotations

import sys

import streamlit as st

from bot.shipment_ledger import (
    LOCK_RELEASE_MIN_AGE_SECONDS, LockReleaseRefused, inspect_writer_lock, release_stuck_writer_lock,
)


def release_writer_lock_with_feedback() -> None:
    """檢查並解除共用鎖，結果以 toast 顯示。"""
    try:
        info = inspect_writer_lock()
        if info is None:
            st.toast("目前沒有共用鎖，可以正常製單。", icon="✅")
            return
        generation = info.get("generation")
        if generation is None or info["stage"] in {"ambiguous", "unreadable"}:
            st.toast("共用鎖狀態異常，無法由此解除，請聯絡系統管理員。", icon="⚠️")
            return
        age = info.get("age_seconds") or 0
        if age < LOCK_RELEASE_MIN_AGE_SECONDS:
            st.toast(
                f"共用鎖建立僅 {int(age)} 秒，可能仍有人在製單；"
                f"請至少等待 {LOCK_RELEASE_MIN_AGE_SECONDS // 60} 分鐘。", icon="⏳")
            return
        title = release_stuck_writer_lock(generation)
        print(f"[LOCK_BUTTON] released title={title}", file=sys.stderr)
        st.toast("已解除共用鎖，可以重新製單。已完成的 PDF 與製單紀錄不受影響。", icon="🔓")
    except LockReleaseRefused as exc:
        st.toast(str(exc), icon="⚠️")
    except Exception as exc:
        print(f"[LOCK_BUTTON] failed type={type(exc).__name__}", file=sys.stderr)
        st.toast("解除共用鎖失敗，請稍後再試或聯絡系統管理員。", icon="⚠️")


def render_writer_lock_button() -> None:
    if st.button("解除共用鎖", key="writer_lock_release_button", type="secondary",
                 help="製單途中被中斷（關閉或重整畫面）導致卡住時使用；只會封存鎖，不刪資料。"):
        release_writer_lock_with_feedback()
