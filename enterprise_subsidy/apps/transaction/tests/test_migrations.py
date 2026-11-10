"""
Test migrations.
"""
import pytest

SPLIT_MIGRATION = ("transaction", "0001_split_enrollment_models")


@pytest.mark.django_db
def test_migration_0001_split_enrollment_models_applies_and_reverses(migrator):
    """
    The split enrollment tables are created, and dropped again on reverse.
    """
    old_state = migrator.apply_initial_migration(("transaction", None))
    with pytest.raises(LookupError):
        old_state.apps.get_model("transaction", "SplitEnrollmentRecord")

    new_state = migrator.apply_tested_migration(SPLIT_MIGRATION)
    assert new_state.apps.get_model("transaction", "SplitEnrollmentRecord").objects.count() == 0
    assert new_state.apps.get_model("transaction", "SplitEnrollmentLeg").objects.count() == 0

    reversed_state = migrator.apply_tested_migration(("transaction", None))
    with pytest.raises(LookupError):
        reversed_state.apps.get_model("transaction", "SplitEnrollmentLeg")
