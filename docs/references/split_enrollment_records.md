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
