"""
Management command to find split enrollments that need attention and report what reconciliation would do with them.
"""
import argparse
import logging
from collections import Counter
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from enterprise_subsidy.apps.core.utils import localized_utcnow
from enterprise_subsidy.apps.transaction import reconciliation
from enterprise_subsidy.apps.transaction.models import SplitEnrollmentRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_RECORDS = 200


def positive_int(value):
    """
    argparse type for an integer of at least 1.
    """
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, got {number}')
    return number


class Command(BaseCommand):
    """
    Reports split enrollments that are stuck pending, stuck reversing, or duplicated by another live redemption, and
    the action reconciliation would take for each. It never writes: --fix is disabled by
    ENABLE_SPLIT_RECONCILIATION_FIX, and its rules aren't implemented yet.

    ./manage.py reconcile_split_enrollments [--record <uuid>] [--max-records 200]
    """
    help = 'Report split enrollments that need reconciliation, and what would be done with them.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fix',
            action='store_true',
            help='Apply the reported actions. Disabled unless ENABLE_SPLIT_RECONCILIATION_FIX is set.',
        )
        parser.add_argument(
            '--record',
            type=UUID,
            help='Only consider the split enrollment record with this uuid.',
        )
        parser.add_argument(
            '--max-records',
            type=positive_int,
            default=DEFAULT_MAX_RECORDS,
            help=f'Stop after reporting this many records (default {DEFAULT_MAX_RECORDS}).',
        )

    def handle(self, *args, **options):
        if options['fix']:
            if not settings.ENABLE_SPLIT_RECONCILIATION_FIX:
                raise CommandError('--fix is disabled. Set ENABLE_SPLIT_RECONCILIATION_FIX to enable it.')
            raise CommandError('--fix rules are not implemented yet; run without --fix for a report.')

        records = SplitEnrollmentRecord.objects.all()
        if options['record']:
            records = records.filter(uuid=options['record'])
            if not records.exists():
                raise CommandError(f'No split enrollment record with uuid {options["record"]}.')

        now = localized_utcnow()
        selected, duplicates = self._select(now, records, options['max_records'])
        if options['record'] and not selected:
            record = records.get()
            logger.info(
                f'[DRY RUN] record={record.uuid} matched no rule: state={record.state} '
                f'age={now - record.created} in_state={now - record.modified}.'
            )

        action_counts = Counter()
        errors = 0
        for record, rules in selected.values():
            try:
                action_counts.update(self._report(now, record, rules, duplicates.get(record, [])))
            except Exception:  # pylint: disable=broad-exception-caught
                errors += 1
                logger.exception(f'[DRY RUN] Failed to reconcile split enrollment record {record.uuid}.')

        logger.info(
            f'[DRY RUN] Reconciliation run complete: {len(selected)} record(s) selected, '
            f'actions={dict(sorted(action_counts.items()))}, errors={errors}.'
        )
        if errors:
            raise CommandError(f'{errors} split enrollment record(s) could not be reconciled.')

    def _select(self, now, records, max_records):
        """
        Returns ({record uuid: (record, [rules])}, {record: [other live transaction uuids]}) for every selection rule,
        up to max_records records. The duplicate rule runs first, so a backlog of stale records can't crowd out its
        P1 alert.
        """
        duplicates = reconciliation.select_duplicate_live_redemptions(now, records)
        rule_selections = [
            (reconciliation.RULE_DUPLICATE_LIVE_REDEMPTION, lambda: duplicates),
            (reconciliation.RULE_STALE_PENDING, lambda: reconciliation.select_stale_pending(now, records)),
            (reconciliation.RULE_STALE_REVERSING, lambda: reconciliation.select_stale_reversing(now, records)),
        ]
        selected = {}
        rule_counts = Counter()
        capped = False
        for rule, select in rule_selections:
            for record in select():
                if record.uuid not in selected:
                    if len(selected) >= max_records:
                        # Admit no new records, but keep collecting rules for the ones already selected.
                        capped = True
                        continue
                    selected[record.uuid] = (record, [])
                selected[record.uuid][1].append(rule)
                rule_counts[rule] += 1
        if capped:
            logger.warning(
                f'[DRY RUN] Hit --max-records={max_records}; the remaining records were not reported. '
                'A dry run changes nothing, so the same records come first next time: raise '
                '--max-records or use --record.'
            )
        logger.info(f'[DRY RUN] Selected per rule: {dict(rule_counts)}.')
        return selected, duplicates

    def _report(self, now, record, rules, duplicates):
        """
        Logs one record's state, legs, actions and evidence. Returns the actions it would take.
        """
        actions = []
        evidence = f'fulfillment_identifier={record.fulfillment_identifier}'
        # The P1 doesn't need the legs, so log it first: an error while finding legs mustn't lose the alert.
        if reconciliation.RULE_DUPLICATE_LIVE_REDEMPTION in rules:
            actions.append(reconciliation.ACTION_ALERT_DUPLICATE)
            evidence += f', other_live_transactions={duplicates}'
            logger.error(
                f'[P1] SPLIT_RECONCILIATION_DUPLICATE_LIVE_REDEMPTION record={record.uuid} '
                f'customer={record.enterprise_customer_uuid} lms_user_id={record.lms_user_id} '
                f'content_key={record.content_key} other_live_transactions={duplicates}'
            )
        legs = reconciliation.find_legs(record)
        if reconciliation.RULE_STALE_PENDING in rules or reconciliation.RULE_STALE_REVERSING in rules:
            actions.append(reconciliation.describe_action(record, legs))
        logger.info(
            f'[DRY RUN] record={record.uuid} state={record.state} age={now - record.created} '
            f'in_state={now - record.modified} rules={rules} '
            f'legs=[{"; ".join(str(leg) for leg in legs)}] actions={actions} evidence: {evidence}'
        )
        return actions
