import os
import unittest
from unittest.mock import patch

import outbound_url_policy as policy


class _Response:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._body = body
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=65536):
        yield self._body


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class OutboundUrlPolicyTests(unittest.TestCase):
    def tearDown(self):
        policy.clear_dns_policy_cache()

    def test_rejects_local_and_private_destinations(self):
        self.assertFalse(policy.validate_outbound_url("http://localhost/a").allowed)
        self.assertFalse(policy.validate_outbound_url("http://127.0.0.1/a").allowed)
        self.assertFalse(policy.validate_outbound_url("http://169.254.169.254/latest").allowed)

    def test_rejects_userinfo_and_non_http_schemes(self):
        self.assertFalse(policy.validate_outbound_url("http://user:pass@example.com/").allowed)
        self.assertFalse(policy.validate_outbound_url("file:///etc/passwd").allowed)

    @patch("outbound_url_policy._resolve_host", return_value=("93.184.216.34",))
    def test_accepts_public_destination(self, _resolver):
        result = policy.validate_outbound_url("HTTPS://Example.COM/page#fragment")
        self.assertTrue(result.allowed)
        self.assertEqual(result.normalized_url, "https://example.com/page")

    @patch("outbound_url_policy._resolve_host")
    def test_revalidates_redirect_and_blocks_private_target(self, resolver):
        resolver.side_effect = lambda host: {
            "example.com": ("93.184.216.34",),
            "internal.example": ("10.0.0.8",),
        }[host]
        response = _Response(302, {"Location": "http://internal.example/secret"})
        client = _Client([response])
        with self.assertRaisesRegex(ValueError, "outbound_redirect_blocked"):
            policy.safe_request_get("https://example.com/start", session=client)
        self.assertTrue(response.closed)

    @patch("outbound_url_policy._resolve_host", return_value=("93.184.216.34",))
    def test_tls_verification_cannot_be_disabled_per_call(self, _resolver):
        client = _Client([_Response(200)])
        with patch.dict(os.environ, {"CRAWL_TLS_VERIFY": "true"}):
            policy.safe_request_get("https://example.com/", session=client, verify=False)
        self.assertIs(client.calls[0][1]["verify"], True)

    def test_response_size_limit_checks_streamed_body(self):
        with self.assertRaisesRegex(ValueError, "response_too_large"):
            policy.read_response_bytes_limited(_Response(body=b"12345"), limit=4)


if __name__ == "__main__":
    unittest.main()
