"""
Defines django-filter/DRF FilterSets
for our API views.
"""
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_filters import filters
from django_filters import rest_framework as drf_filters
from openedx_ledger.models import Transaction, TransactionStateChoices
from rest_framework.exceptions import ValidationError


class HelpfulFilterSet(drf_filters.FilterSet):
    """
    Using an explicit FilterSet object works nicely with drf-spectacular
    for API schema documentation, and injecting the help_text from the model
    field into the filter field causes the help_text value to be rendered
    in the API docs alongside the query parameter names for each filter.

    This implementation is copied from a tip in the django-filter docs:
    https://django-filter.readthedocs.io/en/stable/guide/tips.html#adding-model-field-help-text-to-filters
    """
    @classmethod
    def filter_for_field(cls, field, field_name, lookup_expr=None):
        filter_obj = super(HelpfulFilterSet, cls).filter_for_field(field, field_name, lookup_expr)
        filter_obj.extra['help_text'] = field.help_text
        return filter_obj


class TransactionAdminFilterSet(HelpfulFilterSet):
    """
    Filters for admin transaction list action.
    """
    # It's important that this is filtered as a `MultipleChoiceFilter`,
    # which allows us to specify multiple matching state values
    # in the request query params - this filter type performs an OR
    # by default.
    # https://django-filter.readthedocs.io/en/main/ref/filters.html?highlight=MultipleChoiceFilter#multiplechoicefilter
    state = filters.MultipleChoiceFilter(
        field_name='state',
        choices=TransactionStateChoices.CHOICES,
    )
    start_date = filters.CharFilter(
        method='filter_start_date',
        help_text='Only include transactions created on/after this ISO date or datetime (a date means 00:00:00).',
    )
    end_date = filters.CharFilter(
        method='filter_end_date',
        help_text='Only include transactions created on/before this ISO date or datetime (a date means 23:59:59).',
    )

    @staticmethod
    def _parse_date(value, end_of_day=False):
        """
        Parse an ISO date or datetime into an aware datetime; bare dates snap to the start or end of that day.
        """
        # Raise DRF's ValidationError (not Django's) so an invalid value surfaces as a 400, not a 500.
        invalid_value_error = ValidationError({'detail': f'{value} is not a valid ISO date or datetime.'})
        try:
            # parse_date/parse_datetime raise ValueError for well-formed but impossible values like 2024-02-30.
            parsed_datetime = parse_datetime(value)
            parsed_date = parse_date(value) if parsed_datetime is None else None
        except ValueError as exc:
            raise invalid_value_error from exc
        if parsed_datetime is None:
            if parsed_date is None:
                raise invalid_value_error
            parsed_datetime = datetime.combine(
                parsed_date,
                datetime.max.time() if end_of_day else datetime.min.time(),
            )
        if timezone.is_naive(parsed_datetime):
            parsed_datetime = timezone.make_aware(parsed_datetime)
        return parsed_datetime

    def filter_start_date(self, queryset, name, value):
        return queryset.filter(created__gte=self._parse_date(value))

    def filter_end_date(self, queryset, name, value):
        return queryset.filter(created__lte=self._parse_date(value, end_of_day=True))

    class Meta:
        model = Transaction
        fields = [
            'lms_user_id',
            'content_key',
            'subsidy_access_policy_uuid',
            'state',
        ]
