"""共用製單鎖的檢查與手動解除（操作人員用）。

製單途中關閉或重新整理畫面，會讓處理被中斷、鎖來不及釋放。系統刻意不依時間自動
解鎖（無法證明寫入者已死亡），因此提供人工解除：只把卡住的鎖「改名封存」，
不刪任何資料；需達最低建立時間並勾選確認，避免誤拆仍在製單的鎖。
展開面板時不會讀取 Google Sheets，只有按「檢查」才會呼叫一次，避免耗用配額。
"""
from __future__ import annotations

import sys

import streamlit as st

from bot.shipment_ledger import (
    LOCK_RELEASE_MIN_AGE_SECONDS, LockReleaseRefused, inspect_writer_lock, release_stuck_writer_lock,
)

_STATE_KEY = "writer_lock_inspection"
_STAGE_TEXT = {
    "locked": "已上鎖（製單處理中或被中斷）",
    "issuing": "正在送出單據",
    "writeback_pending": "等待回填結果",
    "completed": "已完成但未封存",
    "ambiguous": "同時存在多把鎖，需管理員處理",
    "unreadable": "鎖內容無法判讀，需管理員處理",
}


def _age_text(seconds) -> str:
    if seconds is None:
        return "未知"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes} 分 {secs} 秒" if minutes else f"{secs} 秒"


def render_writer_lock_panel(*, disabled: bool = False) -> None:
    with st.expander("製單卡住了？檢查／解除共用鎖", expanded=False):
        st.caption(
            "製單途中關閉或重新整理畫面，可能讓共用鎖留在「處理中」。確認沒有人正在製單後，"
            "可在此解除；系統只會把鎖改名封存，不會刪除任何資料或製單紀錄。")
        if disabled:
            st.caption("目前有製單執行中，請待其完成後再操作。")
        if st.button("檢查共用鎖狀態", key="writer_lock_check", disabled=disabled):
            try:
                st.session_state[_STATE_KEY] = {"info": inspect_writer_lock()}
            except Exception as exc:
                print(f"[LOCK_PANEL] inspect_failed type={type(exc).__name__}", file=sys.stderr)
                st.session_state[_STATE_KEY] = {"error": "無法讀取共用鎖狀態，請稍後再試。"}

        state = st.session_state.get(_STATE_KEY)
        if not state:
            return
        if state.get("error"):
            st.error(state["error"])
            return
        info = state.get("info")
        if info is None:
            st.success("目前沒有共用鎖，可以正常製單。")
            return

        stage = _STAGE_TEXT.get(info["stage"], info["stage"])
        age = info.get("age_seconds")
        st.warning(f"發現共用鎖：{stage}｜已存在 {_age_text(age)}")
        generation = info.get("generation")
        if generation is None or info["stage"] in {"ambiguous", "unreadable"}:
            st.error("這種狀態無法由此解除，請聯絡系統管理員。")
            return
        if age is not None and age < LOCK_RELEASE_MIN_AGE_SECONDS:
            st.info(f"此鎖建立不到 {LOCK_RELEASE_MIN_AGE_SECONDS // 60} 分鐘，可能仍有人在製單。請稍候再檢查。")
            return
        confirmed = st.checkbox("我確認目前沒有任何人正在製單", key="writer_lock_confirm")
        if st.button("解除共用鎖（改名封存）", key="writer_lock_release",
                     disabled=disabled or not confirmed, type="primary"):
            try:
                title = release_stuck_writer_lock(generation)
            except LockReleaseRefused as exc:
                st.session_state.pop(_STATE_KEY, None)
                st.error(str(exc))
            except Exception as exc:
                print(f"[LOCK_PANEL] release_failed type={type(exc).__name__}", file=sys.stderr)
                st.session_state.pop(_STATE_KEY, None)
                st.error("解除共用鎖失敗，請重新檢查狀態或聯絡系統管理員。")
            else:
                print(f"[LOCK_PANEL] released title={title}", file=sys.stderr)
                st.session_state.pop(_STATE_KEY, None)
                st.success("已解除共用鎖，可以重新製單。已製作完成的 PDF 與製單紀錄不受影響。")
