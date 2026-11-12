# Split Enrollment Records

## Overview
Multi-Plan Learner Credit Funding funds one enrollment from more than one subsidy (**ENT-12412**, spec KD-4 and
KD-15). The ledger assumes one transaction per enrollment, so `apps/transaction/models.py` links the transactions that
together fund one enrollment, without changing openedx-ledger.

- **`SplitEnrollmentRecord`:** one enrollment (enterprise customer, learner, content key) funded by more than one
  subsidy. Holds `state`, `planned_legs`, `total_quantity` (negative USD cents) and `fulfillment_identifier`.
- **`SplitEnrollmentLeg`:** one ledger `Transaction` paying part of a record. Its amount, subsidy and budget are read
  from the transaction, not copied.
- **Created by:** migration `transaction.0001_split_enrollment_models`. Phase 1a ships these tables dormant: nothing
  reads or writes them yet.
- **Admin:** read-only, under **Transaction** in the Django admin. Legs are shown inline on the record.

## Gotchas
- **One live split per enrollment is enforced by the database.** The unique constraint `unique_live_split` covers
  (customer, learner, content key, `is_live`). `is_live` is True while `pending`, `committed` or `reversing`, and
  NULL otherwise, because NULLs never collide in a unique index. Django skips conditional unique constraints on
  MySQL, so don't replace this with `UniqueConstraint(condition=...)`.
- **`is_live` must never be False.** The unique index treats True, False and NULL as different values, so a live
  record saved with False can coexist with another live record (True) for the same enrollment, which defeats the
  one-live-split guarantee. Two False records for the same enrollment would also collide. `save()` always derives it
  from `state`.
- **Bulk paths keep `is_live` in sync too.** `QuerySet.update(state=...)` and `bulk_create()` bypass `save()`, so
  the model's QuerySet derives `is_live` on both. `update()` must set `state` to a plain value (not an expression),
  and can't set `is_live` directly; otherwise it raises `ValueError`. Prefer `transition_to()` for single records.
- **`planned_legs` and leg rows are write-once.** Changing them through `save()` or `QuerySet.update()`, or
  deleting a leg (one or in bulk), raises `SplitEnrollmentWriteOnceException`. `bulk_update()` goes through
  `update()`, so the same rules apply to it.
- **Nothing here can be deleted while legs exist.** Legs are `on_delete=PROTECT` to both the record and the ledger
  `Transaction`, so deleting either raises `ProtectedError`.
- **`fulfillment_identifier` is not unique.** The enterprise fulfillment uuid repeats across re-enrollments.
- **`makemigrations` generates unrelated drift.** On `main` it also writes an `openedx_ledger` migration into
  site-packages, from drift in the installed openedx-ledger. Don't commit it. The split migration depends on
  `openedx_ledger.0016_mariadb_uuid_conversion`.

## Reconciliation (`reconcile_split_enrollments`)
**ENT-12420.** A management command that finds split enrollments needing attention and logs what reconciliation would
do with each. Phase 1a only reports: `--fix` raises unless `ENABLE_SPLIT_RECONCILIATION_FIX` is set, and its rules
aren't implemented yet. The logic lives in `apps/transaction/reconciliation.py`, so the `--fix` work can reuse it.

```
./manage.py reconcile_split_enrollments [--record <uuid>] [--max-records 200]
```

It selects, in this order (the duplicate check runs first so a backlog of stale records under `--max-records` can't
hide its P1 alert; once the cap is hit, no new records are added, but records already selected still collect every
rule they match):

| Rule | Selects | Age measured from |
|---|---|---|
| `duplicate_live_redemption` | **live** records from the last 7 days whose enrollment has another live ledger transaction in any of the customer's ledgers. Logged as `[P1] SPLIT_RECONCILIATION_DUPLICATE_LIVE_REDEMPTION`. A failed or reversed split doesn't count: a later redemption of its enrollment is the expected retry. | `created` |
| `stale_pending` | `pending` records older than 10 minutes (a request owns one for at most the 300 s worker timeout) | `created` |
| `stale_reversing` | `reversing` records older than 15 minutes (a refund cascade step failed) | `modified`, the time it entered `reversing` (`transition_to()` updates it) |

For each record it logs the state, `age` (since `created`), `in_state` (since `modified`), rules, the legs found and
how (leg row, or the ledger idempotency key `split-<record uuid>-<leg_order>` when the leg row is missing), the
action, and evidence. `--record <uuid>` for a record that matches no rule logs its state and ages instead. A dry run
changes nothing, so when `--max-records` is hit the same records come first on the next run. A pending record's
action:

| Legs found | Action |
|---|---|
| none, or any `created` (and none `committed`) | `fail_all_legs` (legs move to pending only after every leg exists, so the enroll was never attempted) |
| all `pending`, no `fulfillment_identifier` | `resend_enroll_to_learn_fulfillment` |
| all `pending`, `fulfillment_identifier` set | `verify_fulfillment_then_commit` (the edx-enterprise lookup is part of `--fix`) |
| some or all `committed`, the rest `pending` | `commit_remaining_legs` |
| a leg missing next to pending or committed ones, `committed` next to `created`, a reversed leg, or anything else | `manual_review` (a missing leg there likely exists but wasn't found, so it's unknown whether the enroll happened; a committed leg means the enroll happened, so failing the others would lose credit) |

A `reversing` record's action is `reverse_remaining_legs`. Each run logs counts per rule and per action, and exits
non-zero if any record errored. It never writes and never calls `create_transaction()`: the ledger checks the balance
before its idempotency lookup, so re-creating a leg that drained its ledger would raise instead of returning the leg.

**Gotcha:** the split redemption must create each leg's transaction with `split_leg_idempotency_key()` from
`reconciliation.py`, or reconciliation can't find legs whose rows were never written.
