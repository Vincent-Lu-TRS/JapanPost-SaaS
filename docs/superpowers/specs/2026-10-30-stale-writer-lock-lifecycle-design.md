# JapanPost Global Writer Lock Lifecycle

## Goal

Prevent a completed or stopped shipment job from leaving the global writer mutex behind, while retaining same-order duplicate prevention for normal, split, and reissue shipments.

## Findings

- The global mutex is a hidden Google Sheets worksheet with a unique title and owner/generation marker. Its nonterminal state is intentionally fail-closed.
- Ambiguous Google Sheets `addSheet`, marker update, or delete responses were previously treated as unknown without a bounded exact read-back. A server-side commit followed by a lost response could leave a lock behind.
- The split/reissue service conflated an order-level `issuing`/`unknown` outcome with a live global worker: it explicitly retained the global lock when that worker exited. Its encrypted per-order shipment record already blocks a retry of the same order.
- The normal flow's release policy and internal guard/ledger error handling could likewise make per-order uncertainty retain the global lock. Uncertainty about one order should block that order; uncertainty about the global lock owner/marker must block the system.
- The retry-writeback action used the same global lock but went straight from `acquire()` to backfill and `release()`. It did not write `writeback_pending` or `completed`, leaving a nonterminal lock if delete was known not to commit.
- Split/reissue did not mirror each durable lifecycle stage to the global marker, which made recovery diagnosis inconsistent.
- A final release exception in split/reissue could hide a result that already contained a known tracking number and PDF.
- The supplied logs identify later attempts failing at `writer_lock` because a canonical lock existed (`shipment_writer_busy_or_unknown`). They do not identify the earlier generation's owner/stage, so they cannot prove which historical request created the live sheet. The code paths above are confirmed contributors and are now corrected.
- The reported batch `APIError` after many rows also matches a code-level quota hazard: normal/retry paths repeated a freshly fenced global `writeback_pending` marker for each shipment, while split/reissue reread the target grid for each package. These paths now persist per-order guards first and use a single batch marker/backfill. The supplied logs cannot prove that this was the historical `APIError` cause.

## Safety invariants

1. Do not unlock by age and do not add an admin-only unlock interface.
2. Before an irreversible postal submission or target-Sheets write, verify the current canonical sheet and exact owner/generation marker. Before releasing a terminal lock, verify the exact marker again.
3. A successful Sheets mutation response confirms that mutation. After an ambiguous Sheets exception, issue a bounded read-only exact read-back. Never repeat the mutation, postal submission, or backfill merely because the response was lost.
4. Once the worker has stopped, persist its terminal global `completed` marker and release its own generation. The per-order ledger/guard remains authoritative for unfinished, ambiguous, or unverified order outcomes and blocks that same order from being resubmitted.
5. If a terminal marker remains because release could not be confirmed or the process stopped after terminalization, a later acquisition may archive only that exact `completed` generation. Preserve the hidden sheet evidence. Nonterminal, malformed, legacy, or mismatched generations remain fail-closed regardless of age.
6. Do not let final lock cleanup overwrite known shipment results, tracking numbers, PDF links, or writeback state.
7. Keep normal, split, and reissue flows on the existing Google Sheets target and existing per-order duplicate guards.

## Implementation

### Global lock and durable per-order records

- `GlobalShipmentLock` resolves ambiguous add, stage, archive, and delete operations with exact one-time read-back; no retry is sent. Successful stage updates do not spend an extra read; critical `issuing`, `writeback_pending`, and `completed` stages verify the current owner before mutation, and release verifies the terminal marker before deletion.
- After the synchronous worker stops, a retained lock gets one additional read-only owner/generation check. It is released only if the canonical sheet still proves the same owner; a missing, malformed, or different generation stays fail-closed.
- Ownership checks verify the canonical lock title, owner, and generation through a fresh title-addressed marker read. Critical gates before USPS issue and Google Sheets writeback, terminalization, and release verify the current marker.
- Only the final `completed` stage is eligible for automatic archival. Archiving renames and hides the old lock sheet; it does not edit shipment history or order rows.
- Internal ledger/guard writes use the successful API response as confirmation and perform one exact row read-back only after a mutation exception. A per-order persistence error aborts that shipment path and relies on any earlier durable tombstone; it does not, by itself, retain the global mutex after the worker stops. A stage confirmed not committed remains cleanable; an ambiguous global lock owner/stage mutation retains the lock.
- Nonterminal per-order records continue to block same-order repeat submissions, including when the global writer is released for unrelated orders.

### Normal, split, and reissue flows

- Normal flow first records each order's `writeback_pending` duplicate guard, advances the global marker once for the batch, then performs target-sheet backfill. It marks the global writer terminal in `finally` once the worker stops, then releases its generation.
- Retry-writeback records `writeback_pending` before backfill, preserves known tracking/PDF state if the API or cleanup fails, then records `completed` and releases once its synchronous worker stops.
- Split/reissue mirrors postal lifecycle transitions to the global marker, accumulates confirmed uploaded packages, and backfills them together. If a later package fails, it first backfills earlier uploaded packages before returning the original failure. It records `completed` at worker shutdown. Existing immutable plans, tracking baselines, PDF upload requirements, and restart guards remain intact.
- If a target `batch_update` raises, backfill reads the exact planned rows; complete rows count as verified, partial writes remain partial, and no mutation is repeated.
- When final lock cleanup fails, preserve known results and expose a safe warning. Do not turn a completed shipment into a failed result or automatically resubmit it.

## Validation and limits

- Focused lock/lifecycle/security/API-budget/sheets suite: 119 tests passed.
- Full repository unittest discovery: 640 tests passed, 6 skipped.
- `python -m compileall -q app.py bot features tests` and `git diff --check` passed before final documentation closeout.
- All tests use local fakes/synthetic data. No USPS request, production order change, target-sheet write, or deployment was performed.
- An abrupt process kill before a durable terminal marker cannot be distinguished safely from a live or ambiguous writer without a lease/heartbeat or explicit operator recovery. Because age-based reclaim and an admin unlock UI are excluded, such nonterminal locks remain fail-closed. Normal worker shutdown and handled API/read-back failures automatically reach terminal cleanup.
