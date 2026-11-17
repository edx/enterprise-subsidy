# Keyed deposit requests (v2 deposit-admin-create)

`POST /api/v2/subsidies/<subsidy_uuid>/admin/deposits/` is safe to retry when the request carries an
`idempotency_key`. Logic lives in `DepositAdminCreate.create()` (`enterprise_subsidy/apps/api/v2/views/deposit.py`).
Callers such as enterprise-access's learner credit top-up rely on the status code to tell "already done" (200) from
"try again" (429) from "failed for good" (400/404/409/422).

The view checks in this order: validate the body, then look up a replay (keyed requests only), then check expiry,
then create.

| Situation (keyed request)                                           | Response                              |
|---------------------------------------------------------------------|---------------------------------------|
| First request for the key                                           | 201, new deposit                      |
| Repeat with same quantity, reference ID and provider                | 200, the original deposit             |
| Repeat after the subsidy has expired                                | 200, the original deposit             |
| Same key, different quantity, reference ID or provider              | 409 `deposit_idempotency_conflict`    |
| Key already used by a non-deposit transaction (e.g. a redemption)   | 409 `deposit_idempotency_conflict`    |
| Loser of a same-key race (insert fails on a unique constraint)      | 200, the winner's deposit             |

| Situation (any request, with or without a key)                      | Response                              |
|---------------------------------------------------------------------|---------------------------------------|
| Request fails validation (even on an expired subsidy)               | 400 with field errors                 |
| Subsidy soft-deleted after the permission check                     | 404, message names the subsidy        |
| Ledger lock held by another request                                 | 429, DRF `Throttled`, code `ledger_lock_error` (retryable) |
| New deposit on an expired subsidy                                   | 422 `deposit_on_expired_subsidy`      |

Without a key, a duplicate still returns 422 `deposit_creation_error`, so an operator's accidental double-submit is
still blocked.

The 429 body is DRF's standard `{"detail": "Attempt to lock the Ledger failed, please try again."}`, the same as the v2
transaction view. The error code is on the `ErrorDetail` (`response.data['detail'].code`); it is not a `code` key in the
body.

## Non-obvious details

- **Replay lookup runs before the expired-subsidy check**, so a retry still gets 200 after expiry. A new key on an
  expired subsidy still gets 422 `deposit_on_expired_subsidy`.
- **Metadata is not compared.** Only quantity, reference ID and provider decide replay vs 409; a replay returns the
  original deposit as created, ignoring any new metadata.
- **The view validates once, up front, and then calls `perform_create()` directly** rather than
  `CreateAPIView.create()`, which would validate a second time.
- **openedx-ledger wraps the lock error.** `openedx_ledger.api.create_deposit()` wraps every failure (including
  `LedgerLockAttemptFailed`) in `openedx_ledger.api.DepositCreationError`. The v2 serializer checks `__cause__` and
  raises a new `LedgerLockAttemptFailed` (same message, chained `from` the ledger's error), so the view can catch it
  directly, as the v2 transaction view does. Don't re-raise `exc.__cause__ from exc`: that makes the `__cause__` chain
  loop back on itself, so any `while exc: exc = exc.__cause__` walk never ends.
- **The deleted-subsidy 404 comes from the `subsidy` property.** The rbac permission check loads the subsidy separately
  (`api/utils.get_subsidy_customer_uuid_from_view`), so a soft-delete between that check and the view would otherwise
  surface as an uncaught `Subsidy.DoesNotExist` (500).
- **Which unique constraint the race loser hits depends on timing.** openedx-ledger's `create_transaction()` uses
  `get_or_create(ledger, idempotency_key)`. If the winner committed before that `get`, the loser reuses the winner's
  Transaction and fails on `Deposit.transaction` (one-to-one). If not, the loser's insert fails on the Transaction
  `(ledger, idempotency_key)` unique constraint. The view handles both the same way: after any non-lock
  `DepositCreationError` on a keyed request, it re-reads the deposit for the key and returns it (or 409s if it
  differs). If no deposit is found, the 422 stands.
- The re-read relies on `ATOMIC_REQUESTS` being off, so the loser's read is not pinned to a pre-race MySQL snapshot.
