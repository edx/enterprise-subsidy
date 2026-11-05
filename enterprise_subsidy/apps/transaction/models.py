"""
Models for the transaction app.

A split enrollment is one enrollment funded by more than one subsidy. The ledger assumes one transaction per
enrollment, so these models link the ledger transactions (legs) that together fund one enrollment, without changing
openedx-ledger.
"""
import json
from uuid import uuid4

from django.db import models
from model_utils.models import TimeStampedModel

from enterprise_subsidy.apps.transaction.exceptions import SplitEnrollmentWriteOnceException


class SplitEnrollmentStates(models.TextChoices):
    """
    Lifecycle states of a SplitEnrollmentRecord.
    """
    PENDING = 'pending'
    COMMITTED = 'committed'
    REVERSING = 'reversing'
    REVERSED = 'reversed'
    FAILED = 'failed'


# A record in one of these states holds its enrollment: no other live split may exist for it.
LIVE_SPLIT_ENROLLMENT_STATES = frozenset({
    SplitEnrollmentStates.PENDING,
    SplitEnrollmentStates.COMMITTED,
    SplitEnrollmentStates.REVERSING,
})


class SplitEnrollmentRecordQuerySet(models.QuerySet):
    """
    Keeps ``is_live`` in sync with ``state``, and ``planned_legs`` write-once, on the bulk paths that bypass
    ``SplitEnrollmentRecord.save()``.
    """
    def update(self, **kwargs):
        if 'planned_legs' in kwargs:
            raise SplitEnrollmentWriteOnceException('planned_legs is write-once and cannot be changed by update().')
        if 'is_live' in kwargs:
            raise ValueError('is_live cannot be set directly; set state instead.')
        if 'state' in kwargs:
            state = kwargs['state']
            if not isinstance(state, str):
                raise ValueError('update() must set state to a plain value, so is_live can be derived from it.')
            kwargs['is_live'] = self.model.is_live_for_state(state)
        return super().update(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        for obj in objs:
            obj.is_live = obj.is_live_for_state(obj.state)
        return super().bulk_create(objs, *args, **kwargs)


class SplitEnrollmentLegQuerySet(models.QuerySet):
    """
    Legs are write-once, so bulk updates and deletes are blocked too.
    """
    def update(self, **kwargs):
        raise SplitEnrollmentWriteOnceException('Split enrollment legs cannot be updated.')

    def delete(self):
        raise SplitEnrollmentWriteOnceException('Split enrollment legs cannot be deleted.')


class SplitEnrollmentRecord(TimeStampedModel):
    """
    One enrollment funded by more than one subsidy. Each ``SplitEnrollmentLeg`` links one ledger Transaction
    that pays part of it.

    At most one record per (enterprise customer, learner, content) may be live. MySQL has no partial unique
    indexes, and Django skips conditional UniqueConstraints on MySQL, so liveness is stored in the nullable
    ``is_live`` column: True while live, NULL otherwise. NULLs never collide in a unique index, so any number of
    failed or reversed records may exist for the same enrollment. ``is_live`` is derived from ``state`` on every
    save and must never be False.

    .. no_pii:
    """

    class Meta:
        """
        Metaclass for SplitEnrollmentRecord.
        """
        constraints = [
            models.UniqueConstraint(
                fields=['enterprise_customer_uuid', 'lms_user_id', 'content_key', 'is_live'],
                name='unique_live_split',
            ),
        ]

    uuid = models.UUIDField(
        primary_key=True,
        default=uuid4,
        editable=False,
    )
    enterprise_customer_uuid = models.UUIDField(
        db_index=True,
        help_text="The one enterprise customer that all legs of this split share.",
    )
    lms_user_id = models.IntegerField(
        db_index=True,
        help_text="The LMS user id of the learner being enrolled.",
    )
    content_key = models.CharField(
        max_length=255,
        db_index=True,
        help_text="The course run being enrolled in.",
    )
    is_live = models.BooleanField(
        null=True,
        default=True,
        editable=False,
        help_text=(
            "True while pending, committed or reversing; NULL otherwise. Derived from state. Enforces one live "
            "split per enrollment."
        ),
    )
    state = models.CharField(
        max_length=16,
        choices=SplitEnrollmentStates.choices,
        default=SplitEnrollmentStates.PENDING,
    )
    failure_reason = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="The error code that failed this split.",
    )
    planned_legs = models.JSONField(
        help_text=(
            "Written once, from the request: "
            "[{leg_order, subsidy_uuid, subsidy_access_policy_uuid, quantity}]."
        ),
    )
    total_quantity = models.BigIntegerField(
        help_text="Negative USD cents; the sum of the legs.",
    )
    fulfillment_identifier = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "The enterprise fulfillment uuid of the one enrollment. Not unique: it repeats across re-enrollments."
        ),
    )

    objects = SplitEnrollmentRecordQuerySet.as_manager()

    @staticmethod
    def is_live_for_state(state):
        """
        Return the ``is_live`` value for the given state: True for live states, None otherwise (never False).
        """
        return True if state in LIVE_SPLIT_ENROLLMENT_STATES else None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        # The write-once check costs a SELECT. Save with update_fields (as transition_to() does) to skip it.
        if not self._state.adding and (update_fields is None or 'planned_legs' in update_fields):
            stored_planned_legs = SplitEnrollmentRecord.objects.filter(
                pk=self.pk,
            ).values_list('planned_legs', flat=True).first()
            # Compare as JSON, so an equal value of another Python type (a tuple, say) isn't treated as a change.
            if stored_planned_legs is not None and (
                json.dumps(stored_planned_legs, sort_keys=True) != json.dumps(self.planned_legs, sort_keys=True)
            ):
                raise SplitEnrollmentWriteOnceException(f'planned_legs of {self} is write-once.')
        self.is_live = self.is_live_for_state(self.state)
        if update_fields is not None and 'state' in update_fields:
            kwargs['update_fields'] = set(update_fields) | {'is_live'}
        super().save(*args, **kwargs)

    def transition_to(self, state, failure_reason=None):
        """
        Move this record to the given state, keeping ``is_live`` in sync.
        """
        self.state = state
        update_fields = ['state']
        if failure_reason is not None:
            self.failure_reason = failure_reason
            update_fields.append('failure_reason')
        self.save(update_fields=update_fields)

    def __str__(self):
        return (
            f'<SplitEnrollmentRecord uuid={self.uuid}, lms_user_id={self.lms_user_id}, '
            f'content_key={self.content_key}, state={self.state}>'
        )


class SplitEnrollmentLeg(TimeStampedModel):
    """
    One ledger Transaction paying part of a SplitEnrollmentRecord. Rows are write-once and cannot be deleted.

    The leg's amount, subsidy and budget are read from its Transaction, not copied here.

    .. no_pii:
    """

    class Meta:
        """
        Metaclass for SplitEnrollmentLeg.
        """
        constraints = [
            models.UniqueConstraint(
                fields=['split_record', 'leg_order'],
                name='unique_split_leg_order',
            ),
        ]

    uuid = models.UUIDField(
        primary_key=True,
        default=uuid4,
        editable=False,
    )
    split_record = models.ForeignKey(
        SplitEnrollmentRecord,
        on_delete=models.PROTECT,
        related_name='legs',
    )
    leg_order = models.PositiveSmallIntegerField(
        help_text="1 = the primary, drawn first.",
    )
    transaction = models.OneToOneField(
        'openedx_ledger.Transaction',
        on_delete=models.PROTECT,
        related_name='split_leg',
    )

    objects = SplitEnrollmentLegQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise SplitEnrollmentWriteOnceException(f'{self} is write-once.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise SplitEnrollmentWriteOnceException(f'{self} cannot be deleted.')

    def __str__(self):
        return (
            f'<SplitEnrollmentLeg uuid={self.uuid}, split_record={self.split_record_id}, '
            f'leg_order={self.leg_order}, transaction={self.transaction_id}>'
        )
