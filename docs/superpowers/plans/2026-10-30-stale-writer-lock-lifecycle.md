# Stale Writer Lock Lifecycle Repair Plan

**Goal:** Ensure a worker that has stopped always attempts the normal terminal-and-release path; keep the global writer lock for live or globally ambiguous work, while per-order tombstones continue preventing duplicate shipments.

**User constraints:** No age-based unlock, no admin unlock UI, no production shipment, no production order or Sheets write, and no deployment in this task.

## Completed work

- [x] Read the lock implementation and all production entry paths: normal `app._start_job`, retry-writeback `_retry_writeback_results`, split/reissue `execute_plan`, lifecycle callbacks in `bot.automation`, Google Sheets backfill, and their existing tests.
- [x] Confirm the lifecycle defect: split/reissue marked the **global** lock retained for an order's `issuing`/`unknown` result even after its worker had stopped; internal per-order ledger/guard write uncertainty also retained the global mutex. Normal cleanup treated certain per-order states as a reason to retain the global lock. Those states already block that same order and must not block every unrelated order.
- [x] Make add/update/delete commit-then-timeout outcomes resolve through one exact read-back; never repeat an ambiguous mutation.
- [x] Verify the current canonical title, sheet generation, owner, and marker at irreversible issue/writeback gates, terminalization, and before deleting a terminal lock.
- [x] Mirror normal, retry-writeback, and split/reissue lifecycle stages; write terminal `completed` only after the worker stops, then release its own generation.
- [x] Reduce Sheets quota pressure: use successful mutation responses as confirmation, read back only ambiguous failures, write per-order guards before one batch-level `writeback_pending` marker, and batch split/reissue writeback while still flushing already-uploaded labels if a later package fails.
- [x] Read back target rows after a backfill mutation exception so a committed write with a lost response is not reported as failed; never repeat the target mutation.
- [x] After a synchronous worker stops, retry only a read-only owner/generation check when the lock was retained by transient uncertainty; auto-clean only if the canonical lock still belongs to that worker.
- [x] Keep ambiguous order outcomes in the encrypted ledger / duplicate guard, but release the global lock when its ownership and marker state are known. Preserve completed tracking/PDF results if final lock cleanup reports a warning.
- [x] Archive only an exact terminal `completed` lock on a later acquire, preserving its hidden sheet as evidence. Never reclaim nonterminal locks based on age.
- [x] Add regressions for mutation read-back, owner fencing, terminal archival, retained order tombstones, normal/retry/split lifecycle, release-result preservation, and Sheets API read budget.
- [x] Run the full unittest suite after the final code-review fixes; record the final count in the handoff. Focused regression suite and API-budget tests passed. `compileall` and `git diff --check` passed.
- [x] Keep all verification local and synthetic. No USPS, production Sheets/order writes, or deployment.

## Safety boundary

A hard process termination before a durable `completed` marker cannot be safely distinguished from a still-running or ambiguous writer without an external lease/heartbeat, operator recovery, or durable job coordinator. This implementation deliberately leaves such a nonterminal lock fail-closed; it does not infer safety from age. Normal Python completion, handled API exceptions, and lost Sheets responses use the automatic cleanup/read-back path.

## Files changed

- `bot/shipment_ledger.py`
- `bot/shipment_service.py`
- `app.py`
- `features/shipment_workbench.py`
- `tests/test_shipment_ledger.py`
- `tests/test_writer_lock_recovery.py`
- `tests/test_shipment_service.py`
- `tests/test_postal_worker_runtime.py`

No PR, merge, or deployment is part of this task; the working branch is `codex/jppost-stale-writer-lock-recovery-20261030`.
