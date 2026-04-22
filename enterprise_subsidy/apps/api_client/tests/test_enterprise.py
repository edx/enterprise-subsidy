import os
from unittest import mock
from uuid import uuid4

import ddt
from django.conf import settings
from django.test import TestCase, override_settings
from openedx_ledger.models import TransactionStateChoices
from openedx_ledger.test_utils.factories import TransactionFactory
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, ReadTimeout

from enterprise_subsidy.apps.api_client.enterprise import (
    ENROLLMENT_REF_ID_FIELD_NAME,
    EnrollmentException,
    EnterpriseApiClient
)
from enterprise_subsidy.apps.subsidy.tests.factories import SubsidyFactory
from test_utils.utils import MockResponse


@ddt.ddt
class EnterpriseApiClientTests(TestCase):
    """
    Tests for the enterprise api client.
    """
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.enterprise_customer_uuid = uuid4()
        cls.user_id = 3
        cls.user_email = 'ayy@lmao.com'
        cls.courserun_key = 'course-v1:edX+DemoX+Demo_Course'

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_successful_create_enterprise_enrollment(self, mock_oauth_client):
        """
        Test the enterprise client's ability to handle successful api requests to create course enrollments
        """
        mock_oauth_client.return_value.post.return_value = MockResponse(
            {
                'successes': [{'email': self.user_email, 'course_run_key': self.courserun_key}],
                'pending': [],
                'failures': []
            },
            201,
        )
        options = [{
            'email': self.user_email,
            'course_run_key': self.courserun_key,
            'transaction_id': 'some-transaction-id',
        }]
        enterprise_client = EnterpriseApiClient()
        response = enterprise_client.bulk_enroll_enterprise_learners(self.enterprise_customer_uuid, options)
        assert response.get('successes') == [{'email': self.user_email, 'course_run_key': self.courserun_key}]
        mock_oauth_client().post.assert_called_with(
            os.path.join(
                EnterpriseApiClient.enterprise_customer_endpoint,
                str(self.enterprise_customer_uuid),
                'enroll_learners_in_courses/',
            ),
            json={'enrollments_info': [{
                'email': self.user_email,
                'course_run_key': self.courserun_key,
                'transaction_id': 'some-transaction-id',
            }]},
            timeout=(
                settings.BULK_ENROLL_CONNECT_TIMEOUT_SECONDS,
                settings.BULK_ENROLL_READ_TIMEOUT_SECONDS,
            ),
        )

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_successful_create_single_learner_enrollment(self, mock_oauth_client):
        """
        Test the enterprise client's ability to handle successful api requests to create a course enrollment
        for a single learner using client.enroll().
        """
        expected_reference_id = 'test-reference-id'
        mock_oauth_client.return_value.post.return_value = MockResponse(
            {
                'successes': [{
                    'user_id': self.user_id,
                    'email': self.user_email,
                    'course_run_key': self.courserun_key,
                    ENROLLMENT_REF_ID_FIELD_NAME: expected_reference_id,
                }],
                'pending': [],
                'failures': []
            },
            201,
        )
        subsidy = SubsidyFactory(enterprise_customer_uuid=self.enterprise_customer_uuid, starting_balance=10000)
        transaction = TransactionFactory(
            state=TransactionStateChoices.PENDING,
            quantity=-1000,
            ledger=subsidy.ledger,
            idempotency_key=f"{subsidy.ledger.idempotency_key}--1000-abcd"
        )

        enterprise_client = EnterpriseApiClient()
        actual_reference_id = enterprise_client.enroll(self.user_id, self.courserun_key, transaction)

        assert actual_reference_id == expected_reference_id
        mock_oauth_client().post.assert_called_with(
            os.path.join(
                EnterpriseApiClient.enterprise_customer_endpoint,
                str(self.enterprise_customer_uuid),
                'enroll_learners_in_courses/',
            ),
            json={'enrollments_info': [{
                'user_id': self.user_id,
                'course_run_key': self.courserun_key,
                'transaction_id': str(transaction.uuid),
            }]},
            timeout=(
                settings.BULK_ENROLL_CONNECT_TIMEOUT_SECONDS,
                settings.BULK_ENROLL_READ_TIMEOUT_SECONDS,
            ),
        )

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_successful_create_single_learner_enrollment_forced(self, mock_oauth_client):
        """
        Test the enterprise client's ability to force a late enrollment when the enterprise-access service desires it.
        """
        expected_reference_id = 'test-reference-id'
        mock_oauth_client.return_value.post.return_value = MockResponse(
            {
                'successes': [{
                    'user_id': self.user_id,
                    'email': self.user_email,
                    'course_run_key': self.courserun_key,
                    ENROLLMENT_REF_ID_FIELD_NAME: expected_reference_id,
                }],
                'pending': [],
                'failures': []
            },
            201,
        )
        subsidy = SubsidyFactory(enterprise_customer_uuid=self.enterprise_customer_uuid, starting_balance=10000)
        transaction = TransactionFactory(
            state=TransactionStateChoices.PENDING,
            quantity=-1000,
            ledger=subsidy.ledger,
            idempotency_key=f"{subsidy.ledger.idempotency_key}--1000-abcd",
            metadata={'allow_late_enrollment': True},  # The actual unique thing we're testing in this test.
        )

        enterprise_client = EnterpriseApiClient()
        actual_reference_id = enterprise_client.enroll(self.user_id, self.courserun_key, transaction)

        assert actual_reference_id == expected_reference_id
        mock_oauth_client().post.assert_called_with(
            os.path.join(
                EnterpriseApiClient.enterprise_customer_endpoint,
                str(self.enterprise_customer_uuid),
                'enroll_learners_in_courses/',
            ),
            json={'enrollments_info': [{
                'user_id': self.user_id,
                'course_run_key': self.courserun_key,
                'transaction_id': str(transaction.uuid),
                'force_enrollment': True,  # The actual unique thing we're testing in this test.
            }]},
            timeout=(
                settings.BULK_ENROLL_CONNECT_TIMEOUT_SECONDS,
                settings.BULK_ENROLL_READ_TIMEOUT_SECONDS,
            ),
        )

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_failed_create_single_learner_enrollment_2xx(self, mock_oauth_client):
        """
        Something bad happened on the enrollment API side which caused a response without any successful enrollments.

        Special case where the status code was still 2xx.
        """
        mock_oauth_client.return_value.post.return_value = MockResponse(
            {
                'successes': [
                    # something weird happened that caused no successful enrollments (despite 201 status I guess...)
                ],
                'pending': [],
                'failures': []
            },
            201,
        )
        subsidy = SubsidyFactory(enterprise_customer_uuid=self.enterprise_customer_uuid, starting_balance=10000)
        transaction = TransactionFactory(
            state=TransactionStateChoices.PENDING,
            quantity=-1000,
            ledger=subsidy.ledger,
            idempotency_key=f"{subsidy.ledger.idempotency_key}--1000-abcd"
        )

        enterprise_client = EnterpriseApiClient()
        with self.assertRaises(EnrollmentException):
            enterprise_client.enroll(self.user_id, self.courserun_key, transaction)

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_failed_create_single_learner_enrollment_4xx(self, mock_oauth_client):
        """
        Something bad happened on the enrollment API side which caused a response without any successful enrollments.

        Special case where the status code was 4xx.
        """
        mock_oauth_client.return_value.post.return_value = MockResponse(None, 403)
        subsidy = SubsidyFactory(enterprise_customer_uuid=self.enterprise_customer_uuid, starting_balance=10000)
        transaction = TransactionFactory(
            state=TransactionStateChoices.PENDING,
            quantity=-1000,
            ledger=subsidy.ledger,
            idempotency_key=f"{subsidy.ledger.idempotency_key}--1000-abcd"
        )

        enterprise_client = EnterpriseApiClient()
        with self.assertRaises(HTTPError):
            enterprise_client.enroll(self.user_id, self.courserun_key, transaction)

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_successful_fetching_of_recent_unenrollments(self, mock_oauth_client):
        """
        Test the enterprise client's expected successful behavior when fetching recent unenrollments
        """
        transaction_id = str(uuid4())
        fulfillment_uuid = str(uuid4())
        mock_oauth_client.return_value.get.return_value = MockResponse(
            [{
                'enterprise_course_enrollment': {
                    'enterprise_customer_user': 10,
                    'course_id': self.courserun_key,
                    'created': '2023-05-25T19:27:29Z',
                    'unenrolled_at': '2023-06-01T19:27:29Z',
                },
                'transaction_id': transaction_id,
                'uuid': fulfillment_uuid,
            }],
            200
        )
        enterprise_client = EnterpriseApiClient()
        response = enterprise_client.fetch_recent_unenrollments()
        assert response == [{
            'enterprise_course_enrollment': {
                'enterprise_customer_user': 10,
                'course_id': self.courserun_key,
                'created': '2023-05-25T19:27:29Z',
                'unenrolled_at': '2023-06-01T19:27:29Z',
            },
            'transaction_id': transaction_id,
            'uuid': fulfillment_uuid,
        }]

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_failed_fetching_of_recent_unenrollments(self, mock_oauth_client):
        """
        Test the enterprise client's expected behavior when fetching recent unenrollments fails
        """
        mock_oauth_client.return_value.get.return_value = MockResponse(None, 400)
        enterprise_client = EnterpriseApiClient()
        with self.assertRaises(HTTPError):
            enterprise_client.fetch_recent_unenrollments()

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient', return_value=mock.MagicMock())
    def test_successful_fetch_enterprise_data(self, mock_oauth_client):
        """
        Test the enterprise client's ability to handle successful api requests to fetch information on an enterprise
        customer
        """
        mock_oauth_client.return_value.get.return_value = MockResponse(
            {
                "uuid": str(self.enterprise_customer_uuid),
                "name": "The Whinery Spirits Company",
                "slug": "the-whinery-spirits-company",
                "active": True,
                "enterprise_customer_catalogs": [
                    "af67a92c-acbe-400a-93af-42074abc70b0"
                ],
                "modified": "2023-02-08T15:40:29.092448Z",
                "admin_users": [
                    {
                        "email": "enterprise_admin_the-whinery-spirits-company@example.com",
                        "lms_user_id": 14
                    },
                    {
                        "email": "aballplayer@gmail.com",
                        "lms_user_id": 33
                    }
                ]
            },
            201,
        )

        enterprise_client = EnterpriseApiClient()
        response = enterprise_client.get_enterprise_customer_data(self.enterprise_customer_uuid)
        assert response.get('uuid') == str(self.enterprise_customer_uuid)


@ddt.ddt
class BulkEnrollRetryTests(TestCase):
    """
    Retry behavior for ``EnterpriseApiClient.bulk_enroll_enterprise_learners``.

    ``time.sleep`` is patched to a no-op so exponential backoff delays
    don't slow the suite.
    """

    def setUp(self):
        super().setUp()
        self.enterprise_customer_uuid = uuid4()
        self.user_id = 3
        self.courserun_key = 'course-v1:edX+DemoX+Demo_Course'
        self.enrollments_info = [{
            'user_id': self.user_id,
            'course_run_key': self.courserun_key,
            'transaction_id': 'some-transaction-id',
        }]

    def _success_response(self):
        return MockResponse(
            {
                'successes': [{
                    'user_id': self.user_id,
                    'course_run_key': self.courserun_key,
                }],
                'pending': [],
                'failures': [],
            },
            201,
        )

    @mock.patch('time.sleep', return_value=None)
    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_retries_on_5xx_then_succeeds(self, mock_oauth_client, _mock_sleep):
        """5xx responses should be retried until success."""
        mock_oauth_client.return_value.post.side_effect = [
            MockResponse({'error': 'service unavailable'}, 503),
            MockResponse({'error': 'gateway'}, 502),
            self._success_response(),
        ]
        client = EnterpriseApiClient()
        response = client.bulk_enroll_enterprise_learners(
            self.enterprise_customer_uuid, self.enrollments_info,
        )
        self.assertEqual(mock_oauth_client.return_value.post.call_count, 3)
        self.assertIn('successes', response)

    @mock.patch('time.sleep', return_value=None)
    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_retries_on_read_timeout_then_succeeds(self, mock_oauth_client, _mock_sleep):
        """A ReadTimeout (the motivating ticket scenario) should be retried."""
        mock_oauth_client.return_value.post.side_effect = [
            ReadTimeout('read timed out'),
            self._success_response(),
        ]
        client = EnterpriseApiClient()
        response = client.bulk_enroll_enterprise_learners(
            self.enterprise_customer_uuid, self.enrollments_info,
        )
        self.assertEqual(mock_oauth_client.return_value.post.call_count, 2)
        self.assertIn('successes', response)

    @mock.patch('time.sleep', return_value=None)
    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_retries_on_connection_error_then_succeeds(self, mock_oauth_client, _mock_sleep):
        """Low-level connection resets should be retried."""
        mock_oauth_client.return_value.post.side_effect = [
            RequestsConnectionError('connection reset'),
            self._success_response(),
        ]
        client = EnterpriseApiClient()
        client.bulk_enroll_enterprise_learners(
            self.enterprise_customer_uuid, self.enrollments_info,
        )
        self.assertEqual(mock_oauth_client.return_value.post.call_count, 2)

    @ddt.data(400, 401, 403, 404, 422)
    @mock.patch('time.sleep', return_value=None)
    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_no_retry_on_4xx(self, status_code, mock_oauth_client, _mock_sleep):
        """Deterministic client errors should not be retried."""
        mock_oauth_client.return_value.post.return_value = MockResponse(
            {'error': 'client error'}, status_code,
        )
        client = EnterpriseApiClient()
        with self.assertRaises(HTTPError):
            client.bulk_enroll_enterprise_learners(
                self.enterprise_customer_uuid, self.enrollments_info,
            )
        self.assertEqual(mock_oauth_client.return_value.post.call_count, 1)

    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_gives_up_after_max_time(self, mock_oauth_client):
        """Persistent failures should surface the exception once max_time elapses."""
        mock_oauth_client.return_value.post.side_effect = ReadTimeout('persistent')
        client = EnterpriseApiClient()
        with self.assertRaises(ReadTimeout):
            client.bulk_enroll_enterprise_learners(
                self.enterprise_customer_uuid, self.enrollments_info,
            )

        # We should make at least one call.
        # The test settings define a very small BULK_ENROLL_RETRY_MAX_SECONDS value,
        # so we set some sufficiently high upper bound for completeness.
        # Local testing shows a max seconds value of 0.5 typically results in 3 calls,
        # so we can use 10 as a safe upper-bound that shouldn't result in this test being flaky.
        self.assertGreaterEqual(mock_oauth_client.return_value.post.call_count, 1)
        self.assertLessEqual(mock_oauth_client.return_value.post.call_count, 10)

    @mock.patch('time.sleep', return_value=None)
    @mock.patch('enterprise_subsidy.apps.api_client.base_oauth.OAuthAPIClient')
    def test_request_uses_split_connect_and_read_timeouts(self, mock_oauth_client, _mock_sleep):
        """Timeout should be passed to requests as a (connect, read) tuple."""
        mock_oauth_client.return_value.post.return_value = self._success_response()
        client = EnterpriseApiClient()
        client.bulk_enroll_enterprise_learners(
            self.enterprise_customer_uuid, self.enrollments_info,
        )
        _, kwargs = mock_oauth_client.return_value.post.call_args
        self.assertEqual(
            kwargs['timeout'],
            (
                settings.BULK_ENROLL_CONNECT_TIMEOUT_SECONDS,
                settings.BULK_ENROLL_READ_TIMEOUT_SECONDS,
            ),
        )
