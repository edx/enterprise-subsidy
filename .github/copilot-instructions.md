# Copilot Instructions: enterprise-subsidy

Enterprise Subsidy is a Django-based microservice within the Open edX ecosystem that manages financial subsidies for enterprise customers. It tracks and balances enterprise-funded learning transactions, allowing companies to provide learning credits to their employees that can be redeemed for courses, programs, and other educational content.

---

## Key Principles

- Search the codebase before assuming something isn't implemented.
- Write comprehensive tests with clear documentation.
- Follow Test-Driven Development when refactoring or modifying existing functionality.
- Always write tests for new functionality you implement.
- Keep changes focused and minimal — do not refactor surrounding code unless asked.
- Follow existing code patterns.
- Use the `ddt` package for parameterized tests to reduce code duplication.
- Do not add features, error handling, or abstractions beyond what is directly requested.
- Do not add docstrings, comments, or type annotations to code you did not change.

---

## Architecture Overview

### Core Applications

- **api** — REST API endpoints with versioned views (`v1/`, `v2/`), serializers, and filters.
- **subsidy** — Core subsidy domain models, including the `Subsidy` model with ledger integration.
- **transaction** — Transaction management, ledger transactions, and reversals using `openedx-ledger`.
- **content_metadata** — Content metadata API integration and caching with versioned cache keys.
- **fulfillment** — Handles fulfillment of subsidies, including GEAG (Get Enrolled & Get Assigned) patterns.
- **api_client** — External service integration (Enterprise API, Enterprise Catalog, LMS User API).
- **core** — Shared utilities, context processors, and base functionality.

### Key Concepts

- **Subsidies**: Store value (learner credit in USD cents or subscription seats) redeemable for educational content.
- **Ledger**: Tracks transactions (value movement in/out of subsidies) using the `openedx-ledger` package.
- **Transactions**: Individual debit/credit entries with content keys, learner emails, amounts, and idempotency keys.
- **Redemption**: The act of redeeming stored value for specific content.
- **Revenue Categories**: Control revenue recognition (`bulk-enrollment-prepay`, `partner-no-rev-prepay`).
- **Reference Types**: Links subsidies to originating objects (e.g., Salesforce `OpportunityLineItem`).
- **Fulfillment**: The process of converting a subsidy redemption into an actual enrollment.

### External Service Integration

- **Enterprise Access**: Determines access policies and approval workflows.
- **Enterprise Catalog**: Content metadata and pricing information.
- **LMS (edxapp/edx-enterprise)**: User management and course enrollment operations.
- **Discovery Service**: Source of truth for content pricing and metadata.
- **Event Bus (Kafka)**: Event-driven communication between services via `openedx-events`.

---

## Data Model Conventions

- Use `django-simple-history` for change tracking on subsidy models.
- Use the soft deletion pattern: `is_soft_deleted` field instead of hard deletes.
- All Django models require PII annotation (enforced by CI checks).
- Use `edx-rbac` for role-based access control.
- Use `openedx-ledger` for transaction tracking and balance management.
- Use versioned cache keys for key-based cache invalidation (see `docs/caching.rst`).

---

## Django Patterns

- **Models**: Use `django-simple-history` for audit trails. Add `is_soft_deleted` for soft delete. Annotate PII fields.
- **Query optimization**: Use `select_related` / `prefetch_related` to avoid N+1 queries. Add `db_index=True` on frequently filtered fields.
- **Caching**: Use `TieredCache` with versioned cache keys. See `docs/caching.rst` for patterns.
- **Timezones**: Always use `django.utils.timezone.now()` instead of `datetime.datetime.now()`.
- **Admin**: Register models with `django.contrib.admin`. Use `list_display`, `search_fields`, `list_filter`.
- **Management commands**: Extend `BaseCommand`. Use `self.stdout.write` for output. Add `--dry-run` flag for destructive operations.

---

## Service Integration Patterns

- Use `api_client/` for all external service calls. Do not call external services directly from views or models.
- Use `requests` with retry logic for HTTP calls to external services. Retry on 5xx responses.
- Use `openedx-events` and the Kafka event bus for cross-service communication. Publish events after state changes, not before.
- Use idempotency keys for all transaction creation requests to prevent duplicate processing.
- Use JWT authentication for service-to-service calls.

---

## Celery / Async Task Patterns

- Place task definitions in `tasks.py` within the relevant app.
- Use `@shared_task` decorator. Bind tasks with `bind=True` to access `self.retry()`.
- Implement retry logic with exponential backoff for external service calls:
  ```python
  self.retry(exc=exc, countdown=2 ** self.request.retries, max_retries=3)
  ```
- Keep tasks idempotent — they may run more than once.
- Do not perform complex business logic inside tasks; delegate to service-layer functions.
- Use `apply_async` with an explicit `queue` parameter when routing matters.

---

## Security Best Practices

- Validate all user input at API boundaries. Never trust client-supplied data.
- Use Django REST Framework serializers for input validation.
- Use `edx-rbac` permission classes on all API views — do not rely solely on authentication.
- Never log PII (emails, names, IDs that map to individuals). Use anonymized identifiers in logs.
- Use parameterized queries (Django ORM). Never use raw SQL with string interpolation.
- Escape all data rendered in templates. Do not use `mark_safe` unless absolutely necessary.
- Store secrets in environment variables, never in source code or version control.

---

## Testing

- Framework: `pytest` with `pytest-django`. Coverage reporting is enabled by default.
- Use the `ddt` package for parameterized tests.
- Use `factory_boy` for test data factories.
- Mock external service calls; do not make real HTTP requests in unit tests.
- PII annotation checks are required for all Django models and are enforced in CI.

### Test Commands

```bash
# Run all tests with coverage
make test

# Run a specific test file or directory
pytest ./path/to/tests

# Run all tests, quality checks, PII checks, and keyword checks
make validate
```

---

## Quality / Linting

```bash
# Run linting and style checks
make quality

# Run full validation suite (tests + quality + PII + keyword checks)
make validate
```

Tools used: `pylint`, `pycodestyle`, `isort`.

---

## Local Development

- Server runs on `localhost:18280`.
- Uses MySQL 8.0, Memcache for caching, and Kafka for the event bus.
- Start with events: `make dev.up.with-events`
- Confluent Control Center (for Kafka): `http://localhost:9021/clusters`
- Consume ping events: `./manage.py consume_enterprise_ping_events`
- Produce ping events: `./manage.py produce_enterprise_ping_event`

---

## Documentation

- `docs/architecture_overview.rst` — Comprehensive architecture guide; update when adding significant new patterns or gotchas.
- `docs/caching.rst` — Cache design, `TieredCache` usage, and versioned cache key patterns.
- `docs/decisions/` — Architectural Decision Records (ADRs) for key design choices.

When you learn a non-obvious pattern, integration quirk, or gotcha, document it in `docs/architecture_overview.rst` or the relevant `docs/` file.
