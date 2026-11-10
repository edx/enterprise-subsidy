"""
Test factories for transaction app models.
"""
from uuid import uuid4

import factory
from openedx_ledger.test_utils.factories import LedgerFactory, TransactionFactory

from enterprise_subsidy.apps.transaction.models import SplitEnrollmentLeg, SplitEnrollmentRecord


def _planned_legs(record):
    """
    Two planned legs whose quantities sum to the record's total_quantity.
    """
    leg_1_quantity = record.total_quantity // 2
    return [
        {
            'leg_order': leg_order,
            'subsidy_uuid': str(uuid4()),
            'subsidy_access_policy_uuid': str(uuid4()),
            'quantity': quantity,
        }
        for leg_order, quantity in ((1, leg_1_quantity), (2, record.total_quantity - leg_1_quantity))
    ]


class SplitEnrollmentRecordFactory(factory.django.DjangoModelFactory):
    """
    Test factory for the ``SplitEnrollmentRecord`` model.
    """
    class Meta:
        model = SplitEnrollmentRecord

    enterprise_customer_uuid = factory.LazyFunction(uuid4)
    lms_user_id = factory.Faker("random_int", min=1, max=1000)
    content_key = factory.Faker("lexify", text="course-v1:???+?????101+2026")
    total_quantity = factory.Faker("random_int", min=-100000, max=-200)
    planned_legs = factory.LazyAttribute(_planned_legs)


class SplitEnrollmentLegFactory(factory.django.DjangoModelFactory):
    """
    Test factory for the ``SplitEnrollmentLeg`` model.
    """
    class Meta:
        model = SplitEnrollmentLeg

    split_record = factory.SubFactory(SplitEnrollmentRecordFactory)
    leg_order = factory.Sequence(lambda n: n + 1)
    transaction = factory.SubFactory(TransactionFactory, ledger=factory.SubFactory(LedgerFactory))
