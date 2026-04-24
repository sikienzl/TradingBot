from unittest.mock import patch, MagicMock
import base64
import time

import pytest

from get_account_balance import (
    get_nonce,
    sign,
    get_signature,
    request,
)


class TestGetNonce:
    """Tests for get_nonce function."""

    def test_nonce_is_string(self):
        """Test that nonce is returned as string."""
        nonce = get_nonce()
        assert isinstance(nonce, str)

    def test_nonce_is_numeric_string(self):
        """Test that nonce is numeric (unix timestamp in milliseconds)."""
        nonce = get_nonce()
        assert nonce.isdigit()

    def test_nonce_increases(self):
        """Test that successive nonces are increasing."""
        nonce1 = get_nonce()
        time.sleep(0.01)  # Sleep 10ms to ensure different nonce
        nonce2 = get_nonce()
        assert int(nonce2) > int(nonce1)

    def test_nonce_length(self):
        """Test that nonce has reasonable length (13 digits for ms timestamp)."""
        nonce = get_nonce()
        assert 12 <= len(nonce) <= 14


class TestSign:
    """Tests for sign function."""

    def test_sign_returns_base64_string(self):
        """Test that sign returns base64 encoded string."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        message = b"test message"
        signature = sign(private_key, message)
        assert isinstance(signature, str)
        # Try to decode base64
        try:
            base64.b64decode(signature)
        except Exception:
            pytest.fail("Signature is not valid base64")

    def test_sign_deterministic(self):
        """Test that same input produces same signature."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        message = b"test message"
        sig1 = sign(private_key, message)
        sig2 = sign(private_key, message)
        assert sig1 == sig2

    def test_sign_different_messages_different_signatures(self):
        """Test that different messages produce different signatures."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        sig1 = sign(private_key, b"message1")
        sig2 = sign(private_key, b"message2")
        assert sig1 != sig2


class TestGetSignature:
    """Tests for get_signature function."""

    def test_get_signature_returns_base64(self):
        """Test that get_signature returns base64 encoded string."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        nonce = "1234567890"
        path = "/0/private/Balance"
        data = '{"test": "data"}'
        signature = get_signature(private_key, data, nonce, path)
        assert isinstance(signature, str)
        try:
            base64.b64decode(signature)
        except Exception:
            pytest.fail("Signature is not valid base64")

    def test_get_signature_uses_nonce_in_hash(self):
        """Test that different nonces produce different signatures."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        path = "/0/private/Balance"
        data = "test data"

        sig1 = get_signature(private_key, data, "nonce1", path)
        sig2 = get_signature(private_key, data, "nonce2", path)
        assert sig1 != sig2

    def test_get_signature_uses_path_in_hash(self):
        """Test that different paths produce different signatures."""
        private_key = base64.b64encode(
            b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode()
        nonce = "1234567890"
        data = "test data"

        sig1 = get_signature(private_key, data, nonce, "/0/private/Balance")
        sig2 = get_signature(private_key, data, nonce, "/0/private/OpenOrders")
        assert sig1 != sig2


class TestRequest:
    """Tests for request function."""

    @patch('urllib.request.urlopen')
    def test_request_get_method(self, mock_urlopen):
        """Test GET request."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        request(
            method="GET",
            path="/0/public/Time",
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        # Check that URL was properly formed
        request_obj = call_args[0][0]
        assert request_obj is not None

    @patch('urllib.request.urlopen')
    def test_request_post_method_with_body(self, mock_urlopen):
        """Test POST request with body."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        body = {"test": "data"}
        request(
            method="POST",
            path="/0/private/Balance",
            body=body,
            public_key="test_key",
            private_key=base64.b64encode(
                b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode(),
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        # Check that request was POST
        assert request_obj.get_method() == "POST"

    @patch('urllib.request.urlopen')
    def test_request_with_query_params(self, mock_urlopen):
        """Test request with query parameters."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        query = {"param1": "value1", "param2": "value2"}
        request(
            method="GET",
            path="/0/public/Assets",
            query=query,
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        # URL should contain query params
        assert "param1=value1" in request_obj.full_url or "param2=value2" in request_obj.full_url

    @patch('urllib.request.urlopen')
    def test_request_adds_api_key_header(self, mock_urlopen):
        """Test that API-Key header is added for authenticated requests."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        request(
            method="POST",
            path="/0/private/Balance",
            public_key="test_api_key",
            private_key=base64.b64encode(
                b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode(),
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        headers = {k.lower(): v for k, v in request_obj.headers.items()}
        assert "api-key" in headers
        assert headers["api-key"] == "test_api_key"

    @patch('urllib.request.urlopen')
    def test_request_adds_api_sign_header(self, mock_urlopen):
        """Test that API-Sign header is added for authenticated requests."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        request(
            method="POST",
            path="/0/private/Balance",
            public_key="test_api_key",
            private_key=base64.b64encode(
                b"test_private_key_64bytes_long__test_private_key_64bytes_long__test").decode(),
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        headers = {k.lower(): v for k, v in request_obj.headers.items()}
        assert "api-sign" in headers

    @patch('urllib.request.urlopen')
    def test_request_sets_content_type_json(self, mock_urlopen):
        """Test that Content-Type header is set to JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        body = {"test": "data"}
        request(
            method="POST",
            path="/0/private/Balance",
            body=body,
            environment="https://api.kraken.com"
        )

        assert mock_urlopen.called
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        headers = {k.lower(): v for k, v in request_obj.headers.items()}
        assert "content-type" in headers
        assert headers["content-type"] == "application/json"

    @patch('urllib.request.urlopen')
    def test_request_returns_response_object(self, mock_urlopen):
        """Test that request returns the response object."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_urlopen.return_value = mock_response

        response = request(
            method="GET",
            path="/0/public/Time",
            environment="https://api.kraken.com"
        )

        assert response == mock_response
