# Stripe PaymentIntent Reference Provider

## Overview
Self-service top-up deposits cite the Stripe PaymentIntent that funded them (**ENT-12394**). They do this through
the openedx-ledger `SalesContractReferenceProvider` row with slug `stripe_payment_intent` ("Stripe PaymentIntent"),
plus the PaymentIntent ID (`pi_…`) in `Deposit.sales_contract_reference_id`.

- **Constant:** `STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG` in `apps/subsidy/constants.py`
- **Created by:** data migration `subsidy.0024_add_stripe_payment_intent_reference_provider` (`get_or_create`, safe
  to re-run)

## Gotchas
- **Never delete this provider row**, including from the Django admin. `Deposit.sales_contract_reference_provider`
  is `on_delete=CASCADE`, so deleting the row deletes every top-up deposit that cites it. The reverse of migration
  0024 is a no-op on purpose, so rolling back the migration keeps the row.
- **It is not a `SubsidyReferenceChoices` value.** Those choices are also the allowed `Subsidy.reference_type`
  values, and a subsidy is never created by a PaymentIntent. Keep the slug as its own constant.
