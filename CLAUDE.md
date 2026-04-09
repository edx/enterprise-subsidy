# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Enterprise Subsidy is a Django-based microservice within the Open edX ecosystem that manages financial subsidies for enterprise customers. The service tracks and balances enterprise-funded learning transactions, allowing companies to provide learning credits to their employees that can be redeemed for courses, programs, and other educational content.

## Test and Quality Instructions

- To run unit tests or generate coverage reports, invoke the `unit-tests` skill.
- To run quality checks (linting, style), invoke the `quality-tests` skill.

## Code Navigation

- Prefer using the LSP tool over grep/glob when navigating Python code (definitions, references, type info)

## Key Principles

- Search the codebase before assuming something isn't implemented
- Write comprehensive tests with clear documentation
- Follow Test-Driven Development when refactoring or modifying existing functionality
- Always write tests for new functionality you implement
- Make a note of when tests for some functionality have been completed. If you
  cannot run the tests, ask me to run them manually, then confirm whether they succeeded or failed.
- Keep changes focused and minimal
- Follow existing code patterns
- Prefer the `ddt` package for parameterized tests to reduce code duplication

## Documentation & Institutional Memory

- Document new functionality in relevant docstrings and comments
- When you learn something important about how this codebase works (gotchas, non-obvious
  patterns, integration quirks), capture it in the relevant documentation or
  suggest adding it to `docs/architecture_overview.rst`
- These docs are institutional memory - future sessions (yours or others) will benefit
  from what you record here

## Architecture Overview

This is a Django service for managing enterprise financial subsidies, part of the Open edX ecosystem.
The `docs/architecture_overview.rst` contains comprehensive documentation on the service architecture
and should be consulted when you need to understand the entire service beyond what's written below.

### Core Applications

- **api** - REST API endpoints with versioned views (v1/, v2/), serializers, and filters
- **subsidy** - Core subsidy domain models, including Subsidy model with ledger integration
- **transaction** - Transaction management, ledger transactions, and reversals using openedx-ledger
- **content_metadata** - Content metadata API integration and caching with versioned cache keys
- **fulfillment** - Handles fulfillment of subsidies, including GEAG (Get Enrolled & Get Assigned) patterns
- **api_client** - External service integration (Enterprise API, Enterprise Catalog, LMS User API)
- **core** - Shared utilities, context processors, and base functionality

### Key Concepts

- **Subsidies**: Store value (learner credit in USD cents or subscription seats) that can be redeemed for educational content
- **Ledger**: Tracks transactions (value movement in/out of subsidies) using the openedx-ledger package
- **Transactions**: Individual debit/credit entries with content keys, learner emails, amounts, and idempotency keys
- **Redemption**: The act of redeeming stored value for specific content
- **Revenue Categories**: Control revenue recognition (bulk-enrollment-prepay, partner-no-rev-prepay)
- **Reference Types**: Links subsidies to originating objects (e.g., Salesforce OpportunityLineItem)
- **Fulfillment**: The process of converting a subsidy redemption into an actual enrollment

### External Service Integration

- **Enterprise Access**: Determines access policies and approval workflows
- **Enterprise Catalog**: Content metadata and pricing information
- **LMS (edxapp/edx-enterprise)**: User management and course enrollment operations
- **Discovery Service**: Source of truth for content pricing and metadata
- **Event Bus (Kafka)**: Event-driven communication between services via openedx-events

### Local Development

- This service may be included in the [edx/devstack](https://github.com/openedx/devstack) repository for integration testing alongside the rest of the Open edX ecosystem
- Server runs on `localhost:18280`
- Uses MySQL 8.0, Memcache for caching, and Kafka for event bus
- For event bus development: `make dev.up.with-events` (Confluent Control Center at http://localhost:9021/clusters)
- Event consumption: `./manage.py consume_enterprise_ping_events`
- Event production: `./manage.py produce_enterprise_ping_event`

## Testing Notes

- Uses pytest with Django integration
- Coverage reporting enabled by default
- PII annotation checks required for Django models
- Test commands:
  - `make test`: Run tests with coverage
  - `make validate`: Run all tests, quality checks, PII checks, and keyword checks
  - `pytest ./path/to/tests`: Run specific tests

## Data Model Architecture

- Subsidies use django-simple-history for change tracking
- Soft deletion pattern with `is_soft_deleted` field
- Integration with edx-rbac for role-based access control
- Uses openedx-ledger for transaction tracking and balance management
- Versioned cache keys for key-based cache invalidation (see `docs/caching.rst`)

## Additional Documentation

- `docs/architecture_overview.rst`: Comprehensive architecture guide for developers new to the edX ecosystem
- `docs/caching.rst`: Cache design, TieredCache usage, and versioned cache key patterns
- `docs/decisions/`: Architectural Decision Records (ADRs) documenting key design choices
