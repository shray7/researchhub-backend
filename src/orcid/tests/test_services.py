from unittest.mock import Mock, patch

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from allauth.socialaccount.providers.orcid.provider import OrcidProvider
from django.test import TestCase

from orcid.services.orcid_service import OrcidService
from orcid.tests.helpers import create_orcid_app
from user.tests.helpers import create_random_default_user


class OrcidServiceTests(TestCase):
    def setUp(self):
        self.service = OrcidService()

    def test_get_app(self):
        with self.assertRaises(SocialApp.DoesNotExist):
            self.service._get_orcid_app()
        app = create_orcid_app()
        self.assertEqual(self.service._get_orcid_app(), app)

    def test_state_encoding_and_decoding(self):
        data = {"user_id": 123, "return_url": "https://researchhub.com"}
        encoded = self.service._encode_signed_value(data)
        self.assertEqual(self.service.decode_state(encoded), data)

    def test_decode_state_invalid(self):
        self.assertIsNone(self.service.decode_state("invalid"))
        self.assertIsNone(self.service.decode_state(""))

    def test_decode_state_expired(self):
        service = OrcidService()
        service.STATE_MAX_AGE = 0
        encoded = service._encode_signed_value({"user_id": 123})
        self.assertIsNone(service.decode_state(encoded))

    def test_build_auth_url(self):
        create_orcid_app()
        url = self.service.build_auth_url(123, "https://researchhub.com/settings")
        self.assertIn("test-id", url)
        self.assertIn("state=", url)
        self.assertIn("oauth/authorize", url)

    def test_connect_creates_account_and_token(self):
        user = create_random_default_user("new")
        create_orcid_app()
        token_data = {"orcid": "0000-0001-2345-6789", "access_token": "a", "refresh_token": "r", "expires_in": 3600}
        self.service.connect_orcid_account(user, token_data)

        self.assertTrue(SocialAccount.objects.filter(user=user).exists())
        token = SocialToken.objects.get(account__user=user)
        self.assertEqual((token.token, token.token_secret), ("a", "r"))

    def test_connect_updates_author(self):
        user = create_random_default_user("author")
        create_orcid_app()
        self.service.connect_orcid_account(user, {"orcid": "0000-0001-2345-6789"})
        user.author_profile.refresh_from_db()
        self.assertEqual(user.author_profile.orcid_id, f"{OrcidService.ORCID_BASE_URL}/0000-0001-2345-6789")

    def test_connect_raises_on_missing_orcid(self):
        user = create_random_default_user("u1")
        create_orcid_app()
        with self.assertRaises(ValueError):
            self.service.connect_orcid_account(user, {})

    def test_connect_raises_on_already_linked(self):
        user1, user2 = create_random_default_user("u1"), create_random_default_user("u2")
        create_orcid_app()
        SocialAccount.objects.create(user=user1, provider=OrcidProvider.id, uid="0000-0001-2345-6789")
        with self.assertRaises(ValueError):
            self.service.connect_orcid_account(user2, {"orcid": "0000-0001-2345-6789"})

    @patch("orcid.services.orcid_service.requests.post")
    def test_exchange_code_for_token(self, mock_post):
        create_orcid_app()
        mock_post.return_value = Mock(json=lambda: {"orcid": "123"}, raise_for_status=Mock())
        result = self.service.exchange_code_for_token("code")
        self.assertEqual(result["orcid"], "123")
        mock_post.assert_called_once()

    def test_get_redirect_url_success(self):
        url = self.service.get_redirect_url(return_url="https://researchhub.com/funds")
        self.assertEqual(url, "https://researchhub.com/funds?orcid_connected=true")

    def test_get_redirect_url_with_existing_params(self):
        url = self.service.get_redirect_url(return_url="https://researchhub.com?tab=1")
        self.assertEqual(url, "https://researchhub.com?tab=1&orcid_connected=true")

    def test_get_redirect_url_error(self):
        url = self.service.get_redirect_url(error="already_linked", return_url="https://researchhub.com")
        self.assertEqual(url, "https://researchhub.com?orcid_error=already_linked")

    def test_get_redirect_url_rejects_invalid(self):
        url = self.service.get_redirect_url(return_url="https://evil.com")
        self.assertNotIn("evil.com", url)

    def test_is_valid_redirect_url(self):
        valid = ["https://researchhub.com/x", "https://www.researchhub.com/x", "http://localhost:3000/x"]
        invalid = ["https://evil.com", "javascript:alert(1)", None, ""]
        for url in valid:
            self.assertTrue(self.service._is_valid_redirect_url(url), f"{url} should be valid")
        for url in invalid:
            self.assertFalse(self.service._is_valid_redirect_url(url), f"{url} should be invalid")
