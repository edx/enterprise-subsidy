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

    def _create_subsidy(self, subsidy_active=True, subsidy_expired=False):
        """
        Helper to create a subsidy (with ledger) in the operator's enterprise. An inactive subsidy is either expired or
        will become active in the future.
        """
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

        return SubsidyFactory(
            enterprise_customer_uuid=STATIC_ENTERPRISE_UUID,
            active_datetime=active_datetime,
            expiration_datetime=expiration_datetime,
        )

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
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
                # Test getting an empty metadata object.
                "metadata": {},
            },
            "expected_response_status": status.HTTP_201_CREATED,
        },
        {
            "subsidy_active": True,
            # Form clients send metadata as a JSON string, which must be parsed rather than dropped.
            "creation_request_data": {
                "desired_deposit_quantity": 100,
                "sales_contract_reference_id": str(uuid.uuid4()),
                "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
                "metadata": '{"foo": "bar"}',
            },
            "request_format": "multipart",
            "expected_metadata": {"foo": "bar"},
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
        request_format="json",
        expected_metadata=None,
    ):
        """
        Test that the DepositCreationRequestSerializer correctly creates a deposit idempotently OR fails with the
        correct status code.
        """
        self.set_up_operator()
        subsidy = self._create_subsidy(subsidy_active, subsidy_expired)

        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])

        response = self.client.post(url, creation_request_data, format=request_format)
        assert response.status_code == expected_response_status
        if response.status_code < 300:
            assert response.data["ledger"] == subsidy.ledger.uuid
            assert response.data["desired_deposit_quantity"] == creation_request_data["desired_deposit_quantity"]
            assert response.data["sales_contract_reference_id"] == creation_request_data["sales_contract_reference_id"]
            assert response.data["sales_contract_reference_provider"] == \
                creation_request_data["sales_contract_reference_provider"]
            assert Deposit.objects.count() == 2
            created_deposit = Deposit.objects.get(uuid=response.data["uuid"])
            assert created_deposit.transaction.metadata == (
                expected_metadata or creation_request_data.get("metadata") or {}
            )
            # Metadata must not leak into any ledger column a metadata key could have been unpacked into.
            for column in ("subsidy_access_policy_uuid", "lms_user_id", "lms_user_email", "content_key"):
                assert getattr(created_deposit.transaction, column) is None
            if "idempotency_key" in creation_request_data:
                assert created_deposit.transaction.idempotency_key == creation_request_data["idempotency_key"]
        else:
            assert Deposit.objects.count() == 1

        response_x2 = self.client.post(url, creation_request_data, format=request_format)
        if expected_response_status == status.HTTP_201_CREATED:
            assert response_x2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            assert response_x2.status_code == expected_response_status

    @ddt.data(
        ###
        # Keys that clash with openedx_ledger.api.create_deposit() or create_transaction() parameters.
        ###
        {
            "metadata": {
                "source": "learner_credit_top_up",
                "subsidy_access_policy_uuid": "11111111-1111-1111-1111-111111111111",
            },
            "expected_error_code": "invalid",
            "expected_reserved_keys": "subsidy_access_policy_uuid",
        },
        {
            "metadata": {"source": "learner_credit_top_up", "lms_user_id": 12345},
            "expected_error_code": "invalid",
            "expected_reserved_keys": "lms_user_id",
        },
        {
            "metadata": {"source": "learner_credit_top_up", "deposit_uuid": "22222222-2222-2222-2222-222222222222"},
            "expected_error_code": "invalid",
            "expected_reserved_keys": "deposit_uuid",
        },
        {
            "metadata": {"source": "learner_credit_top_up", "state": "committed"},
            "expected_error_code": "invalid",
            "expected_reserved_keys": "state",
        },
        {
            "metadata": {"source": "learner_credit_top_up", "quantity": 999},
            "expected_error_code": "invalid",
            "expected_reserved_keys": "quantity",
        },
        ###
        # Metadata that is not a JSON object, even when it is empty or falsy.
        ###
        {
            "metadata": ["not", "an", "object"],
            "expected_error_code": "not_a_dict",
        },
        {
            "metadata": "not-an-object",
            "expected_error_code": "not_a_dict",
        },
        {
            "metadata": [],
            "expected_error_code": "not_a_dict",
        },
        {
            "metadata": "",
            "expected_error_code": "not_a_dict",
        },
        {
            "metadata": 0,
            "expected_error_code": "not_a_dict",
        },
        {
            "metadata": False,
            "expected_error_code": "not_a_dict",
        },
        ###
        # Form clients send metadata as a JSON string; it is parsed and validated the same way.
        ###
        {
            "metadata": '{"source": "learner_credit_top_up", "state": "committed"}',
            "request_format": "multipart",
            "expected_error_code": "invalid",
            "expected_reserved_keys": "state",
        },
        {
            "metadata": '[1, 2]',
            "request_format": "multipart",
            "expected_error_code": "not_a_dict",
        },
    )
    @ddt.unpack
    def test_deposit_creation_rejects_invalid_metadata(
        self,
        metadata,
        expected_error_code,
        expected_reserved_keys=None,
        request_format="json",
    ):
        """
        Metadata that is not a JSON object, or whose keys clash with ledger function parameters, is rejected with a 400
        instead of failing when unpacked as kwargs, and no Deposit or Transaction is created.
        """
        self.set_up_operator()
        subsidy = self._create_subsidy()
        url = reverse("api:v2:deposit-admin-create", args=[subsidy.uuid])
        deposit_count_before = Deposit.objects.count()
        transaction_count_before = Transaction.objects.count()

        response = self.client.post(url, {
            "desired_deposit_quantity": 100,
            "sales_contract_reference_id": str(uuid.uuid4()),
            "sales_contract_reference_provider": DEFAULT_SALES_CONTRACT_REFERENCE_PROVIDER_SLUG,
            "metadata": metadata,
        }, format=request_format)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        [error] = response.data["metadata"]
        assert error.code == expected_error_code
        if expected_reserved_keys:
            # Only the text after the colon lists the keys; the message prefix itself mentions "ledger".
            assert str(error).split(": ", 1)[1] == expected_reserved_keys
        assert Deposit.objects.count() == deposit_count_before
        assert Transaction.objects.count() == transaction_count_before

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
