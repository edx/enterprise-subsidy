"""
Test migrations.
"""
import uuid
from io import StringIO

import django.utils.timezone
import pytest
from django.core.management import call_command
from openedx_ledger.constants import INITIAL_DEPOSIT_TRANSACTION_SLUG
from openedx_ledger.models import SalesContractReferenceProvider as CurrentSalesContractReferenceProvider
from openedx_ledger.models import TransactionStateChoices

from enterprise_subsidy.apps.subsidy import models as subsidy_models
from enterprise_subsidy.apps.subsidy.constants import (
    STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_NAME,
    STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG
)

MIGRATION_0023 = ("subsidy", "0023_mariadb_uuid_conversion")
MIGRATION_0024 = ("subsidy", "0024_add_stripe_payment_intent_reference_provider")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "initial_deposit_exists,subsidy_exists,subsidy_reference_id,subsidy_reference_type",
    [
        (False, False, None,     "salesforce_opportunity_line_item"),
        (False, True,  None,     "salesforce_opportunity_line_item"),
        # This is the most common case that we're targetting in prod.
        (False, True,  "abc123", "salesforce_opportunity_line_item"),
        # This specific case defends against a case in stage where there was a Subsidy with a bogus reference_type.
        (False, True,  "abc123", "bogus_made_up"),
        (True,  False, None,     "salesforce_opportunity_line_item"),
        (True,  True,  None,     "salesforce_opportunity_line_item"),
        (True,  True,  "abc123", "salesforce_opportunity_line_item"),
    ],
)
def test_migration_0022_backfill_initial_deposits(
    migrator,
    initial_deposit_exists,
    subsidy_exists,
    subsidy_reference_id,
    subsidy_reference_type,
):
    """
    Test Backfilling initial deposits via data migration.
    """
    old_state = migrator.apply_initial_migration([
        ("subsidy", "0021_alter_historicalsubsidy_options"),
        ("openedx_ledger", "0012_optional_deposit_sales_contract_reference"),
    ])

    Subsidy = old_state.apps.get_model("subsidy", "Subsidy")
    Ledger = old_state.apps.get_model("openedx_ledger", "Ledger")
    Transaction = old_state.apps.get_model("openedx_ledger", "Transaction")
    Deposit = old_state.apps.get_model("openedx_ledger", "Deposit")
    HistoricalDeposit = old_state.apps.get_model("openedx_ledger", "HistoricalDeposit")
    SalesContractReferenceProvider = old_state.apps.get_model("openedx_ledger", "SalesContractReferenceProvider")

    ledger = Ledger.objects.create()
    subsidy = None
    if subsidy_exists:
        subsidy = Subsidy.objects.create(
            ledger=ledger,
            starting_balance=100,
            reference_id=subsidy_reference_id,
            reference_type=subsidy_reference_type,
            enterprise_customer_uuid=uuid.uuid4(),
        )
    transaction = Transaction.objects.create(
        ledger=ledger,
        idempotency_key=INITIAL_DEPOSIT_TRANSACTION_SLUG,
        quantity=subsidy.starting_balance if subsidy_exists else 100,
        state=TransactionStateChoices.COMMITTED
    )
    if initial_deposit_exists:
        sales_contract_reference_provider = None
        if subsidy_exists:
            sales_contract_reference_provider = SalesContractReferenceProvider.objects.create(
                slug=subsidy.reference_type,
                name="Foo Bar",
            )
        Deposit.objects.create(
            ledger=ledger,
            desired_deposit_quantity=transaction.quantity,
            transaction=transaction,
            sales_contract_reference_id=subsidy_reference_id,  # Sometimes this is None.
            sales_contract_reference_provider=sales_contract_reference_provider,
        )
        HistoricalDeposit.objects.create(
            ledger=ledger,
            desired_deposit_quantity=transaction.quantity,
            transaction=transaction,
            sales_contract_reference_id=subsidy_reference_id,
            sales_contract_reference_provider=sales_contract_reference_provider,
            history_date=django.utils.timezone.now(),
            history_type="+",
            history_change_reason="Data migration to backfill initial deposits",
        )

    new_state = migrator.apply_tested_migration(
        ("subsidy", "0022_backfill_initial_deposits"),
    )
    Deposit = new_state.apps.get_model("openedx_ledger", "Deposit")
    HistoricalDeposit = new_state.apps.get_model("openedx_ledger", "HistoricalDeposit")

    # Make sure there is exactly one deposit, suggesting that if one already existed it is not re-created.
    assert Deposit.objects.all().count() == 1
    assert HistoricalDeposit.objects.all().count() == 1

    # Finally check that all the deposit values are correct.
    deposit = Deposit.objects.first()
    assert deposit.ledger.uuid == ledger.uuid
    assert deposit.desired_deposit_quantity == 100
    assert deposit.transaction.uuid == transaction.uuid
    if subsidy_exists:
        assert deposit.sales_contract_reference_id == subsidy_reference_id
        if subsidy_reference_type == "salesforce_opportunity_line_item":
            assert deposit.sales_contract_reference_provider.slug == subsidy.reference_type
        else:
            assert Deposit.objects.first().sales_contract_reference_provider is None
    else:
        assert deposit.sales_contract_reference_id is None
        assert deposit.sales_contract_reference_provider is None

    historical_deposit = HistoricalDeposit.objects.first()
    assert historical_deposit.ledger.uuid == ledger.uuid
    assert historical_deposit.desired_deposit_quantity == 100
    assert historical_deposit.transaction.uuid == transaction.uuid
    assert historical_deposit.sales_contract_reference_id == deposit.sales_contract_reference_id
    assert historical_deposit.sales_contract_reference_provider == deposit.sales_contract_reference_provider
    assert historical_deposit.history_type == "+"
    assert historical_deposit.history_change_reason == "Data migration to backfill initial deposits"


def test_stripe_payment_intent_slug_fits_provider_slug_field():
    """
    The slug must be exactly this value (other services cite it) and must fit SalesContractReferenceProvider.slug.
    """
    assert STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG == "stripe_payment_intent"
    slug_max_length = CurrentSalesContractReferenceProvider._meta.get_field("slug").max_length
    assert len(STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG) <= slug_max_length


@pytest.mark.django_db
@pytest.mark.parametrize("provider_already_exists", [False, True])
def test_migration_0024_adds_stripe_payment_intent_provider(migrator, provider_already_exists):
    """
    The forward migration creates exactly one stripe_payment_intent provider, and is harmless to re-run when the
    provider already exists.
    """
    old_state = migrator.apply_initial_migration(MIGRATION_0023)
    SalesContractReferenceProvider = old_state.apps.get_model("openedx_ledger", "SalesContractReferenceProvider")
    # Clear out any row seeded by an earlier test database setup, so the starting state is what we say it is.
    SalesContractReferenceProvider.objects.filter(slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG).delete()
    if provider_already_exists:
        SalesContractReferenceProvider.objects.create(
            slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG,
            name="Pre-existing name",
        )

    new_state = migrator.apply_tested_migration(MIGRATION_0024)

    SalesContractReferenceProvider = new_state.apps.get_model("openedx_ledger", "SalesContractReferenceProvider")
    providers = SalesContractReferenceProvider.objects.filter(slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG)
    assert providers.count() == 1
    if provider_already_exists:
        # get_or_create must not overwrite an existing row.
        assert providers.get().name == "Pre-existing name"
    else:
        assert providers.get().name == STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_NAME


@pytest.mark.django_db
def test_migration_0024_reverse_deletes_nothing(migrator):
    """
    Reversing 0024 must keep the provider row, and therefore every Deposit citing it (the FK cascades on delete).
    """
    migrator.apply_initial_migration(MIGRATION_0023)
    new_state = migrator.apply_tested_migration(MIGRATION_0024)

    Ledger = new_state.apps.get_model("openedx_ledger", "Ledger")
    Transaction = new_state.apps.get_model("openedx_ledger", "Transaction")
    Deposit = new_state.apps.get_model("openedx_ledger", "Deposit")
    SalesContractReferenceProvider = new_state.apps.get_model("openedx_ledger", "SalesContractReferenceProvider")

    ledger = Ledger.objects.create()
    transaction = Transaction.objects.create(
        ledger=ledger,
        idempotency_key="top-up-deposit",
        quantity=100,
        state=TransactionStateChoices.COMMITTED,
    )
    deposit = Deposit.objects.create(
        ledger=ledger,
        desired_deposit_quantity=100,
        transaction=transaction,
        sales_contract_reference_id="pi_3Qexample",
        sales_contract_reference_provider=SalesContractReferenceProvider.objects.get(
            slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG,
        ),
    )

    reverted_state = migrator.apply_tested_migration(MIGRATION_0023)

    SalesContractReferenceProvider = reverted_state.apps.get_model("openedx_ledger", "SalesContractReferenceProvider")
    Deposit = reverted_state.apps.get_model("openedx_ledger", "Deposit")
    assert SalesContractReferenceProvider.objects.filter(slug=STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG).exists()
    assert Deposit.objects.filter(uuid=deposit.uuid).exists()


def test_stripe_payment_intent_is_not_a_subsidy_reference_type():
    """
    A subsidy is never created by a PaymentIntent, so the slug must not be an allowed Subsidy.reference_type.
    """
    allowed_slugs = [slug for slug, _ in subsidy_models.SubsidyReferenceChoices.CHOICES]
    assert STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG not in allowed_slugs
    field_choices = [slug for slug, _ in subsidy_models.Subsidy._meta.get_field("reference_type").choices]
    assert STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG not in field_choices


@pytest.mark.django_db
def test_no_pending_reference_type_migration():
    """
    makemigrations proposes no change to Subsidy.reference_type. Scoped to reference_type because main already has
    unrelated pending changes (Subsidy Meta options), so ``makemigrations --check`` exits non-zero regardless.
    """
    out = StringIO()
    try:
        call_command("makemigrations", "subsidy", "--check", "--dry-run", "--verbosity", "3", stdout=out)
    except SystemExit:
        pass
    output = out.getvalue()
    # Guard against passing vacuously: makemigrations must have actually reported on the subsidy app.
    assert "No changes detected" in output or "Migrations for 'subsidy'" in output
    assert "reference_type" not in output
