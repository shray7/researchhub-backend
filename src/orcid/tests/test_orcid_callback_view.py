from unittest.mock import patch

from rest_framework import status
from rest_framework.test import APITestCase

from orcid.tests.helpers import create_orcid_app
from user.tests.helpers import create_random_authenticated_user


@patch("orcid.views.orcid_callback_view.OrcidService")
class OrcidCallbackViewTests(APITestCase):
    def setUp(self):
        self.user = create_random_authenticated_user("callback_user")
        self.app = create_orcid_app()

    def _get(self, query):
        return self.client.get(f"/api/orcid/callback/?{query}")

    def _assert_redirect(self, response, expected_url):
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, expected_url)

    def test_error_param_redirects_cancelled(self, mock_service):
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=cancelled"

        response = self._get("error=access_denied")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=cancelled")
        mock_service.return_value.get_redirect_url.assert_called_with(error="cancelled")

    def test_missing_code_redirects_cancelled(self, mock_service):
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=cancelled"

        response = self._get("state=abc")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=cancelled")

    def test_invalid_state_redirects(self, mock_service):
        mock_service.return_value.decode_state.return_value = None
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=invalid_state"

        response = self._get("code=abc&state=invalid")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=invalid_state")
        mock_service.return_value.get_redirect_url.assert_called_with(error="invalid_state")

    def test_user_not_found_redirects(self, mock_service):
        mock_service.return_value.decode_state.return_value = {"user_id": 99999}
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=invalid_state"

        response = self._get("code=abc&state=valid")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=invalid_state")
        mock_service.return_value.get_redirect_url.assert_called_with(error="invalid_state", return_url=None)

    def test_success_redirects(self, mock_service):
        mock_service.return_value.decode_state.return_value = {"user_id": self.user.id}
        mock_service.return_value.exchange_code_for_token.return_value = {"orcid": "0000-0001-2345-6789"}
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_connected=true"

        response = self._get("code=abc&state=valid")

        self._assert_redirect(response, "https://researchhub.com?orcid_connected=true")
        mock_service.return_value.connect_orcid_account.assert_called_once()
        mock_service.return_value.get_redirect_url.assert_called_with(return_url=None)

    def test_success_with_return_url(self, mock_service):
        return_url = "https://researchhub.com/funds"
        mock_service.return_value.decode_state.return_value = {"user_id": self.user.id, "return_url": return_url}
        mock_service.return_value.exchange_code_for_token.return_value = {"orcid": "0000-0001-2345-6789"}
        mock_service.return_value.get_redirect_url.return_value = f"{return_url}?orcid_connected=true"

        response = self._get("code=abc&state=valid")

        self._assert_redirect(response, f"{return_url}?orcid_connected=true")
        mock_service.return_value.get_redirect_url.assert_called_with(return_url=return_url)

    def test_already_linked_redirects(self, mock_service):
        mock_service.return_value.decode_state.return_value = {"user_id": self.user.id}
        mock_service.return_value.exchange_code_for_token.return_value = {"orcid": "0000-0001-2345-6789"}
        mock_service.return_value.connect_orcid_account.side_effect = ValueError("already linked")
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=already_linked"

        response = self._get("code=abc&state=valid")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=already_linked")

    def test_service_error_redirects(self, mock_service):
        mock_service.return_value.decode_state.return_value = {"user_id": self.user.id}
        mock_service.return_value.exchange_code_for_token.side_effect = Exception("service error")
        mock_service.return_value.get_redirect_url.return_value = "https://researchhub.com?orcid_error=service_error"

        response = self._get("code=abc&state=valid")

        self._assert_redirect(response, "https://researchhub.com?orcid_error=service_error")

