"""
Tests for the split enrollment Django admin.
"""
from django.contrib.admin.sites import site
from django.test import RequestFactory, TestCase
from django.urls import reverse

from enterprise_subsidy.apps.subsidy.tests.factories import USER_PASSWORD, UserFactory
from enterprise_subsidy.apps.transaction.models import SplitEnrollmentLeg, SplitEnrollmentRecord
from enterprise_subsidy.apps.transaction.tests.factories import SplitEnrollmentLegFactory, SplitEnrollmentRecordFactory


class SplitEnrollmentAdminTestCase(TestCase):
    """
    The split enrollment admins are registered and read-only.
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory(is_staff=True, is_superuser=True)
        self.client.login(username=self.user.username, password=USER_PASSWORD)
        self.record = SplitEnrollmentRecordFactory()
        self.leg = SplitEnrollmentLegFactory(split_record=self.record)

    def test_admins_are_read_only(self):
        request = RequestFactory().get('/')
        request.user = self.user
        for model, obj in ((SplitEnrollmentRecord, self.record), (SplitEnrollmentLeg, self.leg)):
            model_admin = site._registry[model]  # pylint: disable=protected-access
            assert not model_admin.has_add_permission(request)
            assert not model_admin.has_change_permission(request, obj)
            assert not model_admin.has_delete_permission(request, obj)

    def test_pages_render(self):
        urls = [
            reverse('admin:transaction_splitenrollmentrecord_changelist'),
            reverse('admin:transaction_splitenrollmentrecord_change', args=(self.record.uuid,)),
            reverse('admin:transaction_splitenrollmentleg_changelist'),
            reverse('admin:transaction_splitenrollmentleg_change', args=(self.leg.uuid,)),
        ]
        for url in urls:
            response = self.client.get(url)
            assert response.status_code == 200, url

    def test_record_page_is_view_only_with_legs_inline(self):
        response = self.client.get(reverse('admin:transaction_splitenrollmentrecord_change', args=(self.record.uuid,)))

        content = response.content.decode()
        assert self.record.content_key in content
        assert str(self.leg.transaction.uuid) in content
        assert 'name="content_key"' not in content
        assert 'name="_save"' not in content
