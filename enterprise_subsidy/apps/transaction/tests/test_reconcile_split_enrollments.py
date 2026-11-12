"""
Tests for split enrollment reconciliation: the selection rules, leg lookup and actions, and the
reconcile_split_enrollments management command.
"""
from datetime import datetime, timedelta
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import ddt
import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from openedx_ledger.models import TransactionStateChoices
from openedx_ledger.test_utils.factories import LedgerFactory, ReversalFactory, TransactionFactory

from enterprise_subsidy.apps.core.utils import localized_utcnow
from enterprise_subsidy.apps.subsidy.tests.factories import SubsidyFactory
from enterprise_subsidy.apps.transaction import reconciliation
from enterprise_subsidy.apps.transaction.models import SplitEnrollmentRecord, SplitEnrollmentStates
from enterprise_subsidy.apps.transaction.tests.factories import SplitEnrollmentRecordFactory

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo('UTC'))
COMMAND_MODULE = 'enterprise_subsidy.apps.transaction.management.commands.reconcile_split_enrollments'
REVERSED = 'reversed'  # A committed leg transaction with a committed reversal.


class ReconciliationTestMixin:
    """
    Builds a customer with two subsidies and split records for one enrollment.
    """
    def setUp(self):
        super().setUp()
        self.customer_uuid = uuid4()
        self.lms_user_id = 42
        self.content_key = 'course-v1:edX+DemoX+2026'
        self.subsidies = [
            SubsidyFactory(enterprise_customer_uuid=self.customer_uuid, ledger=LedgerFactory()) for _ in range(2)
        ]

    def make_record(self, state=SplitEnrollmentStates.PENDING, created_ago=timedelta(hours=1), modified_ago=None,
                    **kwargs):
        """
        A split record funded by self.subsidies, with its created/modified times set relative to NOW.
        """
        kwargs.setdefault('lms_user_id', self.lms_user_id)
        kwargs.setdefault('content_key', self.content_key)
        record = SplitEnrollmentRecordFactory(
            state=state,
            enterprise_customer_uuid=self.customer_uuid,
            planned_legs=[
                {'leg_order': order, 'subsidy_uuid': str(subsidy.uuid), 'subsidy_access_policy_uuid': str(uuid4()),
                 'quantity': -1000}
                for order, subsidy in enumerate(self.subsidies, start=1)
            ],
            total_quantity=-2000,
            **kwargs,
        )
        SplitEnrollmentRecord.objects.filter(uuid=record.uuid).update(
            created=NOW - created_ago,
            modified=NOW - (created_ago if modified_ago is None else modified_ago),
        )
        record.refresh_from_db()
        return record

    def make_leg_transaction(self, record, leg_order, state=TransactionStateChoices.PENDING, with_leg_row=True):
        """
        The ledger transaction for one leg, keyed like the split redemption keys it, optionally with its leg row.
        State REVERSED makes a committed transaction with a committed reversal.
        """
        transaction = TransactionFactory(
            ledger=self.subsidies[leg_order - 1].ledger,
            idempotency_key=reconciliation.split_leg_idempotency_key(record.uuid, leg_order),
            lms_user_id=record.lms_user_id,
            content_key=record.content_key,
            state=TransactionStateChoices.COMMITTED if state == REVERSED else state,
        )
        if state == REVERSED:
            ReversalFactory(transaction=transaction)
        if with_leg_row:
            record.legs.create(leg_order=leg_order, transaction=transaction)
        return transaction

    def make_single_redemption(self, state=TransactionStateChoices.COMMITTED, subsidy=None, **kwargs):
        """
        A non-split ledger transaction for the same enrollment. State REVERSED adds a committed reversal.
        """
        kwargs.setdefault('lms_user_id', self.lms_user_id)
        kwargs.setdefault('content_key', self.content_key)
        transaction = TransactionFactory(
            ledger=(subsidy or self.subsidies[0]).ledger,
            state=TransactionStateChoices.COMMITTED if state == REVERSED else state,
            **kwargs,
        )
        if state == REVERSED:
            ReversalFactory(transaction=transaction)
        return transaction


@ddt.ddt
class SelectionRuleTests(ReconciliationTestMixin, TestCase):
    """
    Tests for the three selection rules.
    """
    @ddt.data(
        ('pending', 9, False),
        ('pending', 11, True),
        ('committed', 60, False),
        ('reversing', 60, False),
        ('failed', 60, False),
    )
    @ddt.unpack
    def test_stale_pending(self, state, minutes_old, selected):
        record = self.make_record(state=state, created_ago=timedelta(minutes=minutes_old))

        assert (record in reconciliation.select_stale_pending(NOW)) is selected

    @ddt.data(
        # The age is the time since the record entered reversing (modified), not since it was created.
        ('reversing', 14, False),
        ('reversing', 16, True),
        ('pending', 60, False),
        ('committed', 60, False),
        ('reversed', 60, False),
    )
    @ddt.unpack
    def test_stale_reversing(self, state, minutes_since_change, selected):
        record = self.make_record(
            state=state, created_ago=timedelta(days=2), modified_ago=timedelta(minutes=minutes_since_change),
        )

        assert (record in reconciliation.select_stale_reversing(NOW)) is selected

    @ddt.data((14, False), (16, True))
    @ddt.unpack
    def test_transition_to_reversing_starts_the_reversing_clock(self, minutes_after_transition, selected):
        record = self.make_record(state=SplitEnrollmentStates.COMMITTED, created_ago=timedelta(days=2))
        before_transition = localized_utcnow()

        record.transition_to(SplitEnrollmentStates.REVERSING)

        record.refresh_from_db()
        assert record.modified >= before_transition
        now = record.modified + timedelta(minutes=minutes_after_transition)
        assert (record in reconciliation.select_stale_reversing(now)) is selected

    @ddt.data(
        # (record state, record age in days, the other transaction, selected)
        ('committed', 6, 'committed', True),
        ('committed', 8, 'committed', False),
        ('pending', 1, 'created', True),
        ('reversing', 1, 'pending', True),
        ('committed', 1, 'another_subsidy_of_the_customer', True),
        ('failed', 1, 'committed', False),
        ('reversed', 1, 'committed', False),
        ('committed', 1, 'failed', False),
        ('committed', 1, REVERSED, False),
        ('committed', 1, 'another_customer', False),
        ('committed', 1, 'another_course', False),
        ('committed', 1, 'own_legs', False),
    )
    @ddt.unpack
    def test_duplicate_live_redemption(self, state, days_old, other, selected):
        record = self.make_record(state=state, created_ago=timedelta(days=days_old))
        others = []
        if other == 'another_subsidy_of_the_customer':
            subsidy = SubsidyFactory(enterprise_customer_uuid=self.customer_uuid, ledger=LedgerFactory())
            others.append(self.make_single_redemption(subsidy=subsidy))
        elif other == 'another_customer':
            self.make_single_redemption(subsidy=SubsidyFactory(ledger=LedgerFactory()))
        elif other == 'another_course':
            self.make_single_redemption(content_key='course-v1:edX+OtherX+2026')
        elif other == 'own_legs':
            self.make_leg_transaction(record, 1, state=TransactionStateChoices.COMMITTED)
            self.make_leg_transaction(record, 2, state=TransactionStateChoices.COMMITTED, with_leg_row=False)
        else:
            others.append(self.make_single_redemption(state=other, subsidy=self.subsidies[1]))

        duplicates = reconciliation.select_duplicate_live_redemptions(NOW)

        if selected:
            assert duplicates == {record: [str(transaction.uuid) for transaction in others]}
        else:
            assert record not in duplicates

    def test_duplicate_check_uses_two_queries_however_many_splits(self):
        for lms_user_id in range(5):
            self.make_record(state=SplitEnrollmentStates.COMMITTED, lms_user_id=lms_user_id)
            self.make_single_redemption(lms_user_id=lms_user_id)

        with self.assertNumQueries(2):
            duplicates = reconciliation.select_duplicate_live_redemptions(NOW)

        assert len(duplicates) == 5


@ddt.ddt
class LegsAndActionTests(ReconciliationTestMixin, TestCase):
    """
    Tests for finding legs and deciding the action.
    """
    def test_find_legs(self):
        record = self.make_record()
        leg_1 = self.make_leg_transaction(record, 1, state=REVERSED)
        leg_2 = self.make_leg_transaction(record, 2, with_leg_row=False)

        legs = reconciliation.find_legs(record)

        assert [(leg.leg_order, leg.source, leg.transaction, leg.is_reversed) for leg in legs] == [
            (1, reconciliation.LEG_SOURCE_ROW, leg_1, True),
            (2, reconciliation.LEG_SOURCE_LEDGER_KEY, leg_2, False),
        ]
        leg_2.delete()
        assert reconciliation.find_legs(record)[1].source == reconciliation.LEG_SOURCE_MISSING
        # Planned legs on subsidies that don't exist are reported missing.
        assert [leg.source for leg in reconciliation.find_legs(SplitEnrollmentRecordFactory())] == [
            reconciliation.LEG_SOURCE_MISSING, reconciliation.LEG_SOURCE_MISSING,
        ]

    @ddt.data(
        # (record state, leg 1 state, leg 2 state, fulfillment_identifier, action); None means the leg is missing.
        ('pending', None, None, None, reconciliation.ACTION_FAIL_ALL_LEGS),
        ('pending', 'created', 'created', None, reconciliation.ACTION_FAIL_ALL_LEGS),
        ('pending', 'created', None, None, reconciliation.ACTION_FAIL_ALL_LEGS),
        ('pending', 'pending', None, None, reconciliation.ACTION_MANUAL_REVIEW),
        ('pending', 'committed', None, 'fulfillment-1', reconciliation.ACTION_MANUAL_REVIEW),
        ('pending', 'committed', 'created', 'fulfillment-1', reconciliation.ACTION_MANUAL_REVIEW),
        ('pending', REVERSED, 'pending', 'fulfillment-1', reconciliation.ACTION_MANUAL_REVIEW),
        ('pending', 'pending', 'pending', None, reconciliation.ACTION_RESEND_ENROLL),
        ('pending', 'pending', 'pending', 'fulfillment-1', reconciliation.ACTION_VERIFY_FULFILLMENT),
        ('pending', 'committed', 'pending', 'fulfillment-1', reconciliation.ACTION_COMMIT_REMAINING_LEGS),
        ('pending', 'committed', 'committed', 'fulfillment-1', reconciliation.ACTION_COMMIT_REMAINING_LEGS),
        ('pending', 'committed', 'failed', 'fulfillment-1', reconciliation.ACTION_MANUAL_REVIEW),
        ('pending', 'failed', 'failed', None, reconciliation.ACTION_MANUAL_REVIEW),
        ('reversing', 'committed', 'committed', 'fulfillment-1', reconciliation.ACTION_REVERSE_REMAINING_LEGS),
        ('committed', 'committed', 'committed', 'fulfillment-1', reconciliation.ACTION_MANUAL_REVIEW),
    )
    @ddt.unpack
    def test_describe_action(self, record_state, leg_1_state, leg_2_state, fulfillment_identifier, expected):
        record = self.make_record(state=record_state, fulfillment_identifier=fulfillment_identifier)
        for leg_order, leg_state in ((1, leg_1_state), (2, leg_2_state)):
            if leg_state:
                self.make_leg_transaction(record, leg_order, state=leg_state)

        assert reconciliation.describe_action(record, reconciliation.find_legs(record)) == expected


@mock.patch(f'{COMMAND_MODULE}.localized_utcnow', return_value=NOW)
class ReconcileSplitEnrollmentsCommandTests(ReconciliationTestMixin, TestCase):
    """
    Tests for the reconcile_split_enrollments management command.
    """
    def run_command(self, *args):
        with self.assertLogs(COMMAND_MODULE, level='INFO') as logs:
            call_command('reconcile_split_enrollments', *args)
        return '\n'.join(logs.output)

    def test_reports_each_selected_record(self, _):
        pending = self.make_record(created_ago=timedelta(minutes=30))
        self.make_leg_transaction(pending, 1)
        self.make_leg_transaction(pending, 2, with_leg_row=False)
        self.make_record(state=SplitEnrollmentStates.FAILED)
        committed_duplicate = self.make_record(state=SplitEnrollmentStates.COMMITTED, lms_user_id=7)
        self.make_single_redemption(lms_user_id=7)

        output = self.run_command()

        assert f'record={pending.uuid} state=pending age=0:30:00 in_state=0:30:00' in output
        assert '(via leg_row)' in output and '(via ledger_key)' in output
        assert f"actions=['{reconciliation.ACTION_RESEND_ENROLL}']" in output
        assert f'record={committed_duplicate.uuid} state=committed' in output
        assert f"actions=['{reconciliation.ACTION_ALERT_DUPLICATE}'] evidence" in output
        assert "Selected per rule: {'duplicate_live_redemption': 1, 'stale_pending': 1}" in output
        assert '2 record(s) selected' in output and 'errors=0' in output

    def test_duplicate_logs_p1_and_a_record_matching_two_rules_is_reported_once(self, _):
        record = self.make_record(created_ago=timedelta(minutes=30))
        duplicate = self.make_single_redemption()

        output = self.run_command()

        assert (
            f'[P1] SPLIT_RECONCILIATION_DUPLICATE_LIVE_REDEMPTION record={record.uuid} customer={self.customer_uuid} '
            f'lms_user_id={self.lms_user_id} content_key={self.content_key} '
            f"other_live_transactions=['{duplicate.uuid}']"
        ) in output
        assert output.count(f'[DRY RUN] record={record.uuid}') == 1
        rules = [reconciliation.RULE_DUPLICATE_LIVE_REDEMPTION, reconciliation.RULE_STALE_PENDING]
        assert f'rules={rules}' in output
        assert f"actions=['{reconciliation.ACTION_ALERT_DUPLICATE}', '{reconciliation.ACTION_FAIL_ALL_LEGS}']" in output
        assert '1 record(s) selected' in output

    def test_dry_run_makes_no_writes_and_never_creates_transactions(self, _):
        record = self.make_record(created_ago=timedelta(minutes=30))
        self.make_leg_transaction(record, 1, state=TransactionStateChoices.CREATED)
        self.make_record(state=SplitEnrollmentStates.REVERSING, created_ago=timedelta(days=1),
                         lms_user_id=7, content_key='course-v1:edX+OtherX+2026')

        with mock.patch('openedx_ledger.api.create_transaction') as ledger_create, \
                mock.patch('enterprise_subsidy.apps.subsidy.models.Subsidy.create_transaction') as subsidy_create, \
                CaptureQueriesContext(connection) as queries:
            self.run_command()

        writes = [q['sql'] for q in queries.captured_queries if q['sql'].split()[0].upper() in (
            'INSERT', 'UPDATE', 'DELETE', 'REPLACE',
        )]
        assert not writes
        ledger_create.assert_not_called()
        subsidy_create.assert_not_called()

    def test_record_option(self, _):
        record = self.make_record(created_ago=timedelta(minutes=30))
        other = self.make_record(created_ago=timedelta(minutes=30), lms_user_id=7)
        too_new = self.make_record(created_ago=timedelta(minutes=5), lms_user_id=8)

        output = self.run_command('--record', str(record.uuid))
        assert f'record={record.uuid}' in output and f'record={other.uuid}' not in output

        output = self.run_command('--record', str(too_new.uuid))
        assert f'record={too_new.uuid} matched no rule: state=pending age=0:05:00 in_state=0:05:00' in output
        assert '0 record(s) selected' in output

    def test_max_records_runs_the_duplicate_rule_first_and_keeps_every_rule_of_selected_records(self, _):
        stale_duplicate = self.make_record(created_ago=timedelta(minutes=30))
        self.make_single_redemption()
        others = [self.make_record(created_ago=timedelta(minutes=60), lms_user_id=user) for user in (7, 8)]

        output = self.run_command('--max-records', '1')

        rules = [reconciliation.RULE_DUPLICATE_LIVE_REDEMPTION, reconciliation.RULE_STALE_PENDING]
        assert f'record={stale_duplicate.uuid}' in output and f'rules={rules}' in output
        assert all(f'record={other.uuid}' not in output for other in others)
        assert output.count('Hit --max-records=1; the remaining records were not reported.') == 1
        assert '1 record(s) selected' in output

    def test_invalid_options_are_command_errors(self, _):
        cases = [
            (['--record', str(uuid4())], False, 'No split enrollment record'),
            (['--record', 'not-a-uuid'], False, '--record'),
            (['--max-records', '0'], False, 'must be at least 1'),
            (['--max-records', '-1'], False, 'must be at least 1'),
            (['--fix'], False, '--fix is disabled'),
            (['--fix'], True, 'not implemented'),
        ]
        for args, fix_enabled, message in cases:
            with override_settings(ENABLE_SPLIT_RECONCILIATION_FIX=fix_enabled), \
                    pytest.raises(CommandError, match=message):
                call_command('reconcile_split_enrollments', *args)

    def test_errors_are_counted_exit_non_zero_and_keep_the_p1(self, _):
        failing = self.make_record(state=SplitEnrollmentStates.COMMITTED)
        self.make_single_redemption()
        self.make_record(created_ago=timedelta(minutes=30), lms_user_id=7)
        real_find_legs = reconciliation.find_legs

        def find_legs(record):
            if record.uuid == failing.uuid:
                raise ValueError('boom')
            return real_find_legs(record)

        with mock.patch.object(reconciliation, 'find_legs', side_effect=find_legs), \
                self.assertLogs(COMMAND_MODULE, level='INFO') as logs, \
                pytest.raises(CommandError, match='1 split enrollment record'):
            call_command('reconcile_split_enrollments')

        output = '\n'.join(logs.output)
        assert f'[P1] SPLIT_RECONCILIATION_DUPLICATE_LIVE_REDEMPTION record={failing.uuid}' in output
        assert f'Failed to reconcile split enrollment record {failing.uuid}' in output
        assert '2 record(s) selected' in output and 'errors=1' in output
