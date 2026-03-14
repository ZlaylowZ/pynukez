# tests/conftest.py
"""
Shared test fixtures for sync and async PyNukez tests.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture
def mock_keypair():
    """Patch Keypair so clients can be created without a real keypair file."""
    with patch("pynukez.client.Keypair") as mock_kp:
        mock_kp.return_value.pubkey_b58 = "FakePublicKey123456789"
        mock_kp.return_value.sign_message.return_value = "FakeSignature"
        yield mock_kp


@pytest.fixture
def sync_client(mock_keypair):
    """Create a sync Nukez client with mocked HTTP."""
    from pynukez import Nukez
    client = Nukez(keypair_path="~/.config/solana/id.json")
    client.http = MagicMock()
    return client


@pytest.fixture
def async_client(mock_keypair):
    """Create an AsyncNukez client with mocked HTTP."""
    with patch("pynukez._async_client.Keypair") as async_mock_kp:
        async_mock_kp.return_value.pubkey_b58 = "FakePublicKey123456789"
        async_mock_kp.return_value.sign_message.return_value = "FakeSignature"
        from pynukez import AsyncNukez
        client = AsyncNukez(keypair_path="~/.config/solana/id.json")
        client.http = AsyncMock()
        client._raw_client = AsyncMock()
        yield client
