"""
Add the ``stripe_payment_intent`` SalesContractReferenceProvider, so that top-up deposits can cite the Stripe
PaymentIntent which funded them.

The reverse migration is intentionally a no-op. Deposit.sales_contract_reference_provider uses on_delete=CASCADE, so
deleting this provider row would also delete every Deposit that cites it.
"""
from django.db import migrations

from enterprise_subsidy.apps.subsidy.constants import (
    STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_NAME,
    STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG
)


def forwards_func(apps, schema_editor):  # pylint: disable=unused-argument
    """
    Idempotently create the stripe_payment_intent SalesContractReferenceProvider.
    """
    # We get the model from the versioned app registry; if we directly import it, it'll be the wrong version.
    SalesContractReferenceProvider = apps.get_model("openedx_ledger", "SalesContractReferenceProvider")
    SalesContractReferenceProvider.objects.get_or_create(
        slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG,
        defaults={"name": STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_NAME},
    )


class Migration(migrations.Migration):
    """
    Migration for adding the stripe_payment_intent sales contract reference provider.
    """
    dependencies = [
        ("subsidy", "0023_mariadb_uuid_conversion"),
    ]

    operations = [
        migrations.RunPython(forwards_func, reverse_code=migrations.RunPython.noop),
    ]
