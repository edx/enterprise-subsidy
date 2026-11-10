"""
Tests for the transaction app models.
"""
from uuid import uuid4

import ddt
import pytest
from django.db import IntegrityError, transaction
from django.db.models import F, ProtectedError
from django.test import TestCase

from enterprise_subsidy.apps.transaction.exceptions import SplitEnrollmentWriteOnceException
from enterprise_subsidy.apps.transaction.models import SplitEnrollmentLeg, SplitEnrollmentRecord, SplitEnrollmentStates
from enterprise_subsidy.apps.transaction.tests.factories import SplitEnrollmentLegFactory, SplitEnrollmentRecordFactory

LIVE_STATES = (SplitEnrollmentStates.PENDING, SplitEnrollmentStates.COMMITTED, SplitEnrollmentStates.REVERSING)
STATES_AND_IS_LIVE = (
    *((state, True) for state in LIVE_STATES),
    (SplitEnrollmentStates.REVERSED, None),
    (SplitEnrollmentStates.FAILED, None),
)


@ddt.ddt
class SplitEnrollmentRecordTestCase(TestCase):
    """
    Tests for the ``SplitEnrollmentRecord`` model.
    """
    def setUp(self):
        super().setUp()
        self.enrollment = {
            'enterprise_customer_uuid': uuid4(),
            'lms_user_id': 42,
            'content_key': 'course-v1:edX+DemoX+2026',
        }

    @ddt.data(*STATES_AND_IS_LIVE)
    @ddt.unpack
    def test_is_live_derived_from_state(self, state, expected_is_live):
        """
        is_live is True for live states and None (never False) otherwise, whatever value is set by hand.
        """
        assert SplitEnrollmentRecord.is_live_for_state(state) is expected_is_live

        record = SplitEnrollmentRecordFactory(state=state, is_live=False)
        record.refresh_from_db()
        assert record.is_live is expected_is_live

    @ddt.data(True, False)
    def test_state_changes_keep_is_live_in_sync(self, use_transition_to):
        record = SplitEnrollmentRecordFactory()
        assert record.is_live is True

        if use_transition_to:
            record.transition_to(SplitEnrollmentStates.FAILED, failure_reason='enrollment_error')
        else:
            record.state = SplitEnrollmentStates.FAILED
            record.save(update_fields=['state'])

        record.refresh_from_db()
        assert record.state == SplitEnrollmentStates.FAILED
        assert record.failure_reason == ('enrollment_error' if use_transition_to else None)
        assert record.is_live is None

    @ddt.data(*STATES_AND_IS_LIVE)
    @ddt.unpack
    def test_queryset_update_derives_is_live(self, state, expected_is_live):
        record = SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.REVERSING)

        SplitEnrollmentRecord.objects.filter(uuid=record.uuid).update(state=state)

        record.refresh_from_db()
        assert record.is_live is expected_is_live

    @ddt.data(
        ({'state': F('failure_reason')}, 'must set state to a plain value'),
        ({'is_live': None}, 'is_live cannot be set directly'),
        ({'is_live': True, 'state': SplitEnrollmentStates.FAILED}, 'is_live cannot be set directly'),
    )
    @ddt.unpack
    def test_queryset_update_rejects_underivable_is_live(self, kwargs, message):
        SplitEnrollmentRecordFactory()

        with pytest.raises(ValueError, match=message):
            SplitEnrollmentRecord.objects.update(**kwargs)

    def test_queryset_update_without_state_leaves_is_live(self):
        record = SplitEnrollmentRecordFactory()

        SplitEnrollmentRecord.objects.filter(uuid=record.uuid).update(fulfillment_identifier='abc')

        record.refresh_from_db()
        assert record.fulfillment_identifier == 'abc'
        assert record.is_live is True

    def test_bulk_create_derives_is_live(self):
        SplitEnrollmentRecord.objects.bulk_create([
            SplitEnrollmentRecordFactory.build(state=state, **self.enrollment)
            for state in (SplitEnrollmentStates.FAILED, SplitEnrollmentStates.REVERSED, SplitEnrollmentStates.PENDING)
        ])

        records = SplitEnrollmentRecord.objects.filter(**self.enrollment)
        assert {(record.state, record.is_live) for record in records} == {
            (SplitEnrollmentStates.FAILED, None),
            (SplitEnrollmentStates.REVERSED, None),
            (SplitEnrollmentStates.PENDING, True),
        }

    @ddt.data(*LIVE_STATES)
    def test_two_live_records_for_one_enrollment_conflict(self, second_state):
        SplitEnrollmentRecordFactory(**self.enrollment)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SplitEnrollmentRecordFactory(state=second_state, **self.enrollment)

    def test_moving_back_to_live_conflicts_with_existing_live_record(self):
        failed = SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.FAILED, **self.enrollment)
        SplitEnrollmentRecordFactory(**self.enrollment)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                failed.transition_to(SplitEnrollmentStates.PENDING)

    def test_many_released_records_allowed_for_one_enrollment(self):
        for _ in range(3):
            SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.FAILED, **self.enrollment)
            SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.REVERSED, **self.enrollment)
        SplitEnrollmentRecordFactory(**self.enrollment)

        records = SplitEnrollmentRecord.objects.filter(**self.enrollment)
        assert records.count() == 7
        assert records.filter(is_live__isnull=True).count() == 6

    @ddt.data(
        {'lms_user_id': 43},
        {'content_key': 'course-v1:edX+OtherX+2026'},
        {'enterprise_customer_uuid': uuid4()},
    )
    def test_live_records_for_different_enrollments_allowed(self, override):
        SplitEnrollmentRecordFactory(**self.enrollment)
        SplitEnrollmentRecordFactory(**{**self.enrollment, **override})

        assert SplitEnrollmentRecord.objects.filter(is_live=True).count() == 2

    def test_fulfillment_identifier_not_unique(self):
        SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.REVERSED, fulfillment_identifier='abc')
        SplitEnrollmentRecordFactory(state=SplitEnrollmentStates.COMMITTED, fulfillment_identifier='abc')

        assert SplitEnrollmentRecord.objects.filter(fulfillment_identifier='abc').count() == 2

    def test_planned_legs_write_once(self):
        record = SplitEnrollmentRecordFactory()
        planned_legs = record.planned_legs

        record.planned_legs = planned_legs[:1]
        with pytest.raises(SplitEnrollmentWriteOnceException):
            record.save()

        record = SplitEnrollmentRecord.objects.get(uuid=record.uuid)
        record.planned_legs = []
        with pytest.raises(SplitEnrollmentWriteOnceException):
            record.save()

        record.fulfillment_identifier = 'abc'
        record.planned_legs = planned_legs
        record.save()

        record.refresh_from_db()
        assert record.planned_legs == planned_legs
        assert record.fulfillment_identifier == 'abc'

    def test_planned_legs_equal_json_is_not_a_change(self):
        record = SplitEnrollmentRecord.objects.get(uuid=SplitEnrollmentRecordFactory().uuid)

        record.planned_legs = tuple(record.planned_legs)
        record.save()

        record.refresh_from_db()
        assert len(record.planned_legs) == 2

    def test_queryset_update_cannot_change_planned_legs(self):
        record = SplitEnrollmentRecordFactory()

        with pytest.raises(SplitEnrollmentWriteOnceException):
            SplitEnrollmentRecord.objects.filter(uuid=record.uuid).update(planned_legs=[])

        planned_legs = record.planned_legs
        record.refresh_from_db()
        assert record.planned_legs == planned_legs


class SplitEnrollmentLegTestCase(TestCase):
    """
    Tests for the ``SplitEnrollmentLeg`` model.
    """
    def setUp(self):
        super().setUp()
        self.record = SplitEnrollmentRecordFactory()
        self.leg = SplitEnrollmentLegFactory(split_record=self.record, leg_order=1)

    def test_legs_related_name(self):
        leg_2 = SplitEnrollmentLegFactory(split_record=self.record, leg_order=2)

        assert list(self.record.legs.order_by('leg_order')) == [self.leg, leg_2]
        assert self.leg.transaction.split_leg == self.leg

    def test_leg_order_unique_per_record(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SplitEnrollmentLegFactory(split_record=self.record, leg_order=1)

        # The same leg_order on another record is fine.
        SplitEnrollmentLegFactory(leg_order=1)

    def test_transaction_belongs_to_one_leg(self):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SplitEnrollmentLegFactory(split_record=self.record, leg_order=2, transaction=self.leg.transaction)

    def test_deleting_transaction_or_record_is_protected(self):
        with pytest.raises(ProtectedError):
            self.leg.transaction.delete()
        with pytest.raises(ProtectedError):
            self.record.delete()

    def test_leg_is_write_once(self):
        self.leg.leg_order = 2
        with pytest.raises(SplitEnrollmentWriteOnceException):
            self.leg.save()
        with pytest.raises(SplitEnrollmentWriteOnceException):
            SplitEnrollmentLeg.objects.filter(uuid=self.leg.uuid).update(leg_order=99)
        with pytest.raises(SplitEnrollmentWriteOnceException):
            self.leg.delete()
        with pytest.raises(SplitEnrollmentWriteOnceException):
            SplitEnrollmentLeg.objects.all().delete()

        self.leg.refresh_from_db()
        assert self.leg.leg_order == 1
        assert SplitEnrollmentLeg.objects.count() == 1
