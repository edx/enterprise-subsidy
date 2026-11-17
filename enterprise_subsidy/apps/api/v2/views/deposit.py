"""
Views for the enterprise-subsidy service relating to the Deposit model
"""
import logging

from django.utils import timezone
from django.utils.functional import cached_property
from drf_spectacular.utils import extend_schema
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from openedx_ledger.models import LedgerLockAttemptFailed, Transaction
from rest_framework import generics, permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, Throttled, ValidationError
from rest_framework.response import Response

from enterprise_subsidy.apps.api.exceptions import DepositCreationAPIException, ErrorCodes
from enterprise_subsidy.apps.api.utils import get_subsidy_customer_uuid_from_view
from enterprise_subsidy.apps.api.v2.serializers.deposits import (
    DepositCreationError,
    DepositCreationRequestSerializer,
    DepositSerializer
)
from enterprise_subsidy.apps.subsidy.api import get_subsidy_by_uuid
from enterprise_subsidy.apps.subsidy.constants import PERMISSION_CAN_CREATE_DEPOSITS
from enterprise_subsidy.apps.subsidy.models import Subsidy

logger = logging.getLogger(__name__)


class DepositAdminCreate(generics.CreateAPIView):
    """
    A create-only API view for deposits.

    This is only accessible to admins of the related subsidy's enterprise customer.
    """
    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DepositCreationRequestSerializer

    @property
    def requested_subsidy_uuid(self):
        """
        Returns the requested ``subsidy_uuid`` path parameter.
        """
        return self.kwargs.get('subsidy_uuid')

    @cached_property
    def subsidy(self):
        """
        Returns the Subsidy instance from the requested ``subsidy_uuid``.

        Raises NotFound (404) if the subsidy no longer exists, e.g. it was soft-deleted after the permission check.
        """
        try:
            return get_subsidy_by_uuid(self.requested_subsidy_uuid, should_raise=True)
        except Subsidy.DoesNotExist as exc:
            raise NotFound(detail=f'Subsidy {self.requested_subsidy_uuid} not found.') from exc

    @permission_required(PERMISSION_CAN_CREATE_DEPOSITS, fn=get_subsidy_customer_uuid_from_view)
    @extend_schema(
        tags=['deposits'],
        request=DepositCreationRequestSerializer,
        responses={
            status.HTTP_200_OK: DepositSerializer,
            status.HTTP_201_CREATED: DepositSerializer,
            status.HTTP_400_BAD_REQUEST: ValidationError,
            status.HTTP_403_FORBIDDEN: PermissionDenied,
            status.HTTP_404_NOT_FOUND: NotFound,
            status.HTTP_409_CONFLICT: APIException,
            status.HTTP_429_TOO_MANY_REQUESTS: Throttled,
            status.HTTP_422_UNPROCESSABLE_ENTITY: APIException,
        },
    )
    def create(self, request, subsidy_uuid):
        """
        A create view that is accessible only to operators of the system.
        It creates a new Deposit record.

        Responses:
          201: the deposit was created.
          200: a deposit with the requested ``idempotency_key`` already exists with the same quantity, sales contract
               reference ID and provider; it is returned unchanged.
          400: the request is invalid (checked first, even on an expired subsidy).
          404: the subsidy no longer exists.
          409: the ``idempotency_key`` is already used with different values, or by a non-deposit transaction.
          422: the subsidy has expired, or the deposit could not be created (including an unkeyed duplicate).
          429: another request holds the ledger lock; retry later.
        """
        # Validate first, so an invalid request is a 400 even on an expired subsidy, and a replay is only compared once
        # its values are known to be valid.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data
        idempotency_key = validated_data.get('idempotency_key')

        if idempotency_key is not None:
            # Must run before the expired-subsidy check, so that a replay still succeeds after the subsidy expires.
            existing_deposit = self._get_replayed_deposit(validated_data)
            if existing_deposit:
                return Response(DepositSerializer(existing_deposit).data, status=status.HTTP_200_OK)

        if self.subsidy.expiration_datetime < timezone.now():
            raise DepositCreationAPIException(
                detail='Cannot create a deposit in an expired subsidy',
                code=ErrorCodes.DEPOSIT_ON_EXPIRED_SUBSIDY_ERROR,
            )
        try:
            self.perform_create(serializer)
        except LedgerLockAttemptFailed as exc:
            raise Throttled(
                detail='Attempt to lock the Ledger failed, please try again.',
                code=ErrorCodes.LEDGER_LOCK_ERROR,
            ) from exc
        except DepositCreationError as exc:
            if idempotency_key is not None:
                # We may have lost a race against a concurrent request with the same key, which committed its deposit
                # after our replay check above. If so, re-read and return the winner's deposit (or a 409).
                existing_deposit = self._get_replayed_deposit(validated_data)
                if existing_deposit:
                    return Response(DepositSerializer(existing_deposit).data, status=status.HTTP_200_OK)
            # Return the generic default_detail: exc's message can carry ledger/DB internals. The serializer logs it.
            raise DepositCreationAPIException() from exc
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def _get_replayed_deposit(self, validated_data):
        """
        Returns the Deposit for the requested idempotency_key, or None. Raises a 409 if its values differ.
        """
        # One query: the Transaction and its Deposit are committed together, so read them from the same snapshot.
        existing_transaction = Transaction.objects.select_related('deposit').filter(
            ledger=self.subsidy.ledger,
            idempotency_key=validated_data['idempotency_key'],
        ).first()
        if not existing_transaction:
            return None
        existing_deposit = getattr(existing_transaction, 'deposit', None)
        if not existing_deposit:
            # openedx-ledger would reuse a non-deposit Transaction with this key (e.g. a redemption) and attach the new
            # Deposit to it without crediting the ledger.
            raise DepositCreationAPIException(
                detail=(
                    f'idempotency_key {validated_data["idempotency_key"]} is already used by a non-deposit '
                    'transaction in this subsidy.'
                ),
                code=ErrorCodes.DEPOSIT_IDEMPOTENCY_CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            existing_deposit.desired_deposit_quantity != validated_data['desired_deposit_quantity']
            or existing_deposit.sales_contract_reference_id != validated_data['sales_contract_reference_id']
            or existing_deposit.sales_contract_reference_provider != validated_data['sales_contract_reference_provider']
        ):
            raise DepositCreationAPIException(
                detail=(
                    f'A deposit with idempotency_key {validated_data["idempotency_key"]} already exists in this '
                    'subsidy with a different quantity, sales contract reference ID or provider.'
                ),
                code=ErrorCodes.DEPOSIT_IDEMPOTENCY_CONFLICT,
                status_code=status.HTTP_409_CONFLICT,
            )
        return existing_deposit
