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

`metadata` is a `DictField(allow_null=True)`, so anything that is not a JSON object (a list, string, number or
boolean, including empty or falsy ones like `[]`, `""`, `0` and `false`) is rejected with **400** (`not_a_dict`).
Before this change the empty/falsy ones were silently treated as no metadata and returned 201. `validate_metadata`
then returns **400** if any key is in `RESERVED_DEPOSIT_METADATA_KEYS`, naming each clashing key. Validation runs
inside `is_valid()`, so no Deposit or Transaction is created. `null` and `{}` are accepted and stored as no metadata.

`RESERVED_DEPOSIT_METADATA_KEYS` is built by `reserved_metadata_keys()` in
`enterprise_subsidy/apps/transaction/utils.py`. It uses `inspect.signature` to collect the named (positional-or-keyword
and keyword-only) parameters of every function the metadata is forwarded through, so it follows openedx-ledger
upgrades without manual edits. `**metadata` itself is excluded. `ReservedMetadataKeysTestCase` tests the helper, and
`test_reserved_keys_include_known_ledger_parameters` checks that the deposit set contains all 14 current
`create_deposit()`/`create_transaction()` parameter names (a `>=` floor, so new ledger parameters don't break it).
It fails if the set ever comes out empty or partial (e.g. if
openedx-ledger wraps these functions in a decorator that hides their parameters).

## Same problem on the redemption path (not fixed yet)

`POST /api/v1/transactions/` (`TransactionViewSet.create`) and `POST /api/v2/subsidies/<subsidy_uuid>/admin/transactions/`
(`TransactionCreationRequestSerializer`) pass client metadata to `Subsidy.redeem()`. It is splatted into
`Subsidy.create_transaction(**tx_metadata)` and then `openedx_ledger.api.create_transaction(**transaction_metadata)`
(`enterprise_subsidy/apps/subsidy/models.py`) without any validation. A fix should reuse
`reserved_metadata_keys(Subsidy.create_transaction, openedx_ledger.api.create_transaction)` rather than copy the logic.
Passing the unbound `Subsidy.create_transaction` also reserves `self`, which is correct: a `self` key would collide too.
