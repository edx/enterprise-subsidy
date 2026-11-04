# Keyed deposit requests (v2 deposit-admin-create)

`POST /api/v2/subsidies/<subsidy_uuid>/admin/deposits/` is safe to retry when the request carries an
`idempotency_key`. Logic lives in `DepositAdminCreate.create()` (`enterprise_subsidy/apps/api/v2/views/deposit.py`).

| Situation (keyed request)                                           | Response                              |
|---------------------------------------------------------------------|---------------------------------------|
| First request for the key                                           | 201, new deposit                      |
| Repeat with same quantity, reference ID and provider                | 200, the original deposit             |
| Repeat after the subsidy has expired                                | 200, the original deposit             |
| Same key, different quantity, reference ID or provider              | 409 `deposit_idempotency_conflict`    |
| Key already used by a non-deposit transaction (e.g. a redemption)   | 409 `deposit_idempotency_conflict`    |
| Ledger lock held by another request                                 | 429 `ledger_lock_error` (retryable)   |
| Loser of a same-key race (insert fails on a unique constraint)      | 200, the winner's deposit             |

Requests **without** a key are unchanged: a duplicate returns 422 `deposit_creation_error`, and lock contention
also returns 422.

## Non-obvious details

- **Replay lookup runs before the expired-subsidy check**, so a retry still gets 200 after expiry. A new key on an
  expired subsidy still gets 422 `deposit_on_expired_subsidy`.
- **The lock error arrives wrapped twice.** `openedx_ledger.api.create_deposit()` wraps every failure (including
  `LedgerLockAttemptFailed`) in `openedx_ledger.api.DepositCreationError`, and the v2 serializer wraps that again in
  its own `DepositCreationError`. The view walks the `__cause__` chain (`_is_ledger_lock_error`). The v2 transaction
  view catches `LedgerLockAttemptFailed` directly, which does not work here.
- **Which unique constraint the race loser hits depends on timing.** openedx-ledger's `create_transaction()` uses
  `get_or_create(ledger, idempotency_key)`. If the winner committed before that `get`, the loser reuses the winner's
  Transaction and fails on `Deposit.transaction` (one-to-one). If not, the loser's insert fails on the Transaction
  `(ledger, idempotency_key)` unique constraint. The view handles both the same way: on any creation failure for a
  keyed request, it re-reads the deposit for the key and returns it (or 409s if it differs).
- The re-read relies on `ATOMIC_REQUESTS` being off, so the loser's read is not pinned to a pre-race MySQL snapshot.
