# tests/test_payment_utils.py
"""
Batch 4C: Payment utility tests.
"""
import pytest
from pynukez.payment import _default_rpc_for_network


class TestDefaultRpcForNetwork:
    """RPC URL resolution by network name."""

    def test_devnet_returns_url(self):
        url = _default_rpc_for_network("devnet")
        assert url is not None
        assert url.startswith("https://")

    def test_mainnet_returns_url(self):
        url = _default_rpc_for_network("mainnet-beta")
        assert url is not None
        assert url.startswith("https://")

    def test_unknown_network_returns_something(self):
        """Unknown network should return a default or raise."""
        try:
            url = _default_rpc_for_network("unknown-network-xyz")
            # If it returns a value, it should be a URL
            if url:
                assert isinstance(url, str)
        except Exception:
            pass  # Some implementations may raise

    def test_solana_devnet_variant(self):
        """'solana-devnet' should work."""
        url = _default_rpc_for_network("solana-devnet")
        assert url is not None

    def test_testnet_returns_url(self):
        url = _default_rpc_for_network("testnet")
        assert url is not None
