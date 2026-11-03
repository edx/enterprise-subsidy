"""
Tests for the v2 deposit views.
"""
import uuid
from contextlib import nullcontext
from datetime import timedelta
from unittest import mock

import ddt
from django.utils import timezone
from openedx_ledger.models import Deposit, SalesContractReferenceProvider, Transaction, TransactionStateChoices
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_subsidy.apps.api.exceptions import ErrorCodes
from enterprise_subsidy.apps.api.v1.tests.mixins import STATIC_ENTERPRISE_UUID, APITestMixin
from enterprise_subsidy.apps.api.v2.views.deposit import DepositAdminCreate
from enterprise_subsidy.apps.subsidy.constants import STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG
from enterprise_subsidy.apps.subsidy.models import SubsidyReferenceChoices
from enterprise_subsidy.apps.subsidy.tests.factories import SubsidyFactory

# This test depends on data migration subsidy.0022_backfill_initial_deposits having been run to seed the
# SalesContractReferenceProvider table with a record that has this slug.
DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG = SubsidyReferenceChoices.SALESFORCE_OPPORTUNITY_LINE_ITEM


@ddt.ddt
class DepositCreateViewTests(APITestMixin):
    """
    Test deposit creation via the deposit-admin-create view.
    """

    @ddt.data(
        ###
        # Happy paths:
        ###
        {
            "subsidy_active": True,
            # Typical request we expect to see 99% of the time.
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
                "metadata": {"foo": "bar"},
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": False,
            "subsidy_expired": False,
            # Subsidy isn't active yet, but is unexpired and will be active in the future.
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
                "metadata": {"foo": "bar"},
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": True,
            # Only the minimal set of required request fields included.
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": True,
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
                # Test getting a null metadata value.
                "metadata": None,
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": True,
            # Top-up deposit citing the Stripe PaymentIntent which funded it (provider seeded by migration 0024).
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": "pi_3Qexample",
                "sales_contract_reference_provider": STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG,
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": True,
            # The exact request chunk 8 (enterprise-access, paid → deposited) sends for a top-up.
            "creation_request_data": {
                "desired_deposit_quantity": 100000,
                "sales_contract_reference_id": "pi_3Qexample",
                "sales_contract_reference_provider": STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG,
                "idempotency_key": "lc-top-up-3f1c2a9e-0000-4000-8000-000000000001",
                "metadata": {
                    "source": "learner_credit_top_up",
                    "learner_credit_top_up_uuid": "3f1c2a9e-0000-4000-8000-000000000001",
                    "top_up_policy_uuid": "3f1c2a9e-0000-4000-8000-000000000002",
                    "initiated_by_lms_user_id": 3,
                    "stripe_payment_intent_id": "pi_3Qexample",
                    "discount_rate": "0.00",
                    "charge_amount_cents": 100000,
                },
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },

        ###
        # Sad paths:
        ###
        {
            "subsidy_active": False,
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            },
            "expected_response_status": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "subsidy_expired": True,  # An expired subsidy invalidates request.
        },
        {
            "subsidy_active": True,
            "creation_request_data": {
                "desired_deposit_quantity": -100,  # Invalid deposit quantity.
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            },
            "expected_response_status": status.HTTP_400_BAD_REQUEST,
        },
        {
            "subsidy_active": True,
            "creation_request_data": {
                "desired_deposit_quantity": 0,  # Invalid deposit quantity.
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            },
            "expected_response_status": status.HTTP_400_BAD_REQUEST,
        },
        {
            "subsidy_active": True,
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": "totally-invalid-slug",  # Slug doesn't have existing object in db.
            },
            "expected_response_status": status.HTTP_400_BAD_REQUEST,
        },
    )
    @ddt.unpack
    def test_deposit_creation(
        self,
        subsidy_active,
        creation_request_data,
        expected_response_status,
        subsidy_expired=False,
    ):
        """
        Test that the DepositCreationRequestSerializer correctly creates a deposit idempotently OR fails with the
        correct status code.
        """
        self.set_up_operator()

        if subsidy_active:
            active_datetime = timezone.now() - timedelta(days=1)
            expiration_datetime = timezone.now() + timedelta(days=1)
        else:
            if subsidy_expired:
                active_datetime = timezone.now() - timedelta(days=2)
                expiration_datetime = timezone.now() - timedelta(days=1)
            else:
                active_datetime = timezone.now() + timedelta(days=1)
                expiration_datetime = timezone.now() + timedelta(days=2)

        subsidy = SubsidyFactory(
            enterprise_customer_uuid=STATIC_ENTERPRISE_UUID,
            active_datetime=active_datetime,
            expiration_datetime=expiration_datetime,
        )

        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])

        response = self.client.post(url, creation_request_data)
        assert response.status_code == expected_response_status
        if response.status_code < 300:
            assert response.data["ledger"] == subsidy.ledger.uuid
            assert response.data["desired_deposit_quantity"] == creation_request_data["desired_deposit_quantity"]
            assert response.data["sales_contract_reference_id"] == creation_request_data["sales_contract_reference_id"]
            assert response.data["sales_contract_reference_provider"] == \
                creation_request_data["sales_contract_reference_provider"]
            assert Deposit.objects.count() == 2
            created_deposit = Deposit.objects.get(uuid=response.data["uuid"])
            assert created_deposit.transaction.metadata == (creation_request_data.get("metadata") or {})
            if "idempotency_key" in creation_request_data:
                assert created_deposit.transaction.idempotency_key == creation_request_data["idempotency_key"]
        else:
            assert Deposit.objects.count() == 1

        response_x2 = self.client.post(url, creation_request_data)
        if expected_response_status == status.HTTP_201_CREATED and "idempotency_key" in creation_request_data:
            # A keyed replay returns the original deposit.
            assert response_x2.status_code == status.HTTP_200_OK
            assert response_x2.data == response.data
            assert Deposit.objects.count() == 2
        elif expected_response_status == status.HTTP_201_CREATED:
            assert response_x2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            assert response_x2.status_code == expected_response_status

    @ddt.data(
        {
            "role": "learner",
            "subsidy_enterprise_uuid": STATIC_ENTERPRISE_UUID,
            "expected_response_status": status.HTTP_403_FORBIDDEN,
        },
        {
            "role": "admin",
            "subsidy_enterprise_uuid": STATIC_ENTERPRISE_UUID,
            "expected_response_status": status.HTTP_403_FORBIDDEN,
        },
        {
            "role": "learner",
            "subsidy_enterprise_uuid": uuid.uuid4(),
            "expected_response_status": status.HTTP_403_FORBIDDEN,
        },
        {
            "role": "admin",
            "subsidy_enterprise_uuid": uuid.uuid4(),
            "expected_response_status": status.HTTP_403_FORBIDDEN,
        },
        {
            "role": "operator",
            "subsidy_enterprise_uuid": uuid.uuid4(),
            "expected_response_status": status.HTTP_201_CREATED,
        },
    )
    @ddt.unpack
    def test_learners_and_admins_cannot_create_deposits(
        self,
        role,
        subsidy_enterprise_uuid,
        expected_response_status,
    ):
        """
        Neither learner nor admin roles should provide the ability to create transactions.
        """
        if role == 'admin':
            self.set_up_admin()
        if role == 'learner':
            self.set_up_learner()
        if role == 'operator':
            self.set_up_operator()

        # Create a subsidy either in or not in the requesting user's enterprise.
        subsidy = SubsidyFactory(enterprise_customer_uuid=subsidy_enterprise_uuid)

        # Construct and make a request that is guaranteed to work if the user's role has correct permissions.
        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])
        creation_request_data = {
            "desired_deposit_quantity": 50,
            "sales_contract_reference_id": "abc123",
            "sales_contract_reference_provider": SalesContractReferenceProvider.objects.first().slug,
            "metadata": {"foo": "bar"},
        }
        response = self.client.post(url, creation_request_data)

        assert response.status_code == expected_response_status


@ddt.ddt
class KeyedDepositCreateViewTests(APITestMixin):
    """
    Test that deposit requests carrying an idempotency_key are safe to retry.
    """
    IDEMPOTENCY_KEY = 'K'

    def setUp(self):
        super().setUp()
        self.set_up_operator()
        self.subsidy = SubsidyFactory(
            enterprise_customer_uuid=STATIC_ENTERPRISE_UUID,
            active_datetime=timezone.now() - timedelta(days=1),
            expiration_datetime=timezone.now() + timedelta(days=1),
        )
        self.url = reverse("api:v2:deposit-admin-create", args=[self.subsidy.uuid])
        self.request_data = {
            "desired_deposit_quantity": 100000,
            "sales_contract_reference_id": "R",
            "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            "idempotency_key": self.IDEMPOTENCY_KEY,
        }

    def _keyed_deposits(self):
        return Deposit.objects.filter(ledger=self.subsidy.ledger, transaction__idempotency_key=self.IDEMPOTENCY_KEY)

    def test_first_keyed_request_creates_deposit(self):
        """
        The first keyed request creates exactly one Deposit and one committed Transaction.
        """
        response = self.client.post(self.url, self.request_data)

        assert response.status_code == status.HTTP_201_CREATED
        assert self._keyed_deposits().count() == 1
        transactions = Transaction.objects.filter(ledger=self.subsidy.ledger, idempotency_key=self.IDEMPOTENCY_KEY)
        assert transactions.count() == 1
        assert transactions.first().state == TransactionStateChoices.COMMITTED

    def test_replay_returns_original_deposit(self):
        """
        Repeating a keyed request returns the original deposit with a 200 and credits the ledger only once.
        """
        starting_balance = self.subsidy.ledger.balance()
        first_response = self.client.post(self.url, self.request_data)
        assert first_response.status_code == status.HTTP_201_CREATED

        replay_response = self.client.post(self.url, self.request_data)

        assert replay_response.status_code == status.HTTP_200_OK
        assert replay_response.data == first_response.data
        assert self._keyed_deposits().count() == 1
        assert self.subsidy.ledger.balance() == starting_balance + self.request_data['desired_deposit_quantity']

    def test_replay_after_subsidy_expires_returns_original_deposit(self):
        """
        The replay check runs before the expired-subsidy check, so a replay still returns 200 after expiry.
        """
        first_response = self.client.post(self.url, self.request_data)
        assert first_response.status_code == status.HTTP_201_CREATED
        self.subsidy.active_datetime = timezone.now() - timedelta(days=3)
        self.subsidy.expiration_datetime = timezone.now() - timedelta(days=1)
        self.subsidy.save()

        replay_response = self.client.post(self.url, self.request_data)

        assert replay_response.status_code == status.HTTP_200_OK
        assert replay_response.data == first_response.data

    def test_new_key_on_expired_subsidy_is_still_rejected(self):
        """
        A keyed request with no existing deposit still hits the expired-subsidy check.
        """
        self.subsidy.active_datetime = timezone.now() - timedelta(days=3)
        self.subsidy.expiration_datetime = timezone.now() - timedelta(days=1)
        self.subsidy.save()

        response = self.client.post(self.url, self.request_data)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data['code'] == ErrorCodes.DEPOSIT_ON_EXPIRED_SUBSIDY_ERROR
        assert not self._keyed_deposits().exists()

    @ddt.data(
        ('desired_deposit_quantity', 200000),
        ('sales_contract_reference_id', 'R2'),
        ('sales_contract_reference_provider', STRIPE_PAYMENT_INTENT_REFERENCE_PROVIDER_SLUG),
    )
    @ddt.unpack
    def test_same_key_with_different_values_is_conflict(self, field, changed_value):
        """
        Reusing a key with a different quantity, reference ID or provider returns 409 and creates nothing.
        """
        assert self.client.post(self.url, self.request_data).status_code == status.HTTP_201_CREATED
        deposit_count = Deposit.objects.count()
        transaction_count = Transaction.objects.count()

        response = self.client.post(self.url, {**self.request_data, field: changed_value})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data['code'] == ErrorCodes.DEPOSIT_IDEMPOTENCY_CONFLICT
        assert Deposit.objects.count() == deposit_count
        assert Transaction.objects.count() == transaction_count

    def test_ledger_lock_contention_is_retryable(self):
        """
        A keyed request against a locked ledger returns 429, and succeeds once the lock is released.
        """
        assert self.subsidy.ledger.acquire_lock()
        try:
            locked_response = self.client.post(self.url, self.request_data)
        finally:
            self.subsidy.ledger.release_lock()

        assert locked_response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert locked_response.data['code'] == ErrorCodes.LEDGER_LOCK_ERROR
        assert not self._keyed_deposits().exists()

        retry_response = self.client.post(self.url, self.request_data)
        assert retry_response.status_code == status.HTTP_201_CREATED
        assert self._keyed_deposits().count() == 1

    @ddt.data(
        # The winner committed after our get_or_create() looked for its Transaction, so our Transaction insert hits
        # the ledger's (ledger, idempotency_key) unique constraint.
        True,
        # The winner committed before our get_or_create(), which then returns the winner's Transaction, so our
        # Deposit insert hits the Deposit.transaction one-to-one constraint.
        False,
    )
    def test_loser_of_same_key_race_gets_winners_deposit(self, collide_on_transaction_key):
        """
        When two identical keyed requests both pass the replay check before either commits, the loser's insert
        fails on a unique constraint, and it re-reads and returns the winner's deposit with a 200.
        """
        winner_response = self.client.post(self.url, self.request_data)
        assert winner_response.status_code == status.HTTP_201_CREATED

        original_get_replayed_deposit = DepositAdminCreate._get_replayed_deposit  # pylint: disable=protected-access
        replay_checks = []

        def miss_first_replay_check(view, validated_data):
            # Simulate the loser passing the replay check before the winner committed.
            replay_checks.append(validated_data)
            if len(replay_checks) == 1:
                return None
            return original_get_replayed_deposit(view, validated_data)

        def insert_transaction(ledger, idempotency_key, defaults):
            return Transaction.objects.create(ledger=ledger, idempotency_key=idempotency_key, **defaults), True

        with mock.patch.object(
            DepositAdminCreate, '_get_replayed_deposit', autospec=True, side_effect=miss_first_replay_check,
        ), mock.patch.object(
            Transaction.objects, 'get_or_create', side_effect=insert_transaction,
        ) if collide_on_transaction_key else nullcontext():
            loser_response = self.client.post(self.url, self.request_data)

        assert len(replay_checks) == 2
        assert loser_response.status_code == status.HTTP_200_OK
        assert loser_response.data == winner_response.data
        assert self._keyed_deposits().count() == 1

    def test_unkeyed_duplicate_is_still_rejected(self):
        """
        Without a key, a duplicate request is unchanged and returns 422.
        """
        unkeyed_request_data = {k: v for k, v in self.request_data.items() if k != 'idempotency_key'}
        assert self.client.post(self.url, unkeyed_request_data).status_code == status.HTTP_201_CREATED

        response = self.client.post(self.url, unkeyed_request_data)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data['code'] == ErrorCodes.DEPOSIT_CREATION_ERROR
