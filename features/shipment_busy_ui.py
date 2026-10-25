"""Shared visual operation guard; it never acquires a shipping lock."""
import html

RUNNING_GUARD_CSS = """
<style>
        .running-guard-overlay {
            position: fixed;
            inset: 0;
            z-index: 999999;
            background: rgba(3, 7, 18, 0.48);
            backdrop-filter: blur(1.5px);
            display: flex;
            align-items: flex-start;
            justify-content: center;
            padding-top: 14.5rem;
            pointer-events: all;
        }
        .running-guard-box {
            width: min(560px, calc(100vw - 32px));
            border: 1px solid rgba(82, 117, 168, 0.58);
            background: rgba(15, 23, 42, 0.96);
            border-radius: 8px;
            padding: 1rem 1.25rem;
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
        }
        .running-guard-title {
            color: #ffffff;
            font-size: 1.25rem;
            font-weight: 900;
            line-height: 1.2;
        }
        .running-guard-text {
            color: #9bb7df;
            font-size: .95rem;
            font-weight: 750;
            margin-top: .42rem;
        }
        .running-guard-sub {
            color: #cbd5e1;
            font-size: .82rem;
            margin-top: .35rem;
        }
/* Preserve native widget DOM and pending edits while blocking user input. */
.stApp:has(.reissue-busy-guard) :is(button, input, textarea, select, a, [role="combobox"], [tabindex]) {
    visibility: hidden;
}
.stApp .reissue-busy-guard,
.stApp .reissue-busy-guard * {
    visibility: visible;
}
</style>
"""


def render_running_guard(target, *, title, active_order, active_stage, done=0, total=0,
                         block_background=False):
    count_text = f"｜{done}/{total}" if total else ''
    guard_class = 'running-guard-overlay' + (' reissue-busy-guard' if block_background else '')
    target.markdown(
        RUNNING_GUARD_CSS + f'<div class="{guard_class}" role="status" aria-live="polite">'
        '<div class="running-guard-box">'
        f'<div class="running-guard-title">{html.escape(title)}{count_text}</div>'
        f'<div class="running-guard-text">目前處理：{html.escape(active_order)}｜{html.escape(active_stage)}</div>'
        '<div class="running-guard-sub">已鎖定操作以避免重複製單，畫面會自動更新。</div>'
        '</div></div>', unsafe_allow_html=True)
