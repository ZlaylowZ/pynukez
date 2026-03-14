# tests/test_async_http_client.py
"""
Async HTTP client tests — mirrors sync test_http_client.py structure.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pynukez._async_http import AsyncHTTPClient
from pynukez.errors import (
    NukezError, AuthenticationError, NukezFileNotFoundError,
    RateLimitError, PaymentRequiredError,
)


class MockResponse:
    """Mock HTTP response for async tests."""
    def __init__(self, status_code=200, json_data=None, text="", headers=None, url="https://api.nukez.xyz/test"):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}
        self.content = text.encode() if isinstance(text, str) else text
        self.url = url

    def json(self):
        return self._json


class TestAsyncHTTPInit:
    """Async HTTP client initialization tests."""

    def test_default_base_url(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        assert "nukez" in http.base_url.lower()

    def test_custom_base_url(self):
        http = AsyncHTTPClient(base_url="https://custom.example.com")
        assert http.base_url == "https://custom.example.com"

    def test_trailing_slash_stripped(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz/")
        assert http.base_url == "https://api.nukez.xyz"

    def test_has_required_methods(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        for method in ("get", "post", "put", "delete", "aclose"):
            assert hasattr(http, method), f"Missing {method} method"


class TestAsyncHTTPMethods:
    """Test async HTTP methods with mocked httpx.AsyncClient."""

    async def test_get_success(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(status_code=200, json_data={"files": []})
        mock_resp.content = b'{"files": []}'
        http.client = AsyncMock()
        http.client.get = AsyncMock(return_value=mock_resp)

        result = await http.get("/v1/files", params={"receipt_id": "r123"})
        assert result == {"files": []}
        http.client.get.assert_called_once()

    async def test_post_success(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(status_code=200, json_data={"ok": True})
        mock_resp.content = b'{"ok": true}'
        http.client = AsyncMock()
        http.client.post = AsyncMock(return_value=mock_resp)

        result = await http.post("/v1/files", json={"filename": "test.txt"})
        assert result == {"ok": True}

    async def test_delete_success(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(status_code=200, json_data={"deleted": True})
        mock_resp.content = b'{"deleted": true}'
        http.client = AsyncMock()
        http.client.delete = AsyncMock(return_value=mock_resp)

        result = await http.delete("/v1/files/test.txt")
        assert result == {"deleted": True}

    async def test_put_success(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(status_code=200, json_data={"uploaded": True})
        mock_resp.content = b'{"uploaded": true}'
        http.client = AsyncMock()
        http.client.put = AsyncMock(return_value=mock_resp)

        result = await http.put("/v1/files/test.txt", content=b"data")
        assert result == {"uploaded": True}

    async def test_get_error_401(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(
            status_code=401,
            json_data={"message": "Auth needed"},
        )
        http.client = AsyncMock()
        http.client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(AuthenticationError):
            await http.get("/v1/files")

    async def test_post_error_402(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(
            status_code=402,
            json_data={"pay_req_id": "pr_1", "pay_to_address": "addr", "amount_sol": 0.01, "network": "devnet"},
        )
        http.client = AsyncMock()
        http.client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(PaymentRequiredError):
            await http.post("/v1/storage/request")

    async def test_get_error_429(self):
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        mock_resp = MockResponse(
            status_code=429,
            json_data={"message": "Rate limited"},
            headers={"Retry-After": "60"},
        )
        http.client = AsyncMock()
        http.client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(RateLimitError):
            await http.get("/v1/files")

    async def test_timeout_raises_nukez_error(self):
        import httpx
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        http.client = AsyncMock()
        http.client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with pytest.raises(NukezError, match="timed out"):
            await http.get("/v1/files")

    async def test_connection_error_raises_nukez_error(self):
        import httpx
        http = AsyncHTTPClient(base_url="https://api.nukez.xyz")
        http.client = AsyncMock()
        http.client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(NukezError, match="Request failed"):
            await http.post("/v1/files")


class TestAsyncHTTPContextManager:
    """Test async context manager protocol."""

    async def test_context_manager(self):
        async with AsyncHTTPClient(base_url="https://api.nukez.xyz") as http:
            assert http.base_url == "https://api.nukez.xyz"
