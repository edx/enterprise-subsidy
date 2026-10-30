"""
Tests for the v2 deposit views.
"""
import uuid
from datetime import timedelta

import ddt
from django.utils import timezone
from openedx_ledger.models import Deposit, SalesContractReferenceProvider, Transaction
from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_subsidy.apps.api.v1.tests.mixins import STATIC_ENTERPRISE_UUID, APITestMixin
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
        if expected_response_status == status.HTTP_201_CREATED:
            assert response_x2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            assert response_x2.status_code == expected_response_status

    def _create_active_subsidy(self):
        """
        Helper to create an active, unexpired subsidy (with ledger) in the operator's enterprise.
        """
        return SubsidyFactory(
            enterprise_customer_uuid=STATIC_ENTERPRISE_UUID,
            active_datetime=timezone.now() - timedelta(days=1),
            expiration_datetime=timezone.now() + timedelta(days=1),
        )

    @ddt.data(
        ('subsidy_access_policy_uuid', '11111111-1111-1111-1111-111111111111'),
        ('lms_user_id', 12345),
        ('deposit_uuid', '22222222-2222-2222-2222-222222222222'),
        ('state', 'committed'),
        ('quantity', 999),
    )
    @ddt.unpack
    def test_deposit_creation_rejects_reserved_metadata_keys(self, reserved_key, reserved_value):
        """
        Metadata keys that clash with ``openedx_ledger.api.create_deposit()`` or ``create_transaction()``
        parameters are rejected with a 400 naming the key, and no Deposit or Transaction is created.
        """
        self.set_up_operator()
        subsidy = self._create_active_subsidy()
        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])
        deposit_count_before = Deposit.objects.count()
        transaction_count_before = Transaction.objects.count()

        response = self.client.post(url, {
            "desired_deposit_quantity": 100,
            "sales_contract_reference_id": str(uuid.uuid4()),
            "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            "metadata": {"source": "learner_credit_top_up", reserved_key: reserved_value},
        })

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'metadata' in response.data
        assert reserved_key in str(response.data['metadata'])
        assert Deposit.objects.count() == deposit_count_before
        assert Transaction.objects.count() == transaction_count_before

    @ddt.data(
        ["not", "an", "object"],
        "not-an-object",
    )
    def test_deposit_creation_rejects_non_object_metadata(self, metadata):
        """
        Metadata that is not a JSON object is rejected with a 400 instead of failing when unpacked as kwargs.
        """
        self.set_up_operator()
        subsidy = self._create_active_subsidy()
        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])
        deposit_count_before = Deposit.objects.count()

        response = self.client.post(url, {
            "desired_deposit_quantity": 100,
            "sales_contract_reference_id": str(uuid.uuid4()),
            "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            "metadata": metadata,
        })

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'metadata' in response.data
        assert Deposit.objects.count() == deposit_count_before

    def test_deposit_creation_stores_non_reserved_metadata(self):
        """
        Ordinary metadata keys are stored on the deposit's Transaction and do not leak into ledger columns.
        """
        self.set_up_operator()
        subsidy = self._create_active_subsidy()
        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])
        metadata = {
            "source": "learner_credit_top_up",
            "top_up_policy_uuid": str(uuid.uuid4()),
        }

        response = self.client.post(url, {
            "desired_deposit_quantity": 100,
            "sales_contract_reference_id": str(uuid.uuid4()),
            "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            "metadata": metadata,
        })

        assert response.status_code == status.HTTP_201_CREATED
        created_transaction = Deposit.objects.get(uuid=response.data["uuid"]).transaction
        assert created_transaction.metadata == metadata
        assert created_transaction.subsidy_access_policy_uuid is None

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
