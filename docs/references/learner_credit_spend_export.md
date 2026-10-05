# Learner Credit Spend CSV Export

## Overview
`GET /api/v2/subsidies/<subsidy_uuid>/admin/transactions/export/` streams a subsidy's learner spend as a CSV file,
for the admin portal's "Spent" report download (**ENT-10608**). Enterprise admins reach it through
enterprise-access (`GET /api/v1/subsidy-access-policies/transactions/export/`), which checks the admin's access and
calls this endpoint with its own operator credentials.

- **View:** `TransactionAdminExport` in `apps/api/v2/views/transaction.py`
- **CSV formatting:** `apps/api/csv_exports.py`
- **Filters:** `TransactionExportFilterSet` in `apps/api/filters.py`

## Contract

| Query param | Notes |
|---|---|
| `enterprise_customer_uuid` | Optional. If given and it doesn't own the subsidy, the response is 404. |
| `subsidy_access_policy_uuid` | Optional. Only spend redeemed via this policy (budget). |
| `start_date` / `end_date` | Optional, `YYYY-MM-DD` only, UTC, both inclusive whole days. `end_date` before `start_date` is a 400. |
| `search` | Optional. Matches learner email or course title. |

| Status | When |
|---|---|
| 200 | `text/csv; charset=utf-8` attachment, streamed. |
| 400 | Invalid params (JSON body). |
| 401 / 403 | Not authenticated / lacks `PERMISSION_CAN_READ_ALL_TRANSACTIONS` (admin-level) for the subsidy's customer, or the subsidy doesn't exist (same as the admin list). |
| 404 | Malformed `subsidy_uuid`, or `enterprise_customer_uuid` doesn't own the subsidy. |
| 405 | Any method other than GET. |

Columns: Learner Email, Learner ID, Course Title, Course Key, Date Spent (UTC), Amount Spent, Unit, Status
(`Committed` / `Refunded`), Policy UUID.

## Gotchas

- **Admin-level permission, not the v1 viewset.** The v1 `TransactionViewSet` uses the learner-level
  `PERMISSION_CAN_READ_TRANSACTIONS`, and only filters a learner's rows using roles from the JWT. A learner whose role
  comes from a database assignment would pass the check without that filter. A bulk export of learner emails must use
  the admin-level permission, like the v2 admin list.
- **What counts as spend:** only `COMMITTED` transactions that are neither deposits (including the starting balance)
  nor adjustments. A committed reversal is reported as `Refunded`.
- **Amounts:** spend is a negative quantity. It is negated (not `abs()`-ed) so an unexpected positive row is visible
  as a negative amount. `usd_cents` is converted to dollars with `Decimal`; `seats` is reported as a count.
- **Spreadsheet formula injection:** email, course title and course key come from learners or partners. Cells
  starting with `= + - @ \t \r` are prefixed with `'` (OWASP CSV injection guidance).
- **Excel encoding:** the file starts with a UTF-8 BOM so Excel doesn't garble non-ASCII titles.
- **Dates:** on Python 3.11+, Django's `parse_datetime('2024-01-10')` returns midnight rather than `None`, so
  hand-rolled "date means end of day" parsing silently breaks. Use `DateFilter` with `lookup_expr='date__lte'`
  instead.
- **Streaming and query count:** rows are produced from `queryset.iterator()` through `StreamingHttpResponse`, with
  `select_related('ledger', 'reversal')`. The query count is constant in the number of rows (there's a test).
- **Content negotiation:** errors are rendered as JSON. A client that sends `Accept: text/csv` only would get a 406
  from DRF; send `*/*` (the `requests` default).
