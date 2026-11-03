"""
Views for the enterprise-subsidy service relating to the Deposit model
"""
import logging

from django.utils import timezone
from django.utils.functional import cached_property
from drf_spectacular.utils import extend_schema
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from openedx_ledger.models import Deposit, LedgerLockAttemptFailed
from requests.exceptions import HTTPError
from rest_framework import generics, permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, Throttled
from rest_framework.response import Response

from enterprise_subsidy.apps.api.exceptions import DepositCreationAPIException, ErrorCodes, SubsidyAPIException
from enterprise_subsidy.apps.api.utils import get_subsidy_customer_uuid_from_view
from enterprise_subsidy.apps.api.v2.serializers.deposits import (
    DepositCreationError,
    DepositCreationRequestSerializer,
    DepositSerializer
)
from enterprise_subsidy.apps.subsidy.api import get_subsidy_by_uuid
from enterprise_subsidy.apps.subsidy.constants import PERMISSION_CAN_CREATE_DEPOSITS

logger = logging.getLogger(__name__)


def _is_ledger_lock_error(exc):
    """
    The ledger-lock error reaches the view wrapped twice (by openedx_ledger and by our serializer), so walk __cause__.
    """
    while exc is not None:
        if isinstance(exc, LedgerLockAttemptFailed):
            return True
        exc = exc.__cause__
    return False


class DepositAdminCreate(generics.CreateAPIView):
    """
    A create-only API view for deposits.

    This is only accessible to admins of the related subsidy's enterprise customer.
    """
    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DepositCreationRequestSerializer

    def __init__(self, *args, **kwargs):
        self.extra_context = {}
        super().__init__(*args, **kwargs)

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
        """
        return get_subsidy_by_uuid(self.requested_subsidy_uuid, should_raise=True)

    def set_created(self, created):
        """
        Note: This created context setter/getter framework is really only here for when we eventually make deposit
        creation actually idempotent, but for now it's just for show because we only ever pass True from the serializer.
        """
        self.extra_context['created'] = created

    @property
    def created(self):
        return self.extra_context.get('created', True)

    @permission_required(PERMISSION_CAN_CREATE_DEPOSITS, fn=get_subsidy_customer_uuid_from_view)
    @extend_schema(
        tags=['deposits'],
        request=DepositCreationRequestSerializer,
        responses={
            status.HTTP_200_OK: DepositSerializer,
            status.HTTP_201_CREATED: DepositSerializer,
            status.HTTP_403_FORBIDDEN: PermissionDenied,
            status.HTTP_409_CONFLICT: APIException,
            status.HTTP_429_TOO_MANY_REQUESTS: Throttled,
            status.HTTP_422_UNPROCESSABLE_ENTITY: APIException,
        },
    )
    def create(self, request, subsidy_uuid):
        """
        A create view that is accessible only to operators of the system.
        It creates a new Deposit record.
        """
        # Leave non-dict bodies (e.g. a JSON array) to the serializer, which rejects them with a 400.
        idempotency_key = request.data.get('idempotency_key') if isinstance(request.data, dict) else None
        if idempotency_key:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            # Must run before the expired-subsidy check, so that a replay still succeeds after the subsidy expires.
            existing_deposit = self._get_replayed_deposit(serializer.validated_data)
            if existing_deposit:
                return Response(DepositSerializer(existing_deposit).data, status=status.HTTP_200_OK)

        if self.subsidy.expiration_datetime < timezone.now():
            raise DepositCreationAPIException(
                detail='Cannot create a deposit in an expired subsidy',
                code=ErrorCodes.DEPOSIT_ON_EXPIRED_SUBSIDY_ERROR,
            )
        try:
            response = super().create(request, subsidy_uuid)
            if not self.created:
                response.status_code = status.HTTP_200_OK
            return response  # The default create() response status is HTTP_201_CREATED
        except (HTTPError, DepositCreationError) as exc:
            if idempotency_key:
                if _is_ledger_lock_error(exc):
                    raise SubsidyAPIException(
                        detail='Attempt to lock the Ledger failed, please try again.',
                        code=ErrorCodes.LEDGER_LOCK_ERROR,
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    ) from exc
                # We may have lost a race against a concurrent request with the same key, which committed its deposit
                # after our replay check above. If so, re-read and return the winner's deposit.
                existing_deposit = self._get_replayed_deposit(serializer.validated_data)
                if existing_deposit:
                    return Response(DepositSerializer(existing_deposit).data, status=status.HTTP_200_OK)
            raise DepositCreationAPIException(detail=str(exc)) from exc

    def _get_replayed_deposit(self, validated_data):
        """
        Returns the Deposit for the requested idempotency_key, or None. Raises a 409 if its values differ.
        """
        existing_deposit = Deposit.objects.filter(
            ledger=self.subsidy.ledger,
            transaction__idempotency_key=validated_data['idempotency_key'],
        ).first()
        if not existing_deposit:
            return None
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
