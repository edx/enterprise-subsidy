"""
Read-only helpers that find split enrollments needing attention and decide what reconciliation would do with them.

Phase 1a only reports. The --fix rules that act on these decisions are separate work; see
docs/references/split_enrollment_records.md.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db.models import Q
from openedx_ledger.models import Transaction, TransactionStateChoices

from enterprise_subsidy.apps.subsidy.models import Subsidy
from enterprise_subsidy.apps.transaction.models import SplitEnrollmentRecord, SplitEnrollmentStates

# A request owns a pending split for at most the worker timeout (300 s), so older ones are stuck.
PENDING_MAX_AGE = timedelta(minutes=10)
# A record still reversing after this long means a refund cascade step failed.
REVERSING_MAX_AGE = timedelta(minutes=15)
# How far back to look for a live split that also has another live redemption of the same enrollment.
DUPLICATE_WINDOW = timedelta(days=7)

RULE_STALE_PENDING = 'stale_pending'
RULE_STALE_REVERSING = 'stale_reversing'
RULE_DUPLICATE_LIVE_REDEMPTION = 'duplicate_live_redemption'

ACTION_FAIL_ALL_LEGS = 'fail_all_legs'
ACTION_RESEND_ENROLL = 'resend_enroll_to_learn_fulfillment'
ACTION_VERIFY_FULFILLMENT = 'verify_fulfillment_then_commit'
ACTION_COMMIT_REMAINING_LEGS = 'commit_remaining_legs'
ACTION_REVERSE_REMAINING_LEGS = 'reverse_remaining_legs'
ACTION_ALERT_DUPLICATE = 'alert_duplicate_live_redemption'
ACTION_MANUAL_REVIEW = 'manual_review'

LEG_SOURCE_ROW = 'leg_row'
LEG_SOURCE_LEDGER_KEY = 'ledger_key'
LEG_SOURCE_MISSING = 'missing'

# A ledger transaction that still holds (or may still hold) value for its enrollment.
LIVE_TRANSACTION = (
    Q(state__in=[TransactionStateChoices.CREATED, TransactionStateChoices.PENDING])
    | (
        Q(state=TransactionStateChoices.COMMITTED)
        & (Q(reversal__isnull=True) | ~Q(reversal__state=TransactionStateChoices.COMMITTED))
    )
)


def split_leg_idempotency_key(record_uuid, leg_order):
    """
    The ledger idempotency key of a split leg's transaction. The split redemption must create legs with this key,
    so reconciliation can find a leg's transaction even if its leg row was never written.
    """
    return f'split-{record_uuid}-{leg_order}'


@dataclass(frozen=True)
class LegEvidence:
    """
    What reconciliation found for one leg of a split.
    """
    leg_order: int
    source: str
    transaction: Optional[Transaction] = None

    @property
    def state(self):
        return self.transaction.state if self.transaction else None

    @property
    def is_reversed(self):
        reversal = getattr(self.transaction, 'reversal', None) if self.transaction else None
        return bool(reversal and reversal.state == TransactionStateChoices.COMMITTED)

    def __str__(self):
        if not self.transaction:
            return f'leg {self.leg_order}: missing'
        reversed_note = ', reversed' if self.is_reversed else ''
        return f'leg {self.leg_order}: {self.transaction.uuid} {self.state}{reversed_note} (via {self.source})'


def select_stale_pending(now, records=None):
    """
    Pending records created more than PENDING_MAX_AGE ago.
    """
    records = SplitEnrollmentRecord.objects.all() if records is None else records
    return records.filter(state=SplitEnrollmentStates.PENDING, created__lt=now - PENDING_MAX_AGE).order_by('created')


def select_stale_reversing(now, records=None):
    """
    Records that entered reversing more than REVERSING_MAX_AGE ago. transition_to() updates ``modified``.
    """
    records = SplitEnrollmentRecord.objects.all() if records is None else records
    return records.filter(
        state=SplitEnrollmentStates.REVERSING, modified__lt=now - REVERSING_MAX_AGE,
    ).order_by('created')


def select_duplicate_live_redemptions(now, records=None):
    """
    Live splits created within DUPLICATE_WINDOW whose enrollment also has another live redemption, mapped to the
    uuids of those other live transactions: {record: [transaction uuid, ...]}, in record creation order. A failed or
    reversed split no longer holds its enrollment, so a later redemption of it is expected, not a duplicate.

    This is the only implementation of the rule. It uses two queries however many splits there are: the recent live
    splits, then every live transaction for their enrollments, matched exactly in Python. The database ``IN`` filter
    only narrows the rows (on MySQL it compares content keys case-insensitively).
    """
    records = SplitEnrollmentRecord.objects.all() if records is None else records
    recent_live = list(records.filter(is_live=True, created__gte=now - DUPLICATE_WINDOW).order_by('created'))
    if not recent_live:
        return {}

    live_by_enrollment = defaultdict(list)
    for customer_uuid, lms_user_id, content_key, idempotency_key, split_record_uuid, uuid in Transaction.objects.filter(
        LIVE_TRANSACTION,
        ledger__subsidy__enterprise_customer_uuid__in={record.enterprise_customer_uuid for record in recent_live},
        lms_user_id__in={record.lms_user_id for record in recent_live},
        content_key__in={record.content_key for record in recent_live},
    ).values_list(
        'ledger__subsidy__enterprise_customer_uuid', 'lms_user_id', 'content_key', 'idempotency_key',
        'split_leg__split_record_id', 'uuid',
    ):
        live_by_enrollment[(customer_uuid, lms_user_id, content_key)].append(
            (idempotency_key, split_record_uuid, uuid)
        )

    duplicates = {}
    for record in recent_live:
        own_key_prefix = split_leg_idempotency_key(record.uuid, '')
        others = [
            str(uuid)
            for idempotency_key, split_record_uuid, uuid in live_by_enrollment[
                (record.enterprise_customer_uuid, record.lms_user_id, record.content_key)
            ]
            if not idempotency_key.startswith(own_key_prefix) and split_record_uuid != record.uuid
        ]
        if others:
            duplicates[record] = others
    return duplicates


def find_legs(record):
    """
    Return a LegEvidence per leg, in leg order. Each planned leg is looked up by its leg row, else by its ledger
    idempotency key, so a leg is found even if a crash came before its leg row was written.
    """
    leg_rows = {
        leg.leg_order: leg
        for leg in record.legs.select_related('transaction__reversal')
    }
    planned_subsidies = {planned['leg_order']: planned['subsidy_uuid'] for planned in record.planned_legs}

    evidence = []
    for leg_order in sorted(planned_subsidies.keys() | leg_rows.keys()):
        if leg_order in leg_rows:
            evidence.append(LegEvidence(leg_order, LEG_SOURCE_ROW, leg_rows[leg_order].transaction))
            continue
        subsidy = Subsidy.all_objects.filter(uuid=planned_subsidies[leg_order]).first()
        transaction = None
        if subsidy and subsidy.ledger_id:
            transaction = Transaction.objects.select_related('reversal').filter(
                ledger_id=subsidy.ledger_id,
                idempotency_key=split_leg_idempotency_key(record.uuid, leg_order),
            ).first()
        source = LEG_SOURCE_LEDGER_KEY if transaction else LEG_SOURCE_MISSING
        evidence.append(LegEvidence(leg_order, source, transaction))
    return evidence


def describe_action(record, legs):
    """
    The action reconciliation would take for a stale record, per the spec's reconciliation rules.
    """
    if record.state == SplitEnrollmentStates.REVERSING:
        return ACTION_REVERSE_REMAINING_LEGS
    if record.state != SplitEnrollmentStates.PENDING:
        return ACTION_MANUAL_REVIEW

    states = {leg.state for leg in legs}
    if not legs or states == {None}:
        # Crashed before any leg was created, so the enroll was never attempted.
        return ACTION_FAIL_ALL_LEGS
    if any(leg.is_reversed for leg in legs):
        return ACTION_MANUAL_REVIEW
    if TransactionStateChoices.COMMITTED in states:
        if states <= {TransactionStateChoices.COMMITTED, TransactionStateChoices.PENDING}:
            return ACTION_COMMIT_REMAINING_LEGS
        return ACTION_MANUAL_REVIEW
    if TransactionStateChoices.CREATED in states:
        # Legs move to pending only after every leg exists, so the enroll was never attempted.
        return ACTION_FAIL_ALL_LEGS
    if None in states:
        # A missing leg next to pending ones breaks the contract: the leg likely exists but wasn't found, so it's
        # unknown whether the enroll happened.
        return ACTION_MANUAL_REVIEW
    if states == {TransactionStateChoices.PENDING}:
        return ACTION_VERIFY_FULFILLMENT if record.fulfillment_identifier else ACTION_RESEND_ENROLL
    return ACTION_MANUAL_REVIEW
