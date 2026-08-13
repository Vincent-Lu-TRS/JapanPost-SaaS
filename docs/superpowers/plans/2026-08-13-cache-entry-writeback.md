# JapanPost-SaaS Cache, Entry, and Writeback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓使用者進站即取得兩份待處理資料、以 20 分鐘共享快照降低等待時間、將新版郵局頁面整理為唯一入口，並使製單回填具備擴列、追加製單識別、逐包裹讀回驗證與明確失敗狀態。

**Architecture:** 以純 Python `SharedRefreshCoordinator` 管理 process 內、分資料源、單飛且可保留舊資料的共享快照；Streamlit 只用 `st.cache_resource` 保存協調器並在安全狀態套用資料副本。Google Sheets 回填仍集中於 `bot/sheets.py`，但改成先建立逐包裹分類、擴充 grid、批次寫入，再逐列驗證 B/C/D/J；頁面依每筆 outcome 更新工作狀態。舊郵局 renderer 移除，新版 renderer 與既有製單後端維持單一路徑。

**Tech Stack:** Python 3、Streamlit 1.56、pandas、gspread、`unittest`、Streamlit AppTest、Git。

---

## Scope and non-negotiable gates

- 本計畫只修改本機 `codex/jppost-cache-entry-writeback-20260813` 分支。
- 應用程式實作前，工作階段必須使用使用者指定的 GPT-5.6 Luna、reasoning effort `max`。目前工作階段若不可切換，停在此計畫，不開始 Task 1。
- 每個既有檔案首次修改前，先複製到 repo 的 `backups/20260813_<原檔名>`；同日重名依治理規則加 `-2`。備份不得加入 Git。
- 現有未追蹤 `.planning/`、`backups/`、`tmp/` 都視為使用者檔案，不刪除、不搬移、不納入 commit；禁止 `git add .`。
- 所有測試使用 synthetic data／fake worksheet；不得讀寫正式 Google Sheet、不得執行日本郵政正式製單。
- 分支 push、PR、正式 Streamlit 部署與正式工作表驗證都需另取得使用者同意。
- Community Cloud 休眠期間不執行 20 分鐘刷新；本計畫不建立 keep-alive。
- 跨 process 的「已產生 tracking 但完全未回填」自動恢復需要持久運單帳本，不在本次範圍。當現有證據不足時必須待人工確認，不得重新製單。

## File responsibility map

- Create `refresh_cache.py`: thread-safe TTL、single-flight、stale fallback、錯誤分類與 copy isolation。
- Create `refresh_payloads.py`: 郵局與揀貨資料的 frozen payload，以及明確的深拷貝函式。
- Modify `features/picking_labels.py`: 將 Google Sheets 讀取與 session 套用拆開，保留選取狀態。
- Modify `app.py`: 建立共享 coordinator、登入後自動載入、活躍 session 定時檢查、手動強制刷新、逐包裹回填 outcome，以及唯一郵局入口。
- Modify `pending_editor.py`: 為原始／追加運單加上 `_shipment_role` 證據。
- Modify `bot/automation.py`: 將 `_shipment_role` 帶入自動化結果。
- Modify `bot/sheets.py`: completion authority、四欄判斷、grid 擴列、逐包裹分類、寫入與讀回重試。
- Modify `job_control.py`: 以 package key 執行 preflight，不再用注文番号跳過同批追加運單。
- Modify `postal_ui_feedback.py`: 只有同注文番号的全部包裹成功才從畫面移除，並修正回填失敗文案。
- Create `safe_logging.py`: 結構化事件只允許計數、耗時、安全 reason code、例外類型與列範圍；既有 carrier 自由文字日誌在持久化前統一遮罩提交資料與追跡番号。
- Create `tests/fake_gspread.py`: 有 row limit、擴列、部分寫入與 stale readback 能力的共用 fake。
- Create `tests/test_refresh_cache.py`: 快取核心測試。
- Create `tests/test_safe_logging.py`: 證明完整注文番号、tracking、姓名與 raw exception 不會進入日誌。
- Modify既有測試：`tests/test_picking_labels.py`、`tests/test_postal_start_flow.py`、`tests/test_postal_ui_v2_app.py`、`tests/test_pending_editor.py`、`tests/test_automation_helpers.py`、`tests/test_sheets_helpers.py`、`tests/test_job_control.py`、`tests/test_postal_ui_feedback.py`、`tests/test_postal_mock_e2e.py`。

### Task 1: Establish baseline and protect existing files

**Files:**
- Backup only: `app.py`, `features/picking_labels.py`, `pending_editor.py`, `bot/automation.py`, `bot/sheets.py`, `job_control.py`, `postal_ui_feedback.py`
- Backup only: the nine existing test files listed in the file map

- [ ] **Step 1: Confirm the exact branch, clean tracked state, and baseline commit**

Run:

```powershell
git branch --show-current
git rev-parse HEAD
git status --short
```

Expected: branch is `codex/jppost-cache-entry-writeback-20260813`, HEAD contains design commit `bc23f69`, and the only pre-existing untracked entries are `.planning/`, `backups/`, and `tmp/`.

- [ ] **Step 2: Run and record the baseline tests before changing code**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
$trackedPy = @(git ls-files "*.py")
python -m py_compile @trackedPy
git diff --check
```

Expected: `unittest` ends in `OK`, tracked Python files compile, and `git diff --check` returns no errors. Record the actual test count and any environment-caused skips; `tests.test_postal_ui_v2_app` must later run rather than remain skipped in the acceptance environment.

- [ ] **Step 3: Create governance-required local backups before the first edit**

Run once for each file, choosing `-2` when the destination already exists:

```powershell
Copy-Item -LiteralPath 'app.py' -Destination 'backups/20260813_app.py'
Copy-Item -LiteralPath 'features/picking_labels.py' -Destination 'backups/20260813_picking_labels.py'
Copy-Item -LiteralPath 'pending_editor.py' -Destination 'backups/20260813_pending_editor.py'
Copy-Item -LiteralPath 'bot/automation.py' -Destination 'backups/20260813_automation.py'
Copy-Item -LiteralPath 'bot/sheets.py' -Destination 'backups/20260813_sheets.py'
Copy-Item -LiteralPath 'job_control.py' -Destination 'backups/20260813_job_control.py'
Copy-Item -LiteralPath 'postal_ui_feedback.py' -Destination 'backups/20260813_postal_ui_feedback.py'
Copy-Item -LiteralPath 'tests/test_picking_labels.py' -Destination 'backups/20260813_test_picking_labels.py'
Copy-Item -LiteralPath 'tests/test_postal_start_flow.py' -Destination 'backups/20260813_test_postal_start_flow.py'
Copy-Item -LiteralPath 'tests/test_postal_ui_v2_app.py' -Destination 'backups/20260813_test_postal_ui_v2_app.py'
Copy-Item -LiteralPath 'tests/test_pending_editor.py' -Destination 'backups/20260813_test_pending_editor.py'
Copy-Item -LiteralPath 'tests/test_automation_helpers.py' -Destination 'backups/20260813_test_automation_helpers.py'
Copy-Item -LiteralPath 'tests/test_sheets_helpers.py' -Destination 'backups/20260813_test_sheets_helpers.py'
Copy-Item -LiteralPath 'tests/test_job_control.py' -Destination 'backups/20260813_test_job_control.py'
Copy-Item -LiteralPath 'tests/test_postal_ui_feedback.py' -Destination 'backups/20260813_test_postal_ui_feedback.py'
Copy-Item -LiteralPath 'tests/test_postal_mock_e2e.py' -Destination 'backups/20260813_test_postal_mock_e2e.py'
```

Expected: all backups exist locally and remain untracked. If any destination already exists, use the exact same command with `-2` inserted before `.py`; do not overwrite an earlier backup.

### Task 2: Build the shared refresh coordinator

**Files:**
- Create: `refresh_cache.py`
- Create: `tests/test_refresh_cache.py`

- [ ] **Step 1: Write the failing coordinator tests**

Create `tests/test_refresh_cache.py` with a controllable clock and these concrete cases:

```python
import threading
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from refresh_cache import SharedRefreshCoordinator


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 13, tzinfo=timezone.utc)

    def now(self):
        return self.value


class RefreshCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.cache = SharedRefreshCoordinator(
            ttl=timedelta(minutes=20), now=self.clock.now
        )

    def test_first_get_loads_and_returns_copy(self):
        calls = []
        result = self.cache.get("pending", lambda: calls.append(1) or {"rows": [1]})
        result.data["rows"].append(2)
        again = self.cache.get("pending", lambda: self.fail("must use cache"))
        self.assertEqual(calls, [1])
        self.assertEqual(again.data, {"rows": [1]})

    def test_get_within_ttl_reuses_snapshot(self):
        calls = []
        self.cache.get("pending", lambda: calls.append(1) or 1)
        self.clock.value += timedelta(minutes=19, seconds=59)
        self.assertEqual(self.cache.get("pending", lambda: calls.append(2) or 2).data, 1)
        self.assertEqual(calls, [1])

    def test_expired_get_refreshes(self):
        values = iter([1, 2])
        self.cache.get("pending", lambda: next(values))
        self.clock.value += timedelta(minutes=20)
        self.assertEqual(self.cache.get("pending", lambda: next(values)).data, 2)

    def test_force_refresh_ignores_ttl(self):
        values = iter([1, 2])
        self.cache.get("pending", lambda: next(values))
        self.assertEqual(self.cache.get("pending", lambda: next(values), force=True).data, 2)

    def test_refresh_failure_serves_last_success(self):
        self.cache.get("pending", lambda: {"rows": [1]})
        self.clock.value += timedelta(minutes=20)
        result = self.cache.get("pending", lambda: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(result.data, {"rows": [1]})
        self.assertTrue(result.status.served_stale)
        self.assertEqual(result.status.error_code, "network")

    def test_first_refresh_failure_returns_no_data(self):
        result = self.cache.get("pending", lambda: (_ for _ in ()).throw(PermissionError()))
        self.assertIsNone(result.data)
        self.assertIsNone(result.status.loaded_at)
        self.assertEqual(result.status.error_code, "permission_denied")

    def test_dataframe_copy_isolation(self):
        first = self.cache.get("pending", lambda: pd.DataFrame({"id": [1]}))
        first.data.loc[0, "id"] = 99
        second = self.cache.get("pending", lambda: self.fail("must use cache"))
        self.assertEqual(second.data.loc[0, "id"], 1)

    def test_sources_refresh_independently(self):
        self.assertIsNone(self.cache.get("pending", lambda: (_ for _ in ()).throw(RuntimeError())).data)
        self.assertEqual(self.cache.get("picking", lambda: ["ok"]).data, ["ok"])

    def test_concurrent_gets_call_loader_once(self):
        gate = threading.Barrier(8)
        release = threading.Event()
        calls = []
        outputs = []

        def loader():
            calls.append(1)
            release.wait(2)
            return {"rows": [1]}

        def worker():
            gate.wait()
            outputs.append(self.cache.get("pending", loader).data)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        while not calls:
            pass
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(outputs, [{"rows": [1]}] * 8)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m unittest tests.test_refresh_cache -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'refresh_cache'`.

- [ ] **Step 3: Implement the minimal thread-safe coordinator**

Create `refresh_cache.py` with these exact public contracts:

```python
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RefreshStatus:
    source: str
    loaded_at: datetime | None
    last_attempt_at: datetime | None
    is_stale: bool
    is_refreshing: bool
    served_stale: bool
    error_code: str | None


@dataclass(frozen=True)
class RefreshResult(Generic[T]):
    data: T | None
    status: RefreshStatus


@dataclass
class _Entry:
    data: object | None = None
    loaded_at: datetime | None = None
    last_attempt_at: datetime | None = None
    refreshing: bool = False
    error_code: str | None = None


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    return "unexpected"


class SharedRefreshCoordinator:
    def __init__(
        self,
        ttl: timedelta = timedelta(minutes=20),
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ttl = ttl
        self._now = now
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _Entry] = {}

    def _result(self, source: str, entry: _Entry, *, served_stale: bool, copier):
        now = self._now()
        stale = entry.loaded_at is None or now - entry.loaded_at >= self._ttl
        return RefreshResult(
            data=None if entry.data is None else copier(entry.data),
            status=RefreshStatus(
                source=source,
                loaded_at=entry.loaded_at,
                last_attempt_at=entry.last_attempt_at,
                is_stale=stale,
                is_refreshing=entry.refreshing,
                served_stale=served_stale,
                error_code=entry.error_code,
            ),
        )

    def get(
        self,
        source: str,
        loader: Callable[[], T],
        *,
        force: bool = False,
        copier: Callable[[T], T] = copy.deepcopy,
    ) -> RefreshResult[T]:
        with self._condition:
            entry = self._entries.setdefault(source, _Entry())
            fresh = entry.loaded_at is not None and self._now() - entry.loaded_at < self._ttl
            if fresh and not force:
                return self._result(source, entry, served_stale=False, copier=copier)
            if entry.refreshing:
                if entry.data is not None and not force:
                    return self._result(source, entry, served_stale=True, copier=copier)
                while entry.refreshing:
                    self._condition.wait()
                return self._result(
                    source, entry, served_stale=entry.error_code is not None, copier=copier
                )
            entry.refreshing = True
            entry.last_attempt_at = self._now()

        try:
            loaded = loader()
        except Exception as exc:
            with self._condition:
                entry.error_code = _safe_error_code(exc)
                entry.refreshing = False
                self._condition.notify_all()
                return self._result(
                    source, entry, served_stale=entry.data is not None, copier=copier
                )

        with self._condition:
            entry.data = copier(loaded)
            entry.loaded_at = self._now()
            entry.error_code = None
            entry.refreshing = False
            self._condition.notify_all()
            return self._result(source, entry, served_stale=False, copier=copier)

    def status(self, source: str) -> RefreshStatus:
        with self._condition:
            entry = self._entries.setdefault(source, _Entry())
            return self._result(
                source,
                entry,
                served_stale=entry.error_code is not None and entry.data is not None,
                copier=copy.deepcopy,
            ).status
```

- [ ] **Step 4: Run focused tests and commit**

Run:

```powershell
python -m unittest tests.test_refresh_cache -v
python -m py_compile refresh_cache.py
git diff --check
git add -- refresh_cache.py tests/test_refresh_cache.py
git commit -m "feat: add shared refresh coordinator"
```

Expected: all refresh tests PASS; commit contains only the two new files.

### Task 3: Define payloads and refactor picking-label loading

**Files:**
- Create: `refresh_payloads.py`
- Modify: `features/picking_labels.py`
- Modify: `tests/test_picking_labels.py`

- [ ] **Step 1: Write RED tests for pure loading, safe apply, and selection preservation**

Add this explicit fixture above the new tests; it uses the `streamlit` stub that
`tests/test_picking_labels.py` already installs in `sys.modules` and does not rely on
an undeclared `setUp()` helper:

```python
import features.picking_labels as picking_labels
from refresh_payloads import PickingPayload


def _picking_payload_for_rows(*source_rows: int) -> PickingPayload:
    return PickingPayload(
        orders=tuple(
            PickingOrder(
                source_row_number=row,
                order_date="",
                order_source="",
                order_no=f"ORDER-{row}",
                logistics_method="郵便局",
                items=[],
                qr_content="",
                shipping_deadline="",
            )
            for row in source_rows
        ),
        warnings=(),
        diagnostics={},
    )
```

Add tests that patch the existing sheet parser and assert these contracts. In each
test, save and restore `picking_labels.st.session_state` with `addCleanup` so this
shared module stub cannot leak state across tests:

```python
def test_load_picking_payload_does_not_mutate_session_state(self):
    state = picking_labels.st.session_state
    before = dict(state)
    self.addCleanup(lambda: (state.clear(), state.update(before)))
    with patch.object(
        picking_labels,
        "load_sheet_values",
        side_effect=([ ["注文番号"], ["ORDER-3"] ], [["発送期限"]]),
    ), patch.object(
        picking_labels,
        "parse_picking_label_candidates",
        return_value=(list(_picking_payload_for_rows(3).orders), []),
    ):
        payload = picking_labels.load_picking_payload()
    self.assertEqual(dict(state), before)
    self.assertIsInstance(payload.orders, tuple)

def test_apply_picking_payload_preserves_selected_source_rows(self):
    state = picking_labels.st.session_state
    before = dict(state)
    self.addCleanup(lambda: (state.clear(), state.update(before)))
    state["picking_selected_rows"] = {3, 8, 99}
    picking_labels.apply_picking_payload(
        _picking_payload_for_rows(3, 8, 10), preserve_selection=True
    )
    self.assertEqual(state["picking_selected_rows"], {3, 8})

def test_legacy_load_orders_delegates_to_new_load_and_apply(self):
    state = picking_labels.st.session_state
    before = dict(state)
    self.addCleanup(lambda: (state.clear(), state.update(before)))
    payload = _picking_payload_for_rows(3)
    with patch.object(picking_labels, "load_picking_payload", return_value=payload) as loader:
        picking_labels._load_orders()
    loader.assert_called_once_with()
    self.assertIn(3, [order.source_row_number for order in state["picking_orders"]])
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_picking_labels -v
```

Expected: new tests fail because `PickingPayload`, `load_picking_payload`, or `apply_picking_payload` do not exist.

- [ ] **Step 3: Add immutable payload types and explicit copy functions**

Create `refresh_payloads.py`:

```python
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from bot.picking_labels import PickingOrder


@dataclass(frozen=True)
class PendingPayload:
    dataframe: pd.DataFrame
    logs: tuple[str, ...]


@dataclass(frozen=True)
class PickingPayload:
    orders: tuple[PickingOrder, ...]
    warnings: tuple[str, ...]
    diagnostics: Mapping[str, object]


def copy_pending_payload(value: PendingPayload) -> PendingPayload:
    return PendingPayload(value.dataframe.copy(deep=True), tuple(value.logs))


def copy_picking_payload(value: PickingPayload) -> PickingPayload:
    return PickingPayload(
        orders=tuple(copy.deepcopy(value.orders)),
        warnings=tuple(value.warnings),
        diagnostics=copy.deepcopy(dict(value.diagnostics)),
    )
```

- [ ] **Step 4: Split picking reading from session application**

In `features/picking_labels.py`, split the direct session-writing `_load_orders()`
into pure load/apply functions, then retain `_load_orders()` as a compatibility
wrapper for the existing renderer callbacks and existing tests until Task 4 rewires
them:

```python
def load_picking_payload() -> PickingPayload:
    values = load_sheet_values(_picking_source_spreadsheet_id(), _picking_source_sheet_name())
    status_values = load_sheet_values(_shipping_status_spreadsheet_id(), _shipping_status_sheet_name())
    shipping_deadlines = build_shipping_deadline_lookup(status_values)
    orders, warnings = parse_picking_label_candidates(values, shipping_deadlines=shipping_deadlines)
    diagnostics = build_picking_source_diagnostics(values, orders, warnings)
    diagnostics.update(
        {
            "source_spreadsheet_id": _picking_source_spreadsheet_id(),
            "source_sheet": _picking_source_sheet_name(),
            "shipping_status_spreadsheet_id": _shipping_status_spreadsheet_id(),
            "shipping_status_sheet": _shipping_status_sheet_name(),
            "pdf_cjk_font": get_registered_cjk_font_info(),
        }
    )
    return PickingPayload(
        orders=tuple(orders),
        warnings=tuple(warnings),
        diagnostics=diagnostics,
    )


def apply_picking_payload(payload: PickingPayload, *, preserve_selection: bool, loaded_at=None) -> None:
    old_selected = set(st.session_state.get("picking_selected_rows", set()))
    valid_rows = {order.source_row_number for order in payload.orders}
    st.session_state["picking_orders"] = list(payload.orders)
    st.session_state["picking_warnings"] = list(payload.warnings)
    st.session_state["picking_diagnostics"] = dict(payload.diagnostics)
    st.session_state["picking_selected_rows"] = (
        old_selected & valid_rows if preserve_selection else set(valid_rows)
    )
    if loaded_at is not None:
        st.session_state["picking_loaded_at"] = loaded_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_orders() -> None:
    payload = load_picking_payload()
    apply_picking_payload(
        payload,
        preserve_selection=False,
        loaded_at=datetime.now().astimezone(),
    )
```

The existing page renderer's normal render path continues to consume session state;
its manual reload callback and post-generation reload may call this compatibility
wrapper during Task 3. Task 4 replaces those two callbacks with coordinator calls,
but retaining the private wrapper is harmless and keeps older unit tests compatible.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_picking_labels -v
python -m py_compile refresh_payloads.py features/picking_labels.py
git diff --check
git add -- refresh_payloads.py features/picking_labels.py tests/test_picking_labels.py
git commit -m "refactor: separate picking data load from session state"
```

Expected: picking tests PASS; existing selection, parser, QR and generation tests remain green.

### Task 4: Wire automatic, forced, and active-session refreshes

**Files:**
- Create: `safe_logging.py`
- Modify: `refresh_cache.py`
- Modify: `app.py`
- Modify: `bot/sheets.py`
- Modify: `features/picking_labels.py`
- Modify: `tests/test_postal_start_flow.py`
- Modify: `tests/test_postal_ui_v2_app.py`
- Modify: `tests/test_sheets_helpers.py`
- Create: `tests/test_safe_logging.py`
- Test: `tests/test_refresh_cache.py`

- [ ] **Step 1: Write RED source-contract and AppTest cases**

Replace the old “manual only” expectations and add:

```python
def test_main_app_auto_requests_pending_and_picking_snapshots(self):
    source = APP_PATH.read_text(encoding="utf-8")
    self.assertIn('_refresh_source("pending", force=False)', source)
    self.assertIn('_refresh_source("picking", force=False)', source)
    self.assertNotIn("if pending_manual_reload_requested:\n        try:\n            df_pending", source)

def test_manual_reload_forces_pending_without_clearing_last_success(self):
    source = APP_PATH.read_text(encoding="utf-8")
    self.assertIn('_refresh_source("pending", force=True)', source)
    self.assertNotIn('pop("last_pending_df", None)', source)

def test_periodic_fragment_runs_every_twenty_minutes(self):
    source = APP_PATH.read_text(encoding="utf-8")
    self.assertIn('@st.fragment(run_every="20m")', source)
    self.assertIn("allow_dirty_reset=False", source)

def test_periodic_picking_refresh_preserves_user_selection(self):
    source = PICKING_PATH.read_text(encoding="utf-8")
    self.assertIn("preserve_selection=True", source)

def test_pending_edit_widgets_mark_the_snapshot_dirty(self):
    source = APP_PATH.read_text(encoding="utf-8")
    renderer = source[source.index("def _render_postal_pending_v2"):source.index("def _render_main_app")]
    for key_marker in ("name_key", "trans_key", "extra_trans_key", "prc_id_key", "pccc_key", "item_key"):
        self.assertRegex(renderer, rf"{key_marker}[\s\S]*?on_change=_mark_pending_editor_dirty")

def test_snapshot_apply_is_blocked_while_busy_or_dirty(self):
    self.assertFalse(may_apply_pending_snapshot(is_busy=True, editor_dirty=False))
    self.assertFalse(may_apply_pending_snapshot(is_busy=False, editor_dirty=True))
    self.assertTrue(may_apply_pending_snapshot(is_busy=False, editor_dirty=False))
    self.assertTrue(may_apply_pending_snapshot(
        is_busy=False, editor_dirty=True, allow_dirty_reset=True
    ))
    self.assertFalse(may_apply_pending_snapshot(
        is_busy=True, editor_dirty=True, allow_dirty_reset=True
    ))

def test_strict_pending_read_raises_instead_of_returning_empty_snapshot(self):
    with patch("bot.sheets._get_gspread_client", side_effect=PermissionError("denied")):
        with self.assertRaisesRegex(RuntimeError, "pending_read_permission_denied"):
            get_pending_orders(strict=True)

def test_non_strict_pending_read_keeps_legacy_empty_fallback(self):
    with patch("bot.sheets._get_gspread_client", side_effect=PermissionError("denied")):
        self.assertTrue(get_pending_orders(strict=False).empty)
```

In `tests/test_postal_ui_v2_app.py`, run the existing subprocess with patches active around both `app.run()` calls:

```python
from refresh_payloads import PickingPayload

empty_picking = PickingPayload(orders=(), warnings=(), diagnostics={})
with (
    patch("bot.sheets.get_pending_orders", return_value=mock_pending) as pending_loader,
    patch("features.picking_labels.load_picking_payload", return_value=empty_picking) as picking_loader,
    patch("fx_rates.fetch_usd_jpy_rate", return_value=(157.79, "2026-08-07", "mock")),
):
    app.run(timeout=30)
    app.session_state["authenticated"] = True
    app.session_state["user_email"] = "tester@tkrjm.co.jp"
    app.session_state["user_name"] = "Mock Tester"
    app.run(timeout=30)
    assert pending_loader.call_count >= 1
    assert picking_loader.call_count >= 1
```

The patches target dependencies imported by the AppTest script while they are already replaced, so neither formal loader can execute. Do not seed `pending_manual_reload_requested` or `last_pending_df`. Assert the `待製郵便運單` tab contains the synthetic order and `重新讀取` button.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_refresh_cache tests.test_sheets_helpers tests.test_postal_start_flow tests.test_postal_ui_v2_app tests.test_picking_labels -v
```

Expected: FAIL because the coordinator is not connected and manual reload still clears session data.

- [ ] **Step 3: Add one cached coordinator and payload loaders to `app.py`**

Create `safe_logging.py` before importing it from an existing module:

```python
from __future__ import annotations

import re

_ALLOWED_FIELDS = {"count", "seconds", "reason", "error_type", "first_row", "last_row"}
_SAFE_EVENTS = {
    "pending_read_failed",
    "preflight_blocked",
    "job_exception",
    "writeback_initialization_failed",
    "writeback_batch_finished",
}
_SAFE_REASONS = {
    "source_changed",
    "source_indicates_done_target_missing",
    "already_completed",
    "invalid_shipment_role",
    "missing_package_identity",
    "duplicate_package_request",
    "additional_transport_matches_primary",
    "writeback_permission_denied",
    "writeback_network_error",
    "writeback_api_error",
    "writeback_readback_failed",
    "partial_write",
    "tracking_owned_by_other_order",
    "unexpected_second_tracking",
    "incomplete_existing_row",
    "duplicate_batch_pair",
    "invalid_writeback_identity",
}
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_TRACKING_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{9}[A-Z]{2}(?![A-Z0-9])", re.I)
_LONG_NUMBER_TOKEN = re.compile(r"(?<!\d)\d{10,18}(?!\d)")
_EMAIL_TOKEN = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


def safe_log_event(log_cb, event: str, **fields) -> None:
    if log_cb is None:
        return
    if event not in _SAFE_EVENTS:
        raise ValueError("unsafe log event")
    unknown = set(fields) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"unsafe log fields: {','.join(sorted(unknown))}")
    parts = [event]
    for key in sorted(fields):
        value = fields[key]
        if key in {"count", "first_row", "last_row"}:
            value = str(int(value))
        elif key == "seconds":
            value = f"{float(value):.1f}"
        elif key == "error_type":
            value = str(value)
            if not _SAFE_ERROR_TYPE.fullmatch(value):
                raise ValueError("unsafe error type")
        elif key == "reason":
            value = str(value)
            if value not in _SAFE_REASONS:
                raise ValueError("unsafe log reason")
        parts.append(f"{key}={value}")
    log_cb(" ".join(parts))


def redact_operational_log(message: object, *, sensitive_values=()) -> str:
    """Remove known business identifiers before a free-form carrier log is stored."""
    text = " ".join(str(message).replace("\r", " ").replace("\n", " ").split())
    for value in sorted(
        {str(value).strip() for value in sensitive_values if len(str(value).strip()) >= 2},
        key=len,
        reverse=True,
    ):
        text = text.replace(value, "[redacted]")
    text = _TRACKING_TOKEN.sub("[tracking]", text)
    text = _LONG_NUMBER_TOKEN.sub("[number]", text)
    return _EMAIL_TOKEN.sub("[email]", text)
```

Create `tests/test_safe_logging.py` immediately:

```python
import unittest

from safe_logging import redact_operational_log, safe_log_event


class SafeLoggingTests(unittest.TestCase):
    def test_safe_event_allows_only_aggregate_fields(self):
        logs = []
        safe_log_event(logs.append, "preflight_blocked", count=2, reason="source_changed")
        self.assertEqual(logs, ["preflight_blocked count=2 reason=source_changed"])

    def test_safe_event_rejects_identifier_fields_and_values(self):
        with self.assertRaises(ValueError):
            safe_log_event(lambda _: None, "preflight_blocked", order_id="ORDER-SECRET")
        with self.assertRaises(ValueError):
            safe_log_event(lambda _: None, "preflight_blocked", reason="ORDER-SECRET")
        with self.assertRaises(ValueError):
            safe_log_event(lambda _: None, "ORDER-SECRET", reason="source_changed")
        with self.assertRaises(ValueError):
            safe_log_event(lambda _: None, "job_exception", error_type="Receiver Name")

    def test_safe_event_never_serializes_raw_exception(self):
        logs = []
        error = RuntimeError("ORDER-SECRET TRACK-SECRET Receiver Name")
        safe_log_event(logs.append, "job_exception", error_type=type(error).__name__)
        self.assertEqual(logs, ["job_exception error_type=RuntimeError"])

    def test_free_form_carrier_log_redacts_known_and_pattern_identifiers(self):
        safe = redact_operational_log(
            "訂單 ORDER-SECRET 收件人 Receiver Name 完成，單號 EE123456789JP",
            sensitive_values=("ORDER-SECRET", "Receiver Name"),
        )
        for forbidden in ("ORDER-SECRET", "Receiver Name", "EE123456789JP"):
            self.assertNotIn(forbidden, safe)
        self.assertIn("[redacted]", safe)
        self.assertIn("[tracking]", safe)
```

Run `python -m unittest tests.test_safe_logging -v`; expected PASS.

Then change `bot.sheets.get_pending_orders` to:

```python
def get_pending_orders(log_cb=None, *, strict: bool = False, exclude_completed: bool = True):
```

Keep its existing source parsing and base filters. Wrap every current failure return (client creation, source read, target authority read, outer exception) through:

```python
def _pending_read_failure(log_cb, exc, *, strict):
    if isinstance(exc, PermissionError):
        code = "pending_read_permission_denied"
    elif isinstance(exc, (TimeoutError, ConnectionError)):
        code = "pending_read_network"
    else:
        code = "pending_read_failed"
    safe_log_event(log_cb, "pending_read_failed", error_type=type(exc).__name__)
    if strict:
        raise RuntimeError(code) from None
    return pd.DataFrame()
```

Only run `read_completed_order_ids(client)` and `_filter_pending_orders_dataframe(..., completed_ids=...)` when `exclude_completed=True`. When false, return the existing base-filtered/deduplicated source with `_source_row_number` and `_source_fingerprint`, without consulting the completed target. Change the app wrapper to:

```python
def _load_pending_orders(*, strict=False, exclude_completed=True):
    logs = []
    dataframe = get_pending_orders(
        log_cb=logs.append,
        strict=strict,
        exclude_completed=exclude_completed,
    )
    return dataframe, logs
```

Then add imports and these functions to `app.py`:

```python
from datetime import timedelta

from refresh_cache import RefreshResult, SharedRefreshCoordinator
from refresh_payloads import PendingPayload, copy_pending_payload, copy_picking_payload
from features.picking_labels import (
    apply_picking_payload,
    load_picking_payload,
)


@st.cache_resource(show_spinner=False)
def _get_refresh_coordinator(cache_version: str = "2026-08-13-v1") -> SharedRefreshCoordinator:
    return SharedRefreshCoordinator(ttl=timedelta(minutes=20))


def load_pending_payload() -> PendingPayload:
    dataframe, logs = _load_pending_orders(strict=True, exclude_completed=True)
    return PendingPayload(dataframe=dataframe, logs=tuple(logs))


def _refresh_source(source: str, *, force: bool) -> RefreshResult:
    coordinator = _get_refresh_coordinator()
    if source == "pending":
        return coordinator.get(
            source, load_pending_payload, force=force, copier=copy_pending_payload
        )
    if source == "picking":
        return coordinator.get(
            source, load_picking_payload, force=force, copier=copy_picking_payload
        )
    raise ValueError(f"unknown refresh source: {source}")
```

- [ ] **Step 4: Apply snapshots only at safe boundaries**

Add this pure policy function to `refresh_cache.py` and import it in `app.py` and
`tests/test_refresh_cache.py`. Keeping the gate outside `app.py` makes the tests
executable without importing and running the Streamlit app:

```python
def may_apply_pending_snapshot(
    *, is_busy: bool, editor_dirty: bool, allow_dirty_reset: bool = False
) -> bool:
    return not is_busy and (allow_dirty_reset or not editor_dirty)


def _apply_pending_result(
    result: RefreshResult[PendingPayload],
    *,
    is_busy: bool,
    allow_dirty_reset: bool = False,
    job=None,
) -> bool:
    if result.data is None or not may_apply_pending_snapshot(
        is_busy=is_busy,
        editor_dirty=st.session_state.get("pending_editor_dirty", False),
        allow_dirty_reset=allow_dirty_reset,
    ):
        return False
    st.session_state["last_pending_df"] = result.data.dataframe.copy(deep=True)
    st.session_state["last_pending_logs"] = list(result.data.logs)
    st.session_state["last_pending_loaded_at"] = result.status.loaded_at
    st.session_state["pending_refresh_error_code"] = result.status.error_code
    return True
```

Implement `_mark_pending_editor_dirty()` as `st.session_state["pending_editor_dirty"] = True`. Pass `on_change=_mark_pending_editor_dirty` to the v2 name, primary transport, additional transport, PRC ID, PCCC, and item `st.data_editor` widgets at the current renderer positions.

Keep the overwrite rules explicit:

- Automatic and periodic refresh call `_apply_pending_result(..., allow_dirty_reset=False, job=job)`; dirty edits remain on screen while the shared snapshot refreshes in the background.
- The ordinary “重新讀取” button also uses `allow_dirty_reset=False, job=job`; it updates the shared snapshot but does not silently discard in-progress edits.
- “全部恢復預設資料” is an explicit discard intent. It calls `_refresh_source("pending", force=True)`, then `_apply_pending_result(..., allow_dirty_reset=True, job=job)`, and only after that call returns `True` sets `pending_editor_dirty=False` and clears the v2 widget/editor keys. This bypasses only the dirty gate, never the `is_busy` gate.
- Successful job completion obtains a force result and calls `_apply_pending_result(..., allow_dirty_reset=True, job=job)` because the submitted editor state is no longer authoritative; clear the dirty flag only after successful apply.
- Picking periodic apply calls `apply_picking_payload(..., preserve_selection=True)`; manual force refresh and successful label generation use `preserve_selection=False`.

Add direct gate tests in `tests/test_refresh_cache.py`:

```python
def test_dirty_snapshot_is_preserved_during_automatic_refresh(self):
    self.assertFalse(may_apply_pending_snapshot(
        is_busy=False, editor_dirty=True, allow_dirty_reset=False
    ))

def test_explicit_restore_can_replace_dirty_snapshot_when_idle(self):
    self.assertTrue(may_apply_pending_snapshot(
        is_busy=False, editor_dirty=True, allow_dirty_reset=True
    ))

def test_explicit_restore_never_replaces_snapshot_while_job_is_busy(self):
    self.assertFalse(may_apply_pending_snapshot(
        is_busy=True, editor_dirty=True, allow_dirty_reset=True
    ))
```

- [ ] **Step 5: Replace manual-only initial load and add the active-session fragment**

At the start of `_render_main_app()`, call both sources independently so one failure does not block the other. Add:

```python
@st.fragment(run_every="20m")
def _active_refresh_tick(*, is_busy: bool, job) -> None:
    pending = _refresh_source("pending", force=False)
    picking = _refresh_source("picking", force=False)
    pending_changed = pending.status.loaded_at != st.session_state.get("pending_applied_at")
    picking_changed = picking.status.loaded_at != st.session_state.get("picking_applied_at")
    changed = False
    if pending_changed and _apply_pending_result(pending, is_busy=is_busy, job=job):
        st.session_state["pending_applied_at"] = pending.status.loaded_at
        changed = True
    if picking_changed and picking.data is not None:
        apply_picking_payload(
            picking.data,
            preserve_selection=True,
            loaded_at=picking.status.loaded_at,
        )
        st.session_state["picking_applied_at"] = picking.status.loaded_at
        changed = True
    if changed:
        st.rerun()
```

The first full run gets the current job from `_JOB_REGISTRY.get(email)`, passes that
same object to `_apply_pending_result(..., job=job)`, applies both results before
rendering, and stores `*_applied_at`, so the fragment's initial call does not cause a
rerun loop. Invoke `_active_refresh_tick(is_busy=is_busy, job=job)`. Pass the same
current job through the v2 renderer so manual refresh, restore-default, and
post-completion apply calls all use `job=job`. Manual buttons call `force=True` and
never clear prior data before a successful result.

- [ ] **Step 6: Use human-readable refresh status without exposing internals**

Normal UI text is limited to:

```python
st.caption(f"資料更新於 {loaded_at.astimezone().strftime('%H:%M')}")
st.warning("暫時無法取得最新資料，目前顯示上次成功讀取的內容。")
st.error("目前無法取得待製郵便運單資料，請稍後重新讀取。")
```

Do not display TTL seconds, row counts, locks, error objects, or raw API text in the operational pages.

In the existing diagnostic tab, render only a safe two-row summary:

```python
def _refresh_diagnostics_rows():
    coordinator = _get_refresh_coordinator()
    labels = {"pending": "待製郵便運單", "picking": "跨境揀貨單"}
    error_labels = {
        None: "",
        "permission_denied": "權限不足",
        "network": "連線失敗",
        "unexpected": "讀取失敗",
    }
    rows = []
    for source, label in labels.items():
        status = coordinator.status(source)
        rows.append(
            {
                "資料": label,
                "最後成功": status.loaded_at.astimezone().strftime("%Y-%m-%d %H:%M") if status.loaded_at else "尚未成功",
                "最後嘗試": status.last_attempt_at.astimezone().strftime("%Y-%m-%d %H:%M") if status.last_attempt_at else "尚未嘗試",
                "狀態": "使用上次資料" if status.served_stale else ("需要更新" if status.is_stale else "正常"),
                "問題": error_labels.get(status.error_code, "讀取失敗"),
            }
        )
    return rows
```

Never render a raw exception. Detailed picking diagnostics remain inside the existing diagnostic expander rather than moving to the operational page.

- [ ] **Step 7: Run focused and integration tests, then commit**

Run:

```powershell
python -m unittest tests.test_refresh_cache tests.test_safe_logging tests.test_sheets_helpers tests.test_picking_labels tests.test_postal_start_flow tests.test_postal_ui_v2_app -v
python -m py_compile app.py bot/sheets.py features/picking_labels.py refresh_cache.py refresh_payloads.py safe_logging.py
git diff --check
git add -- safe_logging.py refresh_cache.py app.py bot/sheets.py features/picking_labels.py tests/test_safe_logging.py tests/test_refresh_cache.py tests/test_sheets_helpers.py tests/test_postal_start_flow.py tests/test_postal_ui_v2_app.py tests/test_picking_labels.py
git commit -m "feat: auto-refresh shared order snapshots"
```

Expected: all focused tests PASS; AppTest executes rather than silently skipping.

### Task 5: Carry explicit primary/additional shipment evidence

**Files:**
- Modify: `pending_editor.py`
- Modify: `bot/automation.py`
- Modify: `job_control.py`
- Modify: `tests/test_pending_editor.py`
- Modify: `tests/test_automation_helpers.py`
- Modify: `tests/test_job_control.py`

- [ ] **Step 1: Write RED tests for shipment provenance**

Extend the existing duplicate-trans-type test and result record test:

```python
def test_expand_pending_orders_marks_primary_and_additional_roles(self):
    source = pd.DataFrame([{SHIPPING_COL: "EMS", "注文番号(貼上原始資料)": "ORDER-1"}], index=[10])
    expanded = expand_pending_orders_for_trans_types(source, {10: ["航空便"]})
    self.assertEqual(expanded["_shipment_role"].tolist(), ["primary", "additional"])

def test_build_result_record_preserves_shipment_role(self):
    row = {
        "Shipping Name": "Receiver",
        "收件人國家": "UNITED STATES OF AMERICA",
        "內容物1": "Item",
        "申告金額1": "1.00",
        "數量1": "1",
        "_shipment_role": "additional",
    }
    result = _build_result_record(row, order_id="ORDER-1", tracking="TRACK-2")
    self.assertEqual(result["shipment_role"], "additional")

def test_order_states_and_fingerprint_preserve_shipment_role(self):
    frame = pd.DataFrame([
        {"注文番号(貼上原始資料)": "ORDER-1", "TransType": "EMS", "_shipment_role": "primary"},
        {"注文番号(貼上原始資料)": "ORDER-1", "TransType": "AIR", "_shipment_role": "additional"},
    ])
    states = create_order_states(frame, None)
    self.assertEqual([state["shipment_role"] for state in states], ["primary", "additional"])
    self.assertNotEqual(build_batch_fingerprint(frame.iloc[:1], None), build_batch_fingerprint(frame, None))
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pending_editor tests.test_automation_helpers tests.test_job_control -v
```

Expected: FAIL because `_shipment_role` is not produced or propagated.

- [ ] **Step 3: Implement the provenance column and compatibility default**

In `pending_editor.py`:

```python
SHIPMENT_ROLE_COLUMN = "_shipment_role"

def expand_pending_orders_for_trans_types(df, extra_trans_types_by_index):
    rows = []
    for source_index, row in df.iterrows():
        primary_trans_type = _str_value(row.get(SHIPPING_COL, ""))
        primary = row.copy()
        primary[SHIPMENT_ROLE_COLUMN] = "primary"
        rows.append(primary)
        seen = {primary_trans_type}
        for trans_type in extra_trans_types_by_index.get(source_index, []):
            trans_type = _str_value(trans_type)
            if not trans_type or trans_type in seen:
                continue
            additional = row.copy()
            additional[SHIPPING_COL] = trans_type
            additional[SHIPMENT_ROLE_COLUMN] = "additional"
            rows.append(additional)
            seen.add(trans_type)
    if not rows:
        empty = df.copy()
        empty[SHIPMENT_ROLE_COLUMN] = pd.Series(dtype="object")
        return empty
    output_columns = [*df.columns]
    if SHIPMENT_ROLE_COLUMN not in output_columns:
        output_columns.append(SHIPMENT_ROLE_COLUMN)
    return pd.DataFrame(rows, columns=output_columns).reset_index(drop=True)
```

In `bot/automation.py`, extend `_build_result_record`:

```python
"shipment_role": str(row.get("_shipment_role") or "primary").strip().lower(),
```

Reject any value other than `primary` or `additional` before submission; legacy rows default to `primary`.

In `job_control.py`, add `SHIPMENT_ROLE_COLUMNS = ["_shipment_role", "shipment_role"]`; add `shipment_role` to each `create_order_states()` item with legacy default `primary`, and include it in `build_batch_fingerprint()` payload. This makes `job["orders"]` the submitted package-key authority used by Task 6.

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_pending_editor tests.test_automation_helpers tests.test_job_control -v
python -m py_compile pending_editor.py bot/automation.py job_control.py
git diff --check
git add -- pending_editor.py bot/automation.py job_control.py tests/test_pending_editor.py tests/test_automation_helpers.py tests/test_job_control.py
git commit -m "feat: preserve additional shipment provenance"
```

Expected: provenance tests and all existing editor/automation tests PASS.

### Task 6: Make preflight package-aware without weakening legacy safety

**Files:**
- Modify: `safe_logging.py`
- Modify: `bot/automation.py`
- Modify: `bot/sheets.py`
- Modify: `job_control.py`
- Modify: `postal_ui_feedback.py`
- Modify: `app.py`
- Modify: `tests/test_job_control.py`
- Modify: `tests/test_automation_helpers.py`
- Modify: `tests/test_postal_ui_feedback.py`
- Modify: `tests/test_sheets_helpers.py`
- Modify: `tests/test_postal_start_flow.py`
- Modify: `tests/test_safe_logging.py`

- [ ] **Step 1: Write RED tests for package-aware authority and preflight**

In `tests/test_sheets_helpers.py`, add the following minimal authority-only fake and
test. This fake is deliberately local to Task 6 and does not depend on the fuller
writeback fake introduced in Task 7:

```python
class _AuthorityWorksheet:
    def __init__(self, order_ids, tracking_numbers):
        self._columns = {3: list(order_ids), 4: list(tracking_numbers)}

    def col_values(self, column):
        return list(self._columns.get(column, []))


class _AuthoritySpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def get_worksheet_by_id(self, gid):
        self.requested_gid = gid
        return self._worksheet


class _AuthorityClient:
    def __init__(self, worksheet):
        self._spreadsheet = _AuthoritySpreadsheet(worksheet)

    def open_by_key(self, key):
        self.opened_key = key
        return self._spreadsheet


def test_read_completion_authority_keeps_legacy_ids_and_exact_pairs(self):
    worksheet = _AuthorityWorksheet(
        order_ids=["注文番号", "ORDER-1"],
        tracking_numbers=["追跡番号", "EE123456789JP"],
    )
    authority = read_completion_authority(client=_AuthorityClient(worksheet))
    self.assertEqual(authority.legacy_order_ids, frozenset({"ORDER-1"}))
    self.assertEqual(authority.exact_pairs, frozenset({("ORDER-1", "EE123456789JP")}))
```

In `tests/test_job_control.py`, import `CompletionAuthority` from `bot.sheets` and
extend the existing `job_control` import list with `shipment_package_key`; then add
explicit frame builders plus the package-aware assertions:

```python
def _selected_packages(*rows):
    return pd.DataFrame([
        {
            "order_id": "ORDER-1",
            "TransType": trans_type,
            "_shipment_role": role,
            "_source_fingerprint": "source-v1",
        }
        for trans_type, role in rows
    ])


def _latest_order():
    return pd.DataFrame([{
        "order_id": "ORDER-1",
        "TransType": "EMS",
        "_shipment_role": "primary",
        "_source_fingerprint": "source-v1",
    }])


def _completion(*legacy_order_ids):
    return CompletionAuthority(
        legacy_order_ids=frozenset(legacy_order_ids),
        exact_pairs=frozenset(),
    )

def test_preflight_checks_primary_and_additional_as_distinct_packages(self):
    checks = preflight_batch_orders(
        _selected_packages(("EMS", "primary"), ("AIR", "additional")),
        _latest_order(),
        _completion(),
    )
    self.assertEqual([item["status"] for item in checks], ["ready", "ready"])

def test_preflight_accepts_production_japanese_shipping_column(self):
    selected = pd.DataFrame([{
        "注文番号(貼上原始資料)": "ORDER-1",
        "郵局運送方式(複數商品請自行確認是否走小包)": "EMS",
        "_shipment_role": "primary",
        "_source_fingerprint": "source-v1",
    }])
    latest = selected.copy(deep=True)
    checks = preflight_batch_orders(selected, latest, _completion())
    self.assertEqual(checks[0]["status"], "ready")

def test_shipment_package_key_accepts_job_state_mapping(self):
    state = {
        "order_id": "ORDER-1",
        "trans_type": "AIR",
        "shipment_role": "additional",
    }
    self.assertEqual(shipment_package_key(state), ("ORDER-1", "AIR", "additional"))

def test_preflight_allows_explicit_additional_when_legacy_order_completed(self):
    checks = preflight_batch_orders(
        _selected_packages(("AIR", "additional")),
        _latest_order(),
        _completion("ORDER-1"),
    )
    self.assertEqual(checks[0]["status"], "ready")

def test_completed_primary_does_not_block_ready_additional_from_execution(self):
    selected = _selected_packages(("EMS", "primary"), ("AIR", "additional"))
    checks = preflight_batch_orders(selected, _latest_order(), _completion("ORDER-1"))
    ready, already_completed, hard_blocked = partition_preflight_rows(selected, checks)
    self.assertEqual(ready["TransType"].tolist(), ["AIR"])
    self.assertEqual(
        [(item["trans_type"], item["shipment_role"], item["status"]) for item in already_completed],
        [("EMS", "primary", "completed")],
    )
    self.assertEqual(hard_blocked, [])

def test_preflight_blocks_duplicate_additional_package_key(self):
    checks = preflight_batch_orders(
        _selected_packages(("AIR", "additional"), ("AIR", "additional")),
        _latest_order(),
        _completion(),
    )
    self.assertEqual(checks[1]["reason_code"], "duplicate_package_request")
```

Also add the exact UI filtering tests:

```python
def test_primary_success_additional_backfill_failure_keeps_order_visible(self):
    pending = pd.DataFrame([{"注文番号(貼上原始資料)": "ORDER-1"}])
    submitted = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional"},
    ]
    results = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary", "status": "completed"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional", "status": "backfill_failed"},
    ]
    filtered = filter_pending_orders_after_batch(pending, submitted, results)
    self.assertEqual(filtered["注文番号(貼上原始資料)"].tolist(), ["ORDER-1"])


def test_all_packages_completed_hides_order(self):
    pending = pd.DataFrame([{"注文番号(貼上原始資料)": "ORDER-1"}])
    submitted = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional"},
    ]
    results = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary", "status": "completed"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional", "status": "completed"},
    ]
    self.assertTrue(filter_pending_orders_after_batch(pending, submitted, results).empty)


def test_missing_additional_result_keeps_order_visible(self):
    pending = pd.DataFrame([{"注文番号(貼上原始資料)": "ORDER-1"}])
    submitted = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional"},
    ]
    results = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary", "status": "completed"},
    ]
    self.assertEqual(
        filter_pending_orders_after_batch(pending, submitted, results)["注文番号(貼上原始資料)"].tolist(),
        ["ORDER-1"],
    )
```

Extend `tests/test_safe_logging.py` with a source-boundary test:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
AUTOMATION_PATH = ROOT / "bot" / "automation.py"
SHEETS_PATH = ROOT / "bot" / "sheets.py"

def test_postal_flow_source_removes_identifier_and_raw_exception_logs(self):
    app_source = APP_PATH.read_text(encoding="utf-8")
    automation_source = AUTOMATION_PATH.read_text(encoding="utf-8")
    sheet_source = SHEETS_PATH.read_text(encoding="utf-8")
    start_flow = app_source[app_source.index("def _start_job"):app_source.index("def _render_main_app")]
    self.assertNotIn("API 讀到的來源末端注文番号", sheet_source)
    self.assertNotIn("tb.format_exc()", start_flow)
    self.assertNotIn("check['order_id']", start_flow)
    self.assertIn("redact_operational_log(message", start_flow)
    self.assertIn("status_cb=_status", start_flow)
    self.assertNotIn('print(f"[LOG_ERR] {log_err}"', start_flow)
    self.assertIn("safe_message = redact_operational_log", automation_source)
    self.assertIn("logging.info(safe_message)", automation_source)
    self.assertNotIn("logging.info(msg)", automation_source)
    self.assertNotIn("_tb.format_exc()", automation_source)
```

In `tests/test_automation_helpers.py`, add an integration test around the new
module-level `build_safe_automation_logger()` helper. Patch/capture
`bot.automation.logging.info`, send a message containing a submitted order, name,
address and `EE123456789JP`, and assert neither the captured Python log nor the
callback collector contains any of those values. Also assert the callback receives
the same sanitized string that Python logging receives.

Also write the `update_order_status_from_event` tests described in Step 3 now,
including the non-contiguous-index package-key case and the `label_created` followed
by writeback-failure case. They belong to this RED set even though their production
helper is implemented in Step 3.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_safe_logging tests.test_automation_helpers tests.test_job_control tests.test_postal_ui_feedback tests.test_sheets_helpers tests.test_postal_start_flow -v
```

Expected: FAIL because completion is still a `set[str]` and preflight deduplicates by order ID.

- [ ] **Step 3: Add stable authority and package key contracts**

Reuse `safe_logging.py` created and tested in Task 4; do not weaken its event/reason/error-type validation.

In `bot/sheets.py`:

```python
from dataclasses import dataclass


def _get_target_worksheet(*, client):
    spreadsheet = client.open_by_key(TARGET_SHEET_ID)
    return spreadsheet.get_worksheet_by_id(int(TARGET_GID))


@dataclass(frozen=True)
class CompletionAuthority:
    legacy_order_ids: frozenset[str]
    exact_pairs: frozenset[tuple[str, str]]


def read_completion_authority(client=None) -> CompletionAuthority:
    client = client or _get_gspread_client()
    worksheet = _get_target_worksheet(client=client)
    order_ids = worksheet.col_values(3)
    tracking_numbers = worksheet.col_values(4)
    legacy = frozenset(value.strip() for value in order_ids[1:] if value.strip())
    pairs = frozenset(
        (order_id.strip(), tracking.strip())
        for order_id, tracking in zip(order_ids[1:], tracking_numbers[1:])
        if order_id.strip() and tracking.strip()
    )
    return CompletionAuthority(legacy_order_ids=legacy, exact_pairs=pairs)


def read_completed_order_ids(client=None) -> set[str]:
    return set(read_completion_authority(client=client).legacy_order_ids)
```

In `_start_job()`, replace its local imports and preflight reads with:

```python
from bot.sheets import (
    COUNTRY_CODE_MAP,
    backfill_results,
    get_pending_orders,
    read_completion_authority,
)

completion = read_completion_authority()
latest_pending_df = get_pending_orders(
    log_cb=_log,
    strict=True,
    exclude_completed=False,
)
preflight_checks = preflight_batch_orders(
    rows_for_run,
    latest_pending_df,
    completion,
)
```

In `bot/automation.py`, make sanitization happen before both Python logging and the
application callback. Add the module-level helper tested above and extend
`run_automation(..., log_cb=None, status_cb=None)`:

```python
from safe_logging import redact_operational_log


def build_safe_automation_logger(log_cb, *, sensitive_values):
    def _safe_log(message):
        safe_message = redact_operational_log(
            message,
            sensitive_values=sensitive_values,
        )
        logging.info(safe_message)
        if log_cb:
            log_cb(safe_message)
    return _safe_log
```

At the start of `run_automation`, build `sensitive_values` from every submitted
DataFrame cell whose normalized string length is at least four, then bind `_log =
build_safe_automation_logger(log_cb, sensitive_values=sensitive_values)`. Remove the
old nested `_log` and its `logging.info(msg)`. Audit every exception log in this
workflow: log only a safe operation label and `type(exc).__name__`, never `{exc}` or
traceback text; specifically remove `_tb.format_exc()` at the per-order catch.

Do not recover progress by parsing free-form log text. Add `status_cb` events at the
three authoritative carrier transitions:

```python
event_package = {
    "order_id": order_id,
    "trans_type": _get_excel_val(
        row,
        ["郵局運送方式(複數商品請自行確認是否走小包)", "TransType"],
    ),
    "shipment_role": str(row.get("_shipment_role") or "primary").strip().lower(),
}
if status_cb:
    status_cb({
        "event": "order_started",
        **event_package,
    })
# after a tracking-bearing result is appended
if status_cb:
    status_cb({
        "event": "label_created",
        **event_package,
        "tracking": tracking,
    })
# in the per-order exception path
if status_cb:
    status_cb({
        "event": "order_failed",
        **event_package,
        "error_type": type(exc).__name__,
    })
```

In `job_control.py`, add `update_order_status_from_event(job, event)`. It normalizes
the event's `(order_id, trans_type, shipment_role)` with the same package-key rules,
locates that exact submitted package, and applies these non-ambiguous transitions:

- `order_started` → `status="running"`, stage `製單中`;
- `label_created` → keep `status="running"`, stage `運單已產生・回填中`, store tracking;
- `order_failed` → `status="failed"`, stage `未完成` with safe error type only.

`label_created` is deliberately **not** terminal success: only
`mark_results_completed()` after verified B/C/D/J readback may set a package to
success/completed. Reject unknown events and unmatched keys
without logging their payload. Add unit tests for all three events using a
primary/additional two-row job whose DataFrame index is deliberately non-contiguous
(`index=[10, 42]`); assert the additional event updates only the additional package.
Add a regression sequence: send `label_created`, then apply a failed writeback
outcome (and separately simulate the outer exception cleanup); assert the package is
never `success` and ends at `backfill_failed`/`回填待確認` or failed.
In `app.py`, expose it through the existing
retry-protected binding:

```python
update_order_status_from_event = _job_control.update_order_status_from_event
```

Finally, in `_start_job()` pass only already-sanitized text to the persisted job log,
and pass structured status separately:

```python
from safe_logging import redact_operational_log, safe_log_event


def _log(message: str):
    try:
        safe_message = redact_operational_log(
            message,
            sensitive_values=tuple(
                value
                for value in df.astype(object).to_numpy().ravel().tolist()
                if len(str(value or "").strip()) >= 4
            ),
        )
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {safe_message}"
        print(f"[BOT] {entry}", file=sys.stderr, flush=True)
        job["logs"].append(entry)
    except Exception as log_err:
        print(f"[LOG_ERR] {type(log_err).__name__}", file=sys.stderr, flush=True)


def _status(event):
    update_order_status_from_event(job, event)


results = run_automation(
    rows_for_run,
    ...,
    log_cb=_log,
    status_cb=_status,
)
```

`log_cb=_log` is the only carrier-log callback; `status_cb=_status` is the only
progress channel. Do not pass `print`, `job["logs"].append`, or another unsanitized
callback into carrier automation. The automation logger test, app source-boundary
test, and existing status/progress tests must all pass.

The unfiltered preflight read is deliberate: an already completed primary order may still be present in the source and must be available as the source fingerprint authority for a user-requested explicit additional package. Completion blocking is handled by `CompletionAuthority`, not by deleting the source row before preflight.

In `job_control.py`:

```python
PackageKey = tuple[str, str, str]


def shipment_package_key(row) -> PackageKey:
    order_id = _row_value(row, ORDER_ID_COLUMNS)
    trans_type = _row_value(row, TRANS_TYPE_COLUMNS)
    role = _row_value(row, SHIPMENT_ROLE_COLUMNS, "primary").lower()
    if role not in {"primary", "additional"}:
        raise ValueError("invalid_shipment_role")
    return order_id, trans_type, role
```

First extend the existing `_row_value(row, columns, default="")` so it supports both
`pd.Series` and `Mapping` inputs. For a Series, retain the current `column in
row.index` path; for a Mapping, use `column in row` and `row.get(column)`. This is
required because preflight consumes DataFrame rows while refresh/UI completion logic
consumes `job["orders"]` dictionaries. The mapping test above must pass before adding
the UI helpers. Import `Mapping` from `collections.abc` rather than from `typing` for
the runtime `isinstance` check.

```python
def _row_value(row, columns, default=""):
    if isinstance(row, pd.Series):
        available = row.index
    elif isinstance(row, Mapping):
        available = row
    else:
        return default
    for column in columns:
        if column in available:
            value = row.get(column)
            if pd.notna(value) and str(value).strip():
                return str(value).strip()
    return default
```

Extend `_mark_order(...)` with a `shipment_role=""` argument and require both the
transport and role to match when supplied. Update `mark_results_completed()` and
`mark_results_failed()` to pass the result's normalized `shipment_role`. This keeps
primary and additional package states independent even if future transport options
change; legacy calls that omit the role retain their current order-only behavior.

Replace `preflight_batch_orders(selected_df, latest_pending_df, completion)` as one
coherent loop. Do not append `ready` before the latest-source and fingerprint checks:

```python
def preflight_batch_orders(selected_df, latest_pending_df, completion):
    latest_by_order = {}
    if isinstance(latest_pending_df, pd.DataFrame):
        for _, latest_row in latest_pending_df.iterrows():
            latest_order_id = _row_value(latest_row, ORDER_ID_COLUMNS)
            if latest_order_id and latest_order_id not in latest_by_order:
                latest_by_order[latest_order_id] = latest_row

    primary_transport_by_order = {}
    if isinstance(selected_df, pd.DataFrame):
        for _, selected_row in selected_df.iterrows():
            try:
                order_id, trans_type, role = shipment_package_key(selected_row)
            except ValueError:
                continue
            if role == "primary" and order_id and trans_type:
                primary_transport_by_order[order_id] = trans_type

    checks = []
    seen = set()

    def add_check(row_index, order_id, status, reason_code, reason_text):
        checks.append({
            "row_index": row_index,
            "order_id": order_id,
            "status": status,
            "reason_code": reason_code,
            "reason_text": reason_text,
        })

    if not isinstance(selected_df, pd.DataFrame):
        return checks
    for row_index, row in selected_df.iterrows():
        order_id = _row_value(row, ORDER_ID_COLUMNS)
        try:
            package_key = shipment_package_key(row)
        except ValueError:
            add_check(row_index, order_id, "blocked", "invalid_shipment_role", "追加製單識別無效")
            continue
        order_id, trans_type, role = package_key
        if not order_id or not trans_type:
            add_check(row_index, order_id, "blocked", "missing_package_identity", "缺少注文番号或運送方式")
            continue
        if package_key in seen:
            add_check(row_index, order_id, "blocked", "duplicate_package_request", "同一包裹要求重複")
            continue
        seen.add(package_key)
        if role == "primary" and order_id in completion.legacy_order_ids:
            add_check(row_index, order_id, "already_completed", "already_completed", "目標表已有完成紀錄")
            continue
        if role == "additional" and trans_type == primary_transport_by_order.get(order_id):
            add_check(row_index, order_id, "blocked", "additional_transport_matches_primary", "追加運送方式與原始運單相同")
            continue

        latest = latest_by_order.get(order_id)
        if latest is None:
            source_status = _row_value(row, ["製單上傳狀態(請用[未打單]檢視模式)"])
            reason = (
                "source_indicates_done_target_missing"
                if re.fullmatch(r"[A-Z]{2}\d{9}JP", source_status)
                else "source_changed"
            )
            text = (
                "來源狀態已有 tracking、但目標表缺少完成證據，停止自動製單"
                if reason == "source_indicates_done_target_missing"
                else "最新來源資料已不再是可製單狀態，停止以避免誤製"
            )
            add_check(row_index, order_id, reason, reason, text)
            continue
        selected_fingerprint = _row_value(row, ["_source_fingerprint"])
        latest_fingerprint = _row_value(latest, ["_source_fingerprint"])
        if selected_fingerprint and latest_fingerprint and selected_fingerprint != latest_fingerprint:
            add_check(row_index, order_id, "source_changed", "source_changed", "來源資料在選取後已變更，停止以避免使用過期內容")
            continue
        add_check(row_index, order_id, "ready", "", "")
    return checks
```

The precomputation handles an additional row that appears before its primary. An
explicit additional with a non-empty, different transport is not blocked merely
because the order ID exists. Preserve all existing `source_changed` and
`source_indicates_done_target_missing` tests alongside the new package tests.

Add `partition_preflight_rows(selected_df, checks)` to `job_control.py`. It returns
`(ready_df, already_completed_results, hard_blocked_checks)`:

- `ready_df` contains only rows whose matching check status is `ready`, preserving
  their original columns and row order;
- each `already_completed` check becomes a result built from its matching row with
  `order_id`, normalized transport, normalized shipment role, `status="completed"`,
  `reason_code="already_completed"`, and blank tracking;
- every other non-ready status is hard-blocked.

In `_start_job()`, replace the current “any non-ready means return” consumer with:

```python
ready_rows, already_results, hard_blocked_checks = partition_preflight_rows(
    rows_for_run, preflight_checks
)
if hard_blocked_checks:
    # existing safe aggregate blocked handling and terminal failure
    ...
results = list(already_results)
mark_results_completed(job, already_results)
if not ready_rows.empty:
    results.extend(run_automation(ready_rows, ..., log_cb=_log, status_cb=_status))
```

All downstream status logic operates on `results`, but define writeback candidates
exactly as successful results with a non-empty `tracking` value. Pass only that list
to `backfill_results`; do not send `already_results` with blank tracking into the
writeback classifier. Failed automation results still go directly to
`mark_results_failed`.
`already_completed` is an accepted terminal package state, not a batch-wide error.
This makes the normal UI expansion (primary plus explicit additional) executable
when the primary already exists in the legacy target.

For Task 6's intermediate commit, if `writeback_candidates` is empty, skip
`backfill_results`, summarize the already-completed/failed results, and finish the
job directly. If candidates exist, retain the existing all-or-nothing outcome
handling for those candidates only. Task 8 replaces that interim mapping with
per-item outcomes. This keeps every task independently green.

Keep the existing `_start_job()` consumer contract: every check must contain `order_id`, `status`, `reason_code`, and `reason_text`. Replace its blocked log payload with aggregate-only text so it no longer prints full order IDs:

```python
from collections import Counter
from safe_logging import safe_log_event

reason_counts = Counter(check["reason_code"] for check in blocked_checks)
for reason, count in sorted(reason_counts.items()):
    safe_log_event(_log, "preflight_blocked", count=count, reason=reason)
```

Replace `_start_job()` raw exception and traceback logging with:

```python
safe_log_event(_log, "job_exception", error_type=type(e).__name__)
reason_text = "系統處理失敗；詳細識別資料未寫入日誌"
```

Do not print `tb.format_exc()` to stderr in this workflow. In `bot/sheets.py`, remove the source-tail order sample log and replace raw exception strings in `get_pending_orders()` with `safe_log_event(log_cb, "pending_read_failed", error_type=type(exc).__name__)`. Keep counts and elapsed seconds, but never log complete order IDs, tracking numbers, names, row payloads, or exception messages.

- [ ] **Step 4: Make UI filtering depend on all submitted packages**

In `postal_ui_feedback.py` add:

```python
def completed_package_keys(results: list[dict] | None) -> set[tuple[str, str, str]]:
    return {
        (
            str(item.get("order_id") or "").strip(),
            str(item.get("trans_type") or "").strip(),
            str(item.get("shipment_role") or "primary").strip().lower(),
        )
        for item in (results or [])
        if str(item.get("status") or "").lower() in {"success", "completed"}
    }


def fully_completed_order_ids(submitted_orders, results) -> set[str]:
    submitted_by_order = {}
    for item in submitted_orders or []:
        key = shipment_package_key(item)
        submitted_by_order.setdefault(key[0], set()).add(key)
    completed_keys = completed_package_keys(results)
    return {
        order_id
        for order_id, submitted_keys in submitted_by_order.items()
        if submitted_keys and submitted_keys <= completed_keys
    }


def filter_pending_orders_after_batch(pending, submitted_orders, results):
    completed_ids = fully_completed_order_ids(submitted_orders, results)
    if not completed_ids:
        return pending.copy(deep=True)
    order_column = "注文番号(貼上原始資料)"
    return pending.loc[
        ~pending[order_column].astype(str).str.strip().isin(completed_ids)
    ].copy(deep=True)


def preserve_incomplete_submitted_orders(existing, refreshed, submitted_orders, results):
    submitted_ids = {
        shipment_package_key(item)[0]
        for item in (submitted_orders or [])
        if shipment_package_key(item)[0]
    }
    protected_ids = submitted_ids - fully_completed_order_ids(submitted_orders, results)
    if not protected_ids or not isinstance(existing, pd.DataFrame) or existing.empty:
        return refreshed.copy(deep=True)
    order_column = "注文番号(貼上原始資料)"
    protected = existing.loc[
        existing[order_column].astype(str).str.strip().isin(protected_ids)
    ]
    combined = pd.concat([refreshed, protected], ignore_index=True)
    return combined.drop_duplicates(subset=[order_column], keep="first").reset_index(drop=True)
```

Use `filter_pending_orders_after_batch(df_pending, job["orders"], job["results"])`
when removing pending rows and `fully_completed_order_ids(job["orders"],
job["results"])` when clearing selection. Extend `_apply_pending_result(..., job=None)`:
before assigning a refreshed DataFrame, call
`preserve_incomplete_submitted_orders(current_session_df, refreshed_df,
job["orders"], job["results"])`. Implement the assignment as:

```python
refreshed_df = result.data.dataframe.copy(deep=True)
if job:
    refreshed_df = preserve_incomplete_submitted_orders(
        st.session_state.get("last_pending_df", pd.DataFrame()),
        refreshed_df,
        job.get("orders"),
        job.get("results"),
    )
st.session_state["last_pending_df"] = refreshed_df
```

This session-level merge is necessary because the
shared Google Sheet loader only knows legacy order-level completion and cannot infer
an unfilled additional-package intent. Do not hide an order when any submitted
package is missing from results or is `backfill_failed`, including after a periodic
or forced refresh.

Add this exact regression test to `tests/test_postal_ui_feedback.py`:

```python
def test_refresh_does_not_hide_primary_success_with_additional_conflict(self):
    existing = pd.DataFrame([{"注文番号(貼上原始資料)": "ORDER-1"}])
    refreshed = existing.iloc[0:0].copy()
    submitted = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional"},
    ]
    results = [
        {"order_id": "ORDER-1", "trans_type": "EMS", "shipment_role": "primary", "status": "completed"},
        {"order_id": "ORDER-1", "trans_type": "AIR", "shipment_role": "additional", "status": "backfill_failed"},
    ]
    merged = preserve_incomplete_submitted_orders(existing, refreshed, submitted, results)
    self.assertEqual(merged["注文番号(貼上原始資料)"].tolist(), ["ORDER-1"])
```

Add a wiring contract to `tests/test_postal_start_flow.py` so the pure regression
cannot pass while production callers omit the job:

```python
def test_all_pending_snapshot_apply_paths_pass_current_job(self):
    source = APP_PATH.read_text(encoding="utf-8")
    self.assertIn("def _active_refresh_tick(*, is_busy: bool, job)", source)
    self.assertIn("_active_refresh_tick(is_busy=is_busy, job=job)", source)
    self.assertIn("preserve_incomplete_submitted_orders(", source)
    for marker in (
        "allow_dirty_reset=False, job=job",
        "allow_dirty_reset=True, job=job",
    ):
        self.assertIn(marker, source)
```

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_safe_logging tests.test_automation_helpers tests.test_job_control tests.test_postal_ui_feedback tests.test_sheets_helpers tests.test_postal_start_flow -v
python -m py_compile app.py bot/automation.py bot/sheets.py job_control.py postal_ui_feedback.py
git diff --check
git add -- safe_logging.py app.py bot/automation.py bot/sheets.py job_control.py postal_ui_feedback.py tests/test_safe_logging.py tests/test_automation_helpers.py tests/test_job_control.py tests/test_postal_ui_feedback.py tests/test_sheets_helpers.py tests/test_postal_start_flow.py
git commit -m "fix: preserve package-level postal preflight state"
```

Expected: package-aware tests PASS and legacy order-level completion behavior remains covered by compatibility tests.

### Task 7: Build a realistic fake worksheet and robust writeback engine

**Files:**
- Create: `tests/fake_gspread.py`
- Modify: `bot/sheets.py`
- Modify: `tests/test_sheets_helpers.py`
- Modify: `tests/test_postal_mock_e2e.py`

- [ ] **Step 1: Create the shared fake and write RED tests for grid capacity**

`tests/fake_gspread.py` must implement this usable contract:

```python
import copy
import re


class FakeWorksheet:
    def __init__(self, rows, *, gid=465870894, row_count=None, formula_cells=None):
        self.id = gid
        self.title = "target"
        self.rows = [list(row) + [""] * (10 - len(row)) for row in rows]
        self.row_count = row_count if row_count is not None else max(len(self.rows), 1)
        self.calls = []
        self.fail_add_rows = None
        self.fail_batch_update = None
        self.drop_columns = set()
        self.drop_rows = set()
        self.stale_reads_remaining = 0
        self._stale_rows = None
        self.formula_cells = dict(formula_cells or {})

    def col_values(self, column):
        source = self._stale_rows if self.stale_reads_remaining and self._stale_rows is not None else self.rows
        values = [row[column - 1] for row in source]
        while values and values[-1] == "":
            values.pop()
        if column == 10 and self.stale_reads_remaining:
            self.stale_reads_remaining -= 1
        return values

    def add_rows(self, count):
        self.calls.append(("add_rows", count))
        if self.fail_add_rows:
            raise self.fail_add_rows
        self.row_count += count

    def get(self, a1_range, value_render_option=None):
        if a1_range != "B:J":
            raise ValueError(f"unsupported grid range: {a1_range}")
        rendered = []
        for row_number in range(1, len(self.rows) + 1):
            values = []
            for column_number in range(2, 11):
                if value_render_option == "FORMULA" and (row_number, column_number) in self.formula_cells:
                    values.append(self.formula_cells[(row_number, column_number)])
                else:
                    values.append(self.rows[row_number - 1][column_number - 1])
            rendered.append(values)
        return rendered

    def batch_update(self, batch, value_input_option=None):
        self.calls.append(("batch_update", batch, value_input_option))
        if self.fail_batch_update:
            raise self.fail_batch_update
        self._stale_rows = copy.deepcopy(self.rows)
        for update in batch:
            self._apply_a1_update(update["range"], update["values"])

    def _apply_a1_update(self, a1_range, values):
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", a1_range)
        if not match or match.group(2) != match.group(4):
            raise ValueError(f"unsupported range: {a1_range}")
        start_col = self._column_number(match.group(1))
        end_col = self._column_number(match.group(3))
        row_number = int(match.group(2))
        if row_number > self.row_count:
            raise ValueError("grid_limit")
        if row_number in self.drop_rows:
            return
        while len(self.rows) < row_number:
            self.rows.append([""] * 10)
        for offset, value in enumerate(values[0]):
            column_number = start_col + offset
            if column_number > end_col:
                raise ValueError("too_many_values")
            column_letter = self._column_letter(column_number)
            if column_letter not in self.drop_columns:
                self.rows[row_number - 1][column_number - 1] = value

    @staticmethod
    def _column_number(label):
        value = 0
        for character in label:
            value = value * 26 + ord(character) - ord("A") + 1
        return value

    @staticmethod
    def _column_letter(number):
        label = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            label = chr(ord("A") + remainder) + label
        return label


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self.worksheet = worksheet

    def get_worksheet_by_id(self, gid):
        if gid != self.worksheet.id:
            raise KeyError(gid)
        return self.worksheet


class FakeClient:
    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.opened_keys = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return FakeSpreadsheet(self.worksheet)
```

Add exact capacity tests:

```python
def test_last_used_row_considers_b_c_d_and_j(self):
    columns = {"B": ["B", ""], "C": ["C"] + [""] * 6 + ["ORDER"], "D": ["D"] + [""] * 6 + ["TRACK"], "J": ["J"] + [""] * 7 + ["US"]}
    self.assertEqual(_last_used_writeback_row(columns, occupied_formula_rows=set()), 9)


def test_last_used_row_reserves_formula_empty_string_row(self):
    columns = {"B": ["B", "Receiver", ""], "C": ["C"], "D": ["D"], "J": ["J"]}
    self.assertEqual(_last_used_writeback_row(columns, occupied_formula_rows={3}), 3)


def test_read_writeback_grid_detects_formula_with_empty_display(self):
    worksheet = FakeWorksheet(
        [["", "收件人", "注文番号", "追跡番号", "", "", "", "", "", "國家"], ["", "", "", "", "", "", "", "", "", ""]],
        formula_cells={(2, 2): '=IF(A2="","",A2)'},
    )
    grid = _read_writeback_grid(worksheet)
    self.assertEqual(grid.columns["B"][1], "")
    self.assertEqual(grid.occupied_formula_rows, frozenset({2}))


def test_backfill_expands_grid_before_batch_update(self):
    client = self.make_client([], row_count=1)
    outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client, readback_delay_seconds=0)
    self.assertTrue(outcome["ok"])
    self.assertEqual(client.worksheet.calls[0], ("add_rows", 1))
    self.assertEqual(client.worksheet.calls[1][0], "batch_update")


def test_add_rows_failure_is_not_reported_success(self):
    client = self.make_client([], row_count=1)
    client.worksheet.fail_add_rows = PermissionError("denied")
    outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client)
    self.assertFalse(outcome["ok"])
    self.assertEqual(outcome["items"][0]["status"], "write_failed")
    self.assertFalse(any(call[0] == "batch_update" for call in client.worksheet.calls))
```

- [ ] **Step 2: Run the capacity test and verify RED**

Run:

```powershell
python -m unittest tests.test_sheets_helpers.SheetsHelperTests.test_backfill_expands_grid_before_batch_update -v
```

Expected: FAIL because `backfill_results` never calls `add_rows`.

- [ ] **Step 3: Add four-column helpers and capacity expansion**

In `bot/sheets.py` implement:

```python
@dataclass(frozen=True)
class WritebackGrid:
    columns: dict[str, list[str]]
    occupied_formula_rows: frozenset[int]


def _read_writeback_grid(worksheet) -> WritebackGrid:
    columns = {
        column: worksheet.col_values(index)
        for column, index in {"B": 2, "C": 3, "D": 4, "J": 10}.items()
    }
    formula_grid = worksheet.get("B:J", value_render_option="FORMULA")
    relevant_offsets = {0, 1, 2, 8}
    occupied = frozenset(
        row_number
        for row_number, row in enumerate(formula_grid, start=1)
        if any(
            offset < len(row) and str(row[offset]).startswith("=")
            for offset in relevant_offsets
        )
    )
    return WritebackGrid(columns=columns, occupied_formula_rows=occupied)


def _last_used_writeback_row(columns, *, occupied_formula_rows) -> int:
    value_rows = {
            index
            for values in columns.values()
            for index, value in enumerate(values, start=1)
            if str(value).strip()
    }
    return max(value_rows | set(occupied_formula_rows) | {1})


def _ensure_row_capacity(worksheet, required_last_row: int) -> None:
    missing = required_last_row - int(worksheet.row_count)
    if missing > 0:
        worksheet.add_rows(missing)
```

Resolve the target directly through `get_worksheet_by_id(TARGET_GID)` rather than enumerating worksheets.

- [ ] **Step 4: Add RED tests for idempotency, explicit additional shipments, conflicts, and readback**

Add a small result factory and the concrete cases below. `make_client()` returns the `FakeClient` around a `FakeWorksheet` and exposes the worksheet as `client.worksheet`.

```python
def result(order_id, tracking, *, role="primary", trans_type="EMS", name="Receiver"):
    return {
        "name": name,
        "order_id": order_id,
        "tracking": tracking,
        "country_raw": "US",
        "trans_type": trans_type,
        "shipment_role": role,
    }


def make_client(self, data_rows, *, row_count=None):
    header = ["", "收件人", "注文番号", "追跡番号", "", "", "", "", "", "國家"]
    worksheet = FakeWorksheet(
        [header, *data_rows],
        row_count=row_count if row_count is not None else len(data_rows) + 1,
    )
    return FakeClient(worksheet)


def test_exact_order_tracking_pair_is_idempotent(self):
    client = self.make_client([["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]])
    outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client)
    self.assertEqual(outcome["existing"], 1)
    self.assertEqual(outcome["written"], 0)
    self.assertEqual(outcome["items"][0]["status"], "already_present")
    self.assertFalse(any(call[0] == "batch_update" for call in client.worksheet.calls))


def test_explicit_additional_tracking_appends_new_row(self):
    client = self.make_client([["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]])
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-2", role="additional", trans_type="AIR")],
        client=client,
        readback_delay_seconds=0,
    )
    self.assertEqual(outcome["written"], 1)
    self.assertEqual(outcome["items"][0]["status"], "written")
    self.assertEqual(outcome["items"][0]["row"], 3)


def test_same_batch_primary_and_additional_each_append(self):
    client = self.make_client([], row_count=2)
    outcome = backfill_results(
        [
            result("ORDER-1", "TRACK-1"),
            result("ORDER-1", "TRACK-2", role="additional", trans_type="AIR"),
        ],
        client=client,
        readback_delay_seconds=0,
    )
    self.assertEqual([item["status"] for item in outcome["items"]], ["written", "written"])
    self.assertEqual([item["row"] for item in outcome["items"]], [2, 3])
    self.assertEqual(outcome["written"], 2)


def test_unexpected_second_tracking_requires_manual_review(self):
    client = self.make_client([["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]])
    outcome = backfill_results([result("ORDER-1", "TRACK-2")], client=client)
    self.assertEqual(outcome["items"][0]["status"], "manual_review")
    self.assertEqual(outcome["items"][0]["reason_code"], "unexpected_second_tracking")
    self.assertFalse(any(call[0] == "batch_update" for call in client.worksheet.calls))


def test_same_tracking_for_different_order_is_conflict(self):
    client = self.make_client([["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]])
    outcome = backfill_results([result("ORDER-2", "TRACK-1")], client=client)
    self.assertEqual(outcome["items"][0]["status"], "conflict")
    self.assertEqual(outcome["items"][0]["reason_code"], "tracking_owned_by_other_order")


def test_readback_verifies_all_four_columns(self):
    client = self.make_client([], row_count=2)
    client.worksheet.drop_columns.add("J")
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-1")], client=client, readback_attempts=1
    )
    self.assertEqual(outcome["items"][0]["status"], "partial_write")
    self.assertFalse(outcome["ok"])


def test_readback_retries_until_values_are_visible(self):
    sleeps = []
    client = self.make_client([], row_count=2)
    client.worksheet.stale_reads_remaining = 2
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-1")],
        client=client,
        readback_attempts=3,
        readback_delay_seconds=0.01,
        sleep_fn=sleeps.append,
    )
    self.assertEqual(outcome["items"][0]["status"], "written")
    self.assertEqual(sleeps, [0.01, 0.01])


def test_readback_retry_exhaustion_is_failure(self):
    client = self.make_client([], row_count=2)
    client.worksheet.stale_reads_remaining = 99
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-1")],
        client=client,
        readback_attempts=3,
        readback_delay_seconds=0,
    )
    self.assertFalse(outcome["ok"])
    self.assertEqual(outcome["items"][0]["reason_code"], "writeback_readback_failed")


def test_one_conflict_does_not_erase_verified_success(self):
    client = self.make_client([["", "Receiver", "ORDER-X", "TRACK-X", "", "", "", "", "", "US"]], row_count=3)
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-1"), result("ORDER-2", "TRACK-X")],
        client=client,
        readback_delay_seconds=0,
    )
    self.assertEqual([item["status"] for item in outcome["items"]], ["written", "conflict"])
    self.assertEqual(outcome["written"], 1)
    self.assertFalse(outcome["ok"])


def test_batch_update_partial_write_reports_per_item_outcomes(self):
    client = self.make_client([], row_count=3)
    client.worksheet.drop_rows.add(3)
    outcome = backfill_results(
        [result("ORDER-1", "TRACK-1"), result("ORDER-2", "TRACK-2")],
        client=client,
        readback_attempts=1,
    )
    self.assertEqual(outcome["items"][0]["status"], "written")
    self.assertIn(outcome["items"][1]["status"], {"partial_write", "write_failed"})


def test_initial_sheet_read_failure_returns_item_outcomes(self):
    client = self.make_client([], row_count=2)
    client.worksheet.col_values = lambda column: (_ for _ in ()).throw(PermissionError("ORDER-SECRET"))
    logs = []
    outcome = backfill_results(
        [result("ORDER-SECRET", "TRACK-SECRET")],
        client=client,
        log_cb=logs.append,
    )
    self.assertFalse(outcome["ok"])
    self.assertEqual(outcome["items"][0]["status"], "write_failed")
    self.assertEqual(outcome["items"][0]["reason_code"], "writeback_permission_denied")
    self.assertNotIn("ORDER-SECRET", "\n".join(logs))
    self.assertNotIn("TRACK-SECRET", "\n".join(logs))
```

Each case asserts the per-input status, safe reason, target row, aggregate counts, and whether a write call was allowed.

- [ ] **Step 4b: Run all classification/readback tests and verify RED**

Run before writing the Step 5 implementation:

```powershell
python -m unittest tests.test_sheets_helpers tests.test_postal_mock_e2e -v
```

Expected: the new idempotency, additional-package, conflict, partial-write,
initial-read-failure, and delayed-readback tests fail for the intended missing
classification/readback behavior; the Step 1 capacity tests remain green.

- [ ] **Step 5: Implement classification, write, and readback retry with a compatible return shape**

Keep the existing positional arguments and add injectable test controls. Implement the following helpers and orchestration; country conversion must call the repo's existing `resolve_country_code(country_raw, COUNTRY_CODE_MAP)`:

```python
from safe_logging import safe_log_event

def _safe_sheet_error_code(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "writeback_permission_denied"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "writeback_network_error"
    return "writeback_api_error"


def _column_rows(columns: dict[str, list[str]]):
    row_count = max((len(values) for values in columns.values()), default=0)
    for row_number in range(2, row_count + 1):
        yield row_number, {
            column: str(values[row_number - 1]).strip() if row_number <= len(values) else ""
            for column, values in columns.items()
        }


def _outcome_item(index, result, *, status, reason_code, row=None):
    return {
        "input_index": index,
        "order_id": str(result.get("order_id") or "").strip(),
        "tracking": str(result.get("tracking") or "").strip(),
        "trans_type": str(result.get("trans_type") or "").strip(),
        "shipment_role": str(result.get("shipment_role") or "primary").strip().lower(),
        "status": status,
        "reason_code": reason_code,
        "row": row,
    }


def _classify_writeback_records(results, columns):
    existing_rows = list(_column_rows(columns))
    exact_pairs = {
        (values["C"], values["D"]): row_number
        for row_number, values in existing_rows
        if values["C"] and values["D"]
    }
    tracking_owners = {
        values["D"]: values["C"]
        for _, values in existing_rows
        if values["C"] and values["D"]
    }
    tracking_by_order = {}
    partial_orders = set()
    for _, values in existing_rows:
        if values["C"] and values["D"]:
            tracking_by_order.setdefault(values["C"], set()).add(values["D"])
        elif values["C"]:
            partial_orders.add(values["C"])

    candidates = []
    immediate = []
    planned_pairs = set()
    planned_tracking_owners = dict(tracking_owners)
    for index, raw in enumerate(results):
        result = dict(raw)
        order_id = str(result.get("order_id") or "").strip()
        tracking = str(result.get("tracking") or "").strip()
        role = str(result.get("shipment_role") or "primary").strip().lower()
        result["order_id"] = order_id
        result["tracking"] = tracking
        result["shipment_role"] = role
        pair = (order_id, tracking)
        if not order_id or not tracking or role not in {"primary", "additional"}:
            immediate.append(_outcome_item(index, result, status="manual_review", reason_code="invalid_writeback_identity"))
        elif pair in exact_pairs:
            immediate.append(_outcome_item(index, result, status="already_present", reason_code="exact_pair_exists", row=exact_pairs[pair]))
        elif tracking in planned_tracking_owners and planned_tracking_owners[tracking] != order_id:
            immediate.append(_outcome_item(index, result, status="conflict", reason_code="tracking_owned_by_other_order"))
        elif order_id in partial_orders:
            immediate.append(_outcome_item(index, result, status="manual_review", reason_code="incomplete_existing_row"))
        elif tracking_by_order.get(order_id) and role != "additional":
            immediate.append(_outcome_item(index, result, status="manual_review", reason_code="unexpected_second_tracking"))
        elif pair in planned_pairs:
            immediate.append(_outcome_item(index, result, status="manual_review", reason_code="duplicate_batch_pair"))
        else:
            result["input_index"] = index
            candidates.append(result)
            planned_pairs.add(pair)
            planned_tracking_owners[tracking] = order_id
            tracking_by_order.setdefault(order_id, set()).add(tracking)
    return candidates, immediate


def _verify_writeback_rows(
    worksheet,
    expected_by_row,
    *,
    attempts,
    delay_seconds,
    sleep_fn,
):
    final = {row_number: "writeback_readback_failed" for row_number in expected_by_row}
    for attempt in range(attempts):
        grid = _read_writeback_grid(worksheet)
        columns = grid.columns
        for row_number, expected in expected_by_row.items():
            actual = tuple(
                str(columns[column][row_number - 1]).strip()
                if row_number <= len(columns[column])
                else ""
                for column in ("B", "C", "D", "J")
            )
            matches = sum(actual_value == expected_value for actual_value, expected_value in zip(actual, expected))
            final[row_number] = "verified" if matches == 4 else (
                "partial_write" if matches else "writeback_readback_failed"
            )
        if all(reason == "verified" for reason in final.values()):
            break
        if attempt + 1 < attempts:
            sleep_fn(delay_seconds)
    return final


def backfill_results(
    results: list[dict],
    log_cb=None,
    *,
    client=None,
    sleep_fn=time.sleep,
    readback_attempts: int = 3,
    readback_delay_seconds: float = 0.5,
) -> dict:
    def all_failed(reason_code):
        items = [
            _outcome_item(
                index,
                result,
                status="write_failed",
                reason_code=reason_code,
            )
            for index, result in enumerate(results)
        ]
        return {
            "ok": False,
            "written": 0,
            "existing": 0,
            "failed": [reason_code] * len(items),
            "error": reason_code,
            "items": items,
        }

    if not results:
        return {"ok": True, "written": 0, "existing": 0, "failed": [], "error": "", "items": []}

    try:
        client = client or _get_gspread_client()
        worksheet = _get_target_worksheet(client=client)
        grid = _read_writeback_grid(worksheet)
        classified, immediate_items = _classify_writeback_records(results, grid.columns)
    except Exception as exc:
        reason_code = _safe_sheet_error_code(exc)
        safe_log_event(
            log_cb,
            "writeback_initialization_failed",
            count=len(results),
            reason=reason_code,
        )
        return all_failed(reason_code)

    next_row = _last_used_writeback_row(
        grid.columns,
        occupied_formula_rows=grid.occupied_formula_rows,
    ) + 1

    expected_by_row = {}
    for offset, candidate in enumerate(classified):
        row_number = next_row + offset
        candidate["row"] = row_number
        expected_by_row[row_number] = (
            candidate["name"],
            candidate["order_id"],
            candidate["tracking"],
            resolve_country_code(candidate["country_raw"], COUNTRY_CODE_MAP)
            or str(candidate["country_raw"]).strip(),
        )

    if expected_by_row:
        try:
            _ensure_row_capacity(worksheet, max(expected_by_row))
            updates = []
            for row_number, values in expected_by_row.items():
                name, order_id, tracking, country_code = values
                updates.extend(
                    [
                        {"range": f"B{row_number}:D{row_number}", "values": [[name, order_id, tracking]]},
                        {"range": f"J{row_number}:J{row_number}", "values": [[country_code]]},
                    ]
                )
            worksheet.batch_update(updates, value_input_option="USER_ENTERED")
            verified = _verify_writeback_rows(
                worksheet,
                expected_by_row,
                attempts=readback_attempts,
                delay_seconds=readback_delay_seconds,
                sleep_fn=sleep_fn,
            )
        except Exception as exc:
            safe_code = _safe_sheet_error_code(exc)
            verified = {row_number: safe_code for row_number in expected_by_row}
    else:
        verified = {}

    items = list(immediate_items)
    for candidate in classified:
        reason = verified[candidate["row"]]
        status = "written" if reason == "verified" else (
            "partial_write" if reason == "partial_write" else "write_failed"
        )
        items.append(
            {
                "input_index": candidate["input_index"],
                "order_id": candidate["order_id"],
                "tracking": candidate["tracking"],
                "trans_type": candidate["trans_type"],
                "shipment_role": candidate["shipment_role"],
                "status": status,
                "reason_code": reason,
                "row": candidate["row"],
            }
        )
    items.sort(key=lambda item: item["input_index"])
    written = sum(item["status"] == "written" for item in items)
    existing = sum(item["status"] == "already_present" for item in items)
    failed = [item["reason_code"] for item in items if item["status"] not in {"written", "already_present"}]
    return {
        "ok": not failed,
        "written": written,
        "existing": existing,
        "failed": failed,
        "error": "" if not failed else "writeback_not_fully_verified",
        "items": items,
    }
```

The returned object must always contain:

```python
{
    "ok": bool,
    "written": int,
    "existing": int,
    "failed": list[str],
    "error": str,
    "items": [
        {
            "input_index": int,
            "order_id": str,
            "tracking": str,
            "trans_type": str,
            "shipment_role": str,
            "status": "written" | "already_present" | "manual_review" | "conflict" | "write_failed" | "partial_write",
            "reason_code": str,
            "row": int | None,
        }
    ],
}
```

Classification order is exact pair → same tracking/different order conflict → same order/different tracking role check → incomplete existing row manual review → append candidate. Only explicit `additional` results may append a second tracking for one order. Build one B:D update and one J update per contiguous append group, expand grid first, then verify all four columns with at most three reads. Logs contain only row ranges, counts, reason codes, and safe error classes.

- [ ] **Step 6: Replace duplicated fake worksheets and run focused E2E tests**

Use `tests.fake_gspread.FakeWorksheet/FakeClient` in both sheet-helper and postal mock E2E tests. Ensure a readback failure never leads to `completed`.

Run:

```powershell
python -m unittest tests.test_sheets_helpers tests.test_postal_mock_e2e -v
python -m py_compile bot/sheets.py tests/fake_gspread.py
git diff --check
git add -- bot/sheets.py tests/fake_gspread.py tests/test_sheets_helpers.py tests/test_postal_mock_e2e.py
git commit -m "fix: make postal sheet writeback verifiable"
```

Expected: all grid, idempotency, additional shipment, conflict, partial write, and delayed readback cases PASS.

### Task 8: Apply writeback outcomes per package and present truthful status

**Files:**
- Modify: `app.py`
- Modify: `job_control.py`
- Modify: `postal_ui_feedback.py`
- Modify: `tests/test_job_control.py`
- Modify: `tests/test_postal_ui_feedback.py`
- Modify: `tests/test_postal_mock_e2e.py`

- [ ] **Step 1: Write RED tests for mixed outcomes and user-facing wording**

In `tests/test_job_control.py`, extend the existing `job_control` import list with
`apply_writeback_outcome`, `create_order_states`, `mark_results_completed`,
`mark_results_failed`, and `writeback_retry_candidates`. Then add a concrete
mixed-result fixture and tests; every helper used here is defined in the snippet:

```python
def _mixed_writeback_job():
    frame = pd.DataFrame([
        {"order_id": "ORDER-1", "TransType": "EMS", "_shipment_role": "primary"},
        {"order_id": "ORDER-1", "TransType": "AIR", "_shipment_role": "additional"},
    ])
    results = [
        {
            "order_id": "ORDER-1", "tracking": "EE123456789JP",
            "trans_type": "EMS", "shipment_role": "primary", "status": "success",
        },
        {
            "order_id": "ORDER-1", "tracking": "EE987654321JP",
            "trans_type": "AIR", "shipment_role": "additional", "status": "success",
        },
    ]
    return {"status": "running", "orders": create_order_states(frame, None), "results": results}


def test_one_verified_package_completes_while_conflict_waits_for_review(self):
    job = _mixed_writeback_job()
    terminal = apply_writeback_outcome(job, job["results"], {
        "ok": False,
        "items": [
            {"input_index": 0, "status": "written", "reason_code": "", "row": 2},
            {
                "input_index": 1,
                "status": "conflict",
                "reason_code": "tracking_owned_by_other_order",
                "row": 3,
            },
        ],
    })
    self.assertEqual(job["results"][0]["status"], "completed")
    self.assertEqual(job["results"][1]["status"], "backfill_failed")
    self.assertEqual(job["results"][1]["reason_code"], "writeback_tracking_conflict")
    self.assertEqual([item["status"] for item in job["orders"]], ["success", "failed"])
    self.assertEqual(terminal, "partial_failure")
    self.assertEqual(job["status"], "partial_failure")

def test_backfill_failed_copy_says_label_exists(self):
    summary = summarize_batch_results([
        {"status": "backfill_failed", "order_id": "ORDER-1", "reason_code": "writeback_failed"}
    ])
    message = "\n".join(summary["failure_alerts"])
    self.assertIn("運單已產生，但資料回填未完成", message)
    self.assertNotIn("未製單", message)

def test_writeback_retry_candidates_require_existing_tracking(self):
    results = [
        {"status": "backfill_failed", "order_id": "ORDER-1", "tracking": "TRACK-1"},
        {"status": "backfill_failed", "order_id": "ORDER-2", "tracking": ""},
        {"status": "failed", "order_id": "ORDER-3", "tracking": "TRACK-3"},
    ]
    self.assertEqual(writeback_retry_candidates(results), [results[0]])

def test_retry_success_recomputes_order_and_job_terminal_state(self):
    job = _mixed_writeback_job()
    failed = job["results"][1]
    job["results"][0]["status"] = "completed"
    mark_results_completed(job, [job["results"][0]])
    failed.update({"status": "backfill_failed", "reason_code": "writeback_failed"})
    mark_results_failed(job, [failed])
    terminal = apply_writeback_outcome(job, [failed], {
        "ok": True,
        "items": [
            {"input_index": 0, "status": "already_present", "reason_code": "", "row": 3},
        ],
    })
    self.assertEqual(failed["status"], "completed")
    self.assertEqual([item["status"] for item in job["orders"]], ["success", "success"])
    self.assertTrue(job["pending_refresh_needed"])
    self.assertEqual(terminal, "completed")
    self.assertEqual(job["status"], "completed")

def test_retry_path_calls_backfill_and_never_carrier_automation(self):
    source = APP_PATH.read_text(encoding="utf-8")
    retry_block = source[source.index("def _retry_writeback_results"):source.index("def _render_postal_pending_v2")]
    self.assertIn("backfill_results(", retry_block)
    self.assertNotIn("run_automation(", retry_block)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_job_control tests.test_postal_ui_feedback tests.test_postal_mock_e2e -v
```

Expected: FAIL because `app.py` currently marks all successful automation results together.

- [ ] **Step 3: Map every writeback item back to its automation result**

At module imports in `app.py`, add `backfill_results` from `bot.sheets`. Do **not**
directly import new `job_control` helpers. Extend the existing retry-protected module
binding beside lines 20–30 instead:

```python
apply_writeback_outcome = _job_control.apply_writeback_outcome
writeback_retry_candidates = _job_control.writeback_retry_candidates
```

Remove the duplicate function-local `backfill_results` import after the module import
is covered by tests. Import `shipment_package_key` into
`postal_ui_feedback.py` from `job_control`.

In `postal_ui_feedback.summarize_batch_results()`, build `failure_alerts` by status:

```python
status = str(result.get("status") or "").strip().lower()
if status == "backfill_failed":
    alerts.append(f"訂單編號 {order_id}：運單已產生，但資料回填未完成（{reason}）")
else:
    alerts.append(f"訂單編號 {order_id}：未製單（{reason}）")
```

In `job_control.py`, implement the one authoritative mapper used by both the initial
writeback and retry. It uses `items[*].input_index`, synchronizes result dictionaries,
per-package order state, aggregate outcome, refresh intent, and terminal status:

```python
def apply_writeback_outcome(job, candidates, outcome):
    success_statuses = {"written", "already_present"}
    reason_by_status = {
        "manual_review": "writeback_manual_review",
        "conflict": "writeback_tracking_conflict",
        "partial_write": "writeback_partial",
        "write_failed": "writeback_failed",
    }
    completed = []
    failed = []
    seen_indexes = set()
    for item in (outcome or {}).get("items") or []:
        index = int(item["input_index"])
        if index < 0 or index >= len(candidates) or index in seen_indexes:
            continue
        seen_indexes.add(index)
        result = candidates[index]
        result["backfill_status"] = item["status"]
        result["backfill_row"] = item.get("row")
        if item["status"] in success_statuses:
            result.update({
                "status": "completed", "reason_code": "", "reason_text": "", "message": "",
            })
            completed.append(result)
        else:
            reason_code = reason_by_status.get(item["status"], "writeback_failed")
            result.update({
                "status": "backfill_failed",
                "reason_code": reason_code,
                "reason_text": "運單已產生，但資料回填未完成",
                "message": "運單已產生，但資料回填未完成",
            })
            failed.append(result)
    for index, result in enumerate(candidates):
        if index not in seen_indexes:
            result.update({
                "status": "backfill_failed",
                "backfill_status": "write_failed",
                "reason_code": "writeback_failed",
                "reason_text": "運單已產生，但資料回填未完成",
                "message": "運單已產生，但資料回填未完成",
            })
            failed.append(result)

    mark_results_completed(job, completed)
    mark_results_failed(job, failed)
    job["backfill_outcome"] = outcome
    summary = summarize_job_results(job.get("results"))
    terminal_status = (
        "completed"
        if summary["total"] > 0 and summary["completed"] == summary["total"]
        else "partial_failure"
    )
    job["status"] = terminal_status
    job["pending_refresh_needed"] = terminal_status == "completed"
    return terminal_status
```

Before the initial call, assign `job["results"] = results`; then call
`terminal_status = apply_writeback_outcome(job, writeback_candidates,
backfill_outcome)` and `_JOB_REGISTRY.finish(job, terminal_status)`. Here
`writeback_candidates` is the tracking-bearing list defined in Task 6, so legacy
`already_completed` results are never remapped or written. Remove the old
all-or-nothing mapping. `mark_results_completed()` receives only verified items and
`mark_results_failed()` only failed items. Update the failed-order stage mapping for
`status == "backfill_failed"` to `回填待確認`, never `未製單`.

- [ ] **Step 4: Add same-process safe retry and explicit restart boundary**

Add to `job_control.py`:

```python
def writeback_retry_candidates(results):
    return [
        result
        for result in (results or [])
        if str(result.get("status") or "").lower() == "backfill_failed"
        and str(result.get("tracking") or "").strip()
    ]
```

Add to `app.py` before `_render_postal_pending_v2`; this uses the same authoritative
mapper as the initial path and the registry boundary remains intact:

```python
def _retry_writeback_results(job):
    candidates = writeback_retry_candidates((job or {}).get("results"))
    if not candidates:
        return {
            "ok": False,
            "message": "運單可能已產生但回填紀錄不足，請提供既有追跡番号後再回填。",
        }
    outcome = backfill_results(candidates)
    terminal_status = apply_writeback_outcome(job, candidates, outcome)
    _JOB_REGISTRY.finish(job, terminal_status)
    return {"ok": outcome["ok"], "message": "回填已完成" if outcome["ok"] else "仍有資料需要確認"}
```

In the existing v2 result/status area, render `st.button("重新回填資料", key="pending_v2_retry_writeback")` only when `writeback_retry_candidates(job.get("results"))` is non-empty; on click call `_retry_writeback_results(job)` and rerun. `apply_writeback_outcome()` sets `job["pending_refresh_needed"]` only when **every submitted package** is completed; mixed outcomes keep the session's protected order visible through the Task 6 merge, and a fully verified retry follows the normal force-refresh path. This retry path must not import or invoke `run_automation`. If the process/session no longer holds a tracking-bearing result, show the exact manual-recovery message above. No automatic carrier retry is allowed.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_job_control tests.test_postal_ui_feedback tests.test_postal_mock_e2e -v
python -m py_compile app.py job_control.py postal_ui_feedback.py
git diff --check
git add -- app.py job_control.py postal_ui_feedback.py tests/test_job_control.py tests/test_postal_ui_feedback.py tests/test_postal_mock_e2e.py
git commit -m "fix: report postal writeback outcomes per package"
```

Expected: mixed batch outcomes remain independent, successful packages stay completed, and failed writeback never claims that no label was created.

### Task 9: Remove the legacy postal entry without redesigning the new UI

**Files:**
- Modify: `app.py`
- Modify: `tests/test_postal_start_flow.py`
- Modify: `tests/test_postal_ui_v2_app.py`
- Test: `tests/test_postal_ui_v2.py`

- [ ] **Step 1: Write RED navigation and preservation tests**

Add source-contract tests:

```python
def test_postal_navigation_has_one_formal_entry_and_preserves_order_contract(self):
    source = APP_PATH.read_text(encoding="utf-8")
    self.assertIn('["跨境揀貨單", "待製郵便運單", "使用說明", "讀取診斷"]', source)
    self.assertNotIn("郵局待打單（新版測試）", source)
    self.assertNotIn("with preview_tab:", source)
    self.assertIn("with postal_tab:", source)
    self.assertIn("_render_postal_pending_v2(", source)

def test_v1_renderer_and_session_keys_are_removed(self):
    source = APP_PATH.read_text(encoding="utf-8")
    for marker in ("pending_reset_", "pending_name_", "pending_selected_by_order", "pending_extra_trans_single_"):
        self.assertNotIn(marker, source)
    for marker in ("_start_job", "_native_info", "_apply_data_editor_state", "pending_v2_selected_by_order"):
        self.assertIn(marker, source)
```

Extend the existing `APP_TEST_SCRIPT` string in
`tests/test_postal_ui_v2_app.py`; do not create a new test method or an undeclared
runner helper. Immediately after its second `app.run(timeout=30)`, add:

```python
assert [tab.label for tab in app.tabs] == [
    "跨境揀貨單", "待製郵便運單", "使用說明", "讀取診斷",
]
assert "郵局待打單（新版測試）" not in [tab.label for tab in app.tabs]
assert "郵局待打單" not in [tab.label for tab in app.tabs]
buttons = [item.label for item in app.button]
for label in ("選取全部", "清除全部", "開始製單", "重新讀取", "全部恢復預設資料"):
    assert label in buttons, label
assert not app.exception, app.exception
```

Keep the operational-UI leakage check as the source-contract test in Step 4, scoped
to the `_render_postal_pending_v2` function body. Do not infer tab ownership from
global `app.metric`/`app.json` collections because Streamlit renders every tab and
those collections may legitimately contain diagnostics from another tab.

Reuse the existing selection/clear/restore interactions in `tests/test_postal_ui_v2_app.py`; after each click assert checkbox state and edited name/item values match the pre-change expectations. Do not update CSS snapshots or widget labels except the tab title and approved refresh messages.

- [ ] **Step 2: Run focused UI tests and verify RED**

Run:

```powershell
python -m unittest tests.test_postal_start_flow tests.test_postal_ui_v2 tests.test_postal_ui_v2_app -v
```

Expected: FAIL while both postal tabs and v1 state remain.

- [ ] **Step 3: Remove only v1-owned code and rename the tab**

In `app.py`:

- Replace five tab variables with `picking_tab, postal_tab, guide_tab, diagnostics_tab` and the exact four labels.
- Delete the old `with preview_tab:` renderer block.
- Keep `_render_postal_pending_v2`, `_start_job`, `_zero_value_warning_lines`, `_order_id_for_position`, recipient/ID warnings, `_apply_data_editor_state`, running UI, and `_native_info`.
- Remove only v1 helper families `_reset_key_for` through `_extra_trans_key_for`, v1 frame/selection builders, `_format_short_rate`, `_summary_cell`, `_summary_label`, and their unused session cleanup.
- Keep all `pending_v2_*`, `last_pending_*`, refresh and job state.
- Update guide copy to use `待製郵便運單`; do not globally replace the generic phrase `待製單資料`.
- Keep internal `postal-v2-*` CSS/widget keys because they are not user-visible and changing them risks layout/session regression.

- [ ] **Step 4: Verify minimal UI copy and no technical leakage**

Add a source-contract test that requires the three approved messages and asserts the operational renderer does not interpolate `row_count`, `ttl`, lock state, or raw exception values. Do not remove safe detail from the diagnostic tab.

- [ ] **Step 5: Run UI regression tests and commit**

Run:

```powershell
python -m unittest tests.test_postal_start_flow tests.test_postal_ui_v2 tests.test_postal_ui_v2_app tests.test_picking_labels -v
python -m py_compile app.py
git diff --check
git add -- app.py tests/test_postal_start_flow.py tests/test_postal_ui_v2_app.py
git commit -m "refactor: make postal v2 the only shipping entry"
```

Expected: exact four tabs, old labels absent, all existing v2 selection/editor/layout tests PASS, and no nonessential redesign appears in the diff.

### Task 10: Full verification, fresh review, and deployment-ready evidence

**Files:**
- Create: `docs/evidence/2026-08-13-cache-entry-writeback-local.md`
- Read/verify: all modified source and test files

- [ ] **Step 1: Run the complete automated suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
$trackedPy = @(git ls-files "*.py")
python -m py_compile @trackedPy
git diff --check
```

Expected: `unittest` ends in `OK`; no unexpected AppTest skip; compilation and whitespace checks pass.

- [ ] **Step 2: Run focused acceptance groups separately**

Run:

```powershell
python -m unittest tests.test_refresh_cache tests.test_picking_labels tests.test_postal_start_flow tests.test_postal_ui_v2 tests.test_postal_ui_v2_app -v
python -m unittest tests.test_pending_editor tests.test_automation_helpers tests.test_job_control tests.test_sheets_helpers tests.test_postal_ui_feedback tests.test_postal_mock_e2e -v
```

Expected: both groups end in `OK`, proving cache/UI and package/writeback independently.

- [ ] **Step 3: Perform local visual and interaction verification without business writes**

Launch the app with synthetic/mocked data only. Verify desktop and narrow viewport behavior:

- Four tabs are visible and only `待製郵便運單` is the postal entry.
- Existing v2 card layout, spacing, selection, clear-all, restore-default and start button hierarchy are unchanged.
- Normal state shows only `資料更新於 HH:mm` near refresh.
- Stale fallback shows one concise warning, not cache internals.
- No reload overwrites active postal edits or picking selections.
- No test action reaches Japan Post or the production Sheet.

Expected: screenshots/readback contain no customer data or secrets and show no unintended layout change.

- [ ] **Step 4: Obtain fresh-context code review**

Give a fresh reviewer only the design, acceptance conditions, branch, and commit range. Reviewer must run tests/read code and report P0/P1/P2 findings, with special attention to:

- single-flight correctness and copy isolation;
- rerun-loop prevention;
- selection/edit preservation;
- primary/additional preflight;
- exact-pair idempotency;
- row expansion before write;
- B/C/D/J readback;
- mixed package outcome handling;
- PII-safe logs;
- v1-only deletion boundaries.

Expected: P0/P1/P2 are all zero. Any finding returns to its owning task, followed by focused tests and a new fresh review.

- [ ] **Step 5: Write evidence and commit it**

Create `docs/evidence/2026-08-13-cache-entry-writeback-local.md` containing:

```markdown
# JapanPost-SaaS local acceptance evidence

- Branch and exact HEAD
- Baseline and final test counts
- Focused test commands and outcomes
- Python compile and git diff check outcomes
- Fresh reviewer result
- UI verification viewports and result
- Confirmed non-actions: no production Sheet read/write, no Japan Post submission, no push, no deploy
- Known boundary: no cross-process automatic recovery without a durable shipment ledger
- Next gate: user approval for branch push and Streamlit deployment
```

Run:

```powershell
git add -- docs/evidence/2026-08-13-cache-entry-writeback-local.md
git commit -m "docs: record jppost local acceptance"
git status --short
git log --oneline --decorate -10
```

Expected: only the original `.planning/`, `backups/`, and `tmp/` remain untracked; all implementation changes are committed on the `codex/*` branch.

- [ ] **Step 6: Stop at the external-action gate**

Report local results and ask for explicit approval before any `git push`, PR, Streamlit deployment, or safe test-order writeback. Do not merge `main`; the JapanPost-SaaS domain owner remains unresolved in the governance registry.

## Implementation sequencing rationale

Tasks 2–4 make data arrival faster without touching writeback. Tasks 5–8 then add explicit package identity before changing duplicate and writeback behavior, so the system never accepts a second tracking merely because the order ID matches. Task 9 removes the legacy UI only after the shared backend and new-entry regression tests are green. Task 10 separates local completion from external publication and production verification.

## Deferred follow-up

Create a separate specification for a compliant always-on host and a durable shipment ledger. That future ledger must persist package key, carrier tracking, label evidence, writeback state, idempotency key and recovery status before the system can safely recover an unfilled tracking number across process restarts. It must not reuse an unspecified B/C/D/J column or expose customer PII in logs.
