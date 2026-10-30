# Deposit metadata: reserved keys

Endpoint: `POST /api/v2/subsidies/<subsidy_uuid>/admin/deposits/`
(`DepositCreationRequestSerializer` in `enterprise_subsidy/apps/api/v2/serializers/deposits.py`).

## Gotcha: metadata is splatted into ledger function kwargs

The serializer passes `metadata` to `openedx_ledger.api.create_deposit(..., **metadata)`, which forwards it on to
`create_transaction(..., **metadata)`. A metadata key that matches a named parameter of either function therefore
does not end up in `Transaction.metadata`. Instead it either:

- binds to that parameter and silently writes a ledger column (e.g. `subsidy_access_policy_uuid`, `lms_user_id`,
  `deposit_uuid`) and the request returns 201, or
- collides with an argument that is already passed explicitly, raising `TypeError`. Where it is raised decides the
  status code:
  - `state` collides inside `create_deposit()`, whose catch-all turns it into `DepositCreationError`, so it returned
    a confusing **422**.
  - `quantity`, `ledger`, `idempotency_key`, `sales_contract_reference_id` and `sales_contract_reference_provider`
    collide at the serializer's own call to `create_deposit()`. The serializer only catches the ledger's
    `DepositCreationError`, so the `TypeError` escaped and they returned **500**.

Non-object metadata (a JSON list or string) also returned 500, because `**metadata` requires a mapping.

## Behaviour (ENT-12395)

`validate_metadata` returns **400** if a non-empty `metadata` is not a JSON object, or if any key is in
`RESERVED_DEPOSIT_METADATA_KEYS`. The error message names each clashing key. Validation runs inside
`is_valid()`, so no Deposit or Transaction is created. Empty values (`null`, `{}`, `[]`, `""`) are still treated
as no metadata.

`RESERVED_DEPOSIT_METADATA_KEYS` is computed with `inspect.signature` from the named (positional-or-keyword and
keyword-only) parameters of both ledger functions, so it follows openedx-ledger upgrades without manual edits.
`**metadata` itself is excluded. `test_reserved_keys_track_ledger_api_signatures` guards this.
