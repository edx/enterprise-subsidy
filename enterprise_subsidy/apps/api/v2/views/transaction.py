"""
Views for the enterprise-subsidy service relating to the Transaction model
"""
import logging
from uuid import UUID

from django.http import StreamingHttpResponse
from django.utils.functional import cached_property
from django_filters import rest_framework as drf_filters
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from edx_rbac.decorators import permission_required
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from openedx_ledger.models import LedgerLockAttemptFailed, Transaction
from requests.exceptions import HTTPError
from rest_framework import filters, generics, permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException, NotFound, ParseError, PermissionDenied, Throttled
from rest_framework.renderers import BaseRenderer, JSONRenderer

from enterprise_subsidy.apps.api.csv_exports import iter_spend_report_csv
from enterprise_subsidy.apps.api.exceptions import ErrorCodes, TransactionCreationAPIException
from enterprise_subsidy.apps.api.filters import TransactionAdminFilterSet, TransactionExportFilterSet
from enterprise_subsidy.apps.api.paginators import TransactionListPaginator
from enterprise_subsidy.apps.api.utils import get_subsidy_customer_uuid_from_view
from enterprise_subsidy.apps.api.v1.serializers import (
    TransactionCreationError,
    TransactionCreationRequestSerializer,
    TransactionSerializer
)
from enterprise_subsidy.apps.fulfillment.api import FulfillmentException
from enterprise_subsidy.apps.subsidy.api import get_subsidy_by_uuid
from enterprise_subsidy.apps.subsidy.constants import (
    PERMISSION_CAN_CREATE_TRANSACTIONS,
    PERMISSION_CAN_READ_ALL_TRANSACTIONS,
    PERMISSION_CAN_READ_TRANSACTIONS
)
from enterprise_subsidy.apps.subsidy.models import ContentNotFoundForCustomerException, PriceValidationError, Subsidy

logger = logging.getLogger(__name__)


class TransactionBaseViewMixin:
    """
    Base view mixin that defines default authentication and permission classes;
    a subsidy-scoped Transaction queryset; and a default Transaction serializer.
    """
    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TransactionSerializer
    pagination_class = TransactionListPaginator
    queryset = Transaction.objects.all()

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

    def get_queryset(self):
        """
        A base queryset that selects all transaction records (along with their
        associated ledger, subsidy, reversals, and external references) for the requested ``subsidy_uuid``.
        """
        return Transaction.objects.select_related(
            'ledger',
            'ledger__subsidy',
            'reversal',
        ).prefetch_related(
            'external_reference',
            'external_reference__external_fulfillment_provider',
        ).filter(
            ledger__subsidy=self.subsidy
        )


class TransactionAdminListCreate(TransactionBaseViewMixin, generics.ListCreateAPIView):
    """
    A list view that is accessible only to admins
    of the related subsidy's enterprise customer.  It lists all transactions
    for the requested subsidy, or a subset thereof, depending on the query parameters.
    """
    filter_backends = [drf_filters.DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TransactionAdminFilterSet

    # fields that are queried for search
    search_fields = ['lms_user_email', 'content_title']

    # Settings that control list ordering, powered by OrderingFilter.
    # Fields in `ordering_fields` are what we allow to be passed to the "?ordering=" query param.
    ordering_fields = ['created', 'quantity']
    # `ordering` defines the default order.
    ordering = ['-created']

    def __init__(self, *args, **kwargs):
        self.extra_context = {}
        super().__init__(*args, **kwargs)

    def get_serializer_class(self):
        if self.request.method.lower() == 'get':
            return TransactionSerializer
        if self.request.method.lower() == 'post':
            return TransactionCreationRequestSerializer
        return None

    def set_transaction_was_created(self, created):
        self.extra_context['created'] = created

    @property
    def did_transaction_already_exist(self):
        return self.extra_context.get('created', True) is False

    # https://drf-spectacular.readthedocs.io/en/latest/faq.html#using-extend-schema-on-apiview-has-no-effect
    # @extend_schema needs to be applied to the entrypoint method of the view.
    # For APIView based views, these are get, post, create, etc.
    @extend_schema(
        tags=['transactions'],
        responses={
            status.HTTP_200_OK: TransactionSerializer,
            status.HTTP_403_FORBIDDEN: PermissionDenied,
        },
    )
    def get(self, *args, **kwargs):
        """
        A list view that is accessible only to admins
        of the related subsidy's enterprise customer.  It lists all transactions
        for the requested subsidy, or a subset thereof, depending on the query parameters.

        Note that `TransactionListPaginator`, the pagination_class for this view,
        allows for the inclusion of an `include_aggregates` query parameter,
        which if set to `true`, will include an `aggregates` key in the response
        describing the total quantity of transactions returned in the response `results`.
        """
        return super().get(*args, **kwargs)

    @permission_required(PERMISSION_CAN_READ_ALL_TRANSACTIONS, fn=get_subsidy_customer_uuid_from_view)
    def list(self, request, subsidy_uuid):
        """
        See docstring for get() above.
        """
        return super().list(request, subsidy_uuid)

    @extend_schema(
        tags=['transactions'],
        request=TransactionCreationRequestSerializer,
        responses={
            status.HTTP_200_OK: TransactionSerializer,
            status.HTTP_201_CREATED: TransactionSerializer,
            status.HTTP_403_FORBIDDEN: PermissionDenied,
            status.HTTP_429_TOO_MANY_REQUESTS: Throttled,
            status.HTTP_422_UNPROCESSABLE_ENTITY: APIException,
        },
    )
    def post(self, *args, **kwargs):
        """
        A create view that is accessible only to operators of the system.

        It creates (or just gets, if a matching Transaction is found with same ledger and idempotency_key) a
        transaction via the `Subsidy.redeem()` method.  Normally, the logic of this view
        is responsible for determining the price of the requested content key, with which
        the redeemed transaction's quantity will be valued.

        Note that, under some circumstances (for example, assigned learner content), it is
        appropriate and allowable for the *caller* of this view to request a specific price
        at which a redeemed transaction should occur.  In these circumstances, this service
        still does some validation of the requested price to ensure that it falls within
        a reasonable interval around the *true* price of the related content key. See:

        https://github.com/openedx/enterprise-access/blob/main/docs/decisions/0012-assignment-based-policies.rst
        https://github.com/openedx/enterprise-access/blob/main/docs/decisions/0014-assignment-price-validation.rst
        """
        return super().post(*args, **kwargs)

    @permission_required(PERMISSION_CAN_CREATE_TRANSACTIONS, fn=get_subsidy_customer_uuid_from_view)
    def create(self, request, subsidy_uuid):
        """
        See docstring for post() above.
        """
        if not self.subsidy.is_active:
            raise TransactionCreationAPIException(
                detail='Cannot create a transaction in an inactive subsidy',
                code=ErrorCodes.INACTIVE_SUBSIDY_CREATION_ERROR,
            )
        try:
            response = super().create(request, subsidy_uuid)
            if self.did_transaction_already_exist:
                response.status_code = status.HTTP_200_OK
            return response
        except LedgerLockAttemptFailed:
            raise Throttled(
                detail='Attempt to lock the Ledger failed, please try again.',
                code=ErrorCodes.LEDGER_LOCK_ERROR,
            )
        except HTTPError as exc:
            raise TransactionCreationAPIException(
                detail=str(exc),
                code=ErrorCodes.ENROLLMENT_ERROR,
            )
        except ContentNotFoundForCustomerException as exc:
            raise TransactionCreationAPIException(
                detail=str(exc),
                code=ErrorCodes.CONTENT_NOT_FOUND,
            )
        except PriceValidationError as exc:
            raise TransactionCreationAPIException(
                detail=str(exc),
                code=ErrorCodes.INVALID_REQUESTED_PRICE,
            )
        except FulfillmentException as exc:
            raise TransactionCreationAPIException(
                detail=str(exc),
                code=ErrorCodes.FULFILLMENT_ERROR,
            )
        except TransactionCreationError as exc:
            raise TransactionCreationAPIException(detail=str(exc))


@extend_schema(
    tags=['transactions'],
    responses={
        status.HTTP_200_OK: TransactionSerializer,
        status.HTTP_403_FORBIDDEN: PermissionDenied,
        status.HTTP_404_NOT_FOUND: NotFound,
    },
)
class TransactionUserList(TransactionBaseViewMixin, generics.ListAPIView):
    """
    Lists all transactions in the given ``subsidy_uuid`` with an ``lms_user_id``
    value equal to the requesting user's lms user id.
    """
    @cached_property
    def lms_user_id(self):
        """ Convenience property to get requesting user's lms_user_id value. """
        return self.request.user.lms_user_id

    def get_queryset(self):
        """
        Returns a queryset of transactions for the ``subsidy_uuid`` of the current request,
        filtered to those records  with an ``lms_user_id``
        value equal to the requesting user's lms user id.
        """
        base_queryset = super().get_queryset()
        return base_queryset.filter(
            lms_user_id=self.lms_user_id,
        )

    @permission_required(PERMISSION_CAN_READ_TRANSACTIONS, fn=get_subsidy_customer_uuid_from_view)
    def list(self, request, subsidy_uuid):
        """
        Lists all transactions in the given ``subsidy_uuid`` with an ``lms_user_id``
        value equal to the requesting user's lms user id.
        """
        if not self.lms_user_id:
            raise NotFound(detail='Could not determine lms_user_id in this request.')
        try:
            return super().list(request, subsidy_uuid)
        except Subsidy.DoesNotExist:
            raise NotFound(detail='The requested Subsidy record does not exist.')


class CSVPassthroughRenderer(BaseRenderer):
    """
    Accepts ``Accept: text/csv``. Only errors reach a renderer (the export streams), and they stay JSON.
    """
    media_type = 'text/csv'
    format = 'csv'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return JSONRenderer().render(data)


class TransactionAdminExport(TransactionBaseViewMixin, generics.GenericAPIView):
    """
    Streams a subsidy's learner spend as CSV, for admins of its enterprise and operators.
    """
    filter_backends = [drf_filters.DjangoFilterBackend, filters.SearchFilter]
    filterset_class = TransactionExportFilterSet
    search_fields = ['lms_user_email', 'content_title']
    renderer_classes = [JSONRenderer, CSVPassthroughRenderer]
    # DRF maps HEAD onto get(), which would build the whole report for nothing.
    http_method_names = ['get', 'options']
    # The inherited serializer_class and pagination_class are unused: rows go straight to CSV.

    def get_queryset(self):
        """
        The subsidy's spend, newest first.
        """
        return self.subsidy.spend_transactions().select_related('ledger').order_by('-created', 'uuid')

    def check_requested_enterprise_customer(self):
        """
        All-access callers (e.g. enterprise-access) may pass the enterprise they act for; 404 if it doesn't own the
        subsidy.
        """
        requested_customer_uuid = self.request.query_params.get('enterprise_customer_uuid')
        if not requested_customer_uuid:
            return
        try:
            requested_customer_uuid = UUID(requested_customer_uuid)
        except ValueError as exc:
            raise ParseError(f'{requested_customer_uuid} is not a valid uuid.') from exc
        if requested_customer_uuid != self.subsidy.enterprise_customer_uuid:
            raise NotFound(detail='The requested Subsidy record does not exist.')

    @extend_schema(
        tags=['transactions'],
        parameters=[
            OpenApiParameter(
                'enterprise_customer_uuid', OpenApiTypes.UUID,
                description='If given, the subsidy must belong to this enterprise customer (otherwise 404).',
            ),
            OpenApiParameter(
                'subsidy_access_policy_uuid', OpenApiTypes.UUID,
                description='Only include spend redeemed via this policy (budget).',
            ),
            OpenApiParameter(
                'start_date', OpenApiTypes.DATE,
                description='Only include spend on/after this date (UTC).',
            ),
            OpenApiParameter(
                'end_date', OpenApiTypes.DATE,
                description='Only include spend on/before this date, inclusive (UTC).',
            ),
            OpenApiParameter(
                'search', OpenApiTypes.STR,
                description='Only include spend whose learner email or course title contains this text.',
            ),
        ],
        responses={
            (status.HTTP_200_OK, 'text/csv'): OpenApiResponse(
                response=OpenApiTypes.BINARY,
                description='The spend report, as a UTF-8 CSV file attachment.',
            ),
            status.HTTP_400_BAD_REQUEST: OpenApiResponse(description='Invalid query parameters.'),
            status.HTTP_403_FORBIDDEN: PermissionDenied,
            status.HTTP_404_NOT_FOUND: NotFound,
        },
    )
    @permission_required(PERMISSION_CAN_READ_ALL_TRANSACTIONS, fn=get_subsidy_customer_uuid_from_view)
    def get(self, request, subsidy_uuid):
        """
        Streams the spend report for ``subsidy_uuid`` as a CSV attachment.
        """
        self.check_requested_enterprise_customer()
        # Validates the filters (400 on bad input) before the export is logged as started.
        transactions = self.filter_queryset(self.get_queryset())
        # Audit the bulk read of learner emails here, since callers may use service credentials.
        logger.info(
            'Learner credit spend export started: user_id=%s, subsidy_uuid=%s, '
            'enterprise_customer_uuid=%s, subsidy_access_policy_uuid=%s',
            request.user.id,
            subsidy_uuid,
            self.subsidy.enterprise_customer_uuid,
            request.query_params.get('subsidy_access_policy_uuid'),
        )
        response = StreamingHttpResponse(
            iter_spend_report_csv(transactions.iterator()),
            content_type='text/csv; charset=utf-8',
        )
        response['Content-Disposition'] = f'attachment; filename="spent_report_{subsidy_uuid}.csv"'
        return response
