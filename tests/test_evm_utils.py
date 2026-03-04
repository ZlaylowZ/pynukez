# tests/test_evm_utils.py
"""
Batch 4C: EVM utility tests — pure data checks on token registry.
"""
import pytest

try:
    from pynukez.evm_payment import EVM_TOKENS
    HAS_EVM = True
except ImportError:
    HAS_EVM = False


@pytest.mark.skipif(not HAS_EVM, reason="evm_payment module not available")
class TestEvmTokenRegistry:
    """EVM token registry data validation."""

    def test_registry_is_dict(self):
        assert isinstance(EVM_TOKENS, dict)

    def test_registry_has_entries(self):
        """Should have at least one token entry."""
        assert len(EVM_TOKENS) >= 1

    def test_entry_has_required_fields(self):
        """Each chain entry maps symbol → {address, decimals}."""
        for chain_id, tokens in EVM_TOKENS.items():
            assert isinstance(chain_id, int), f"Chain key {chain_id} is not int"
            assert isinstance(tokens, dict), f"Tokens for chain {chain_id} is not a dict"
            for symbol, cfg in tokens.items():
                assert isinstance(cfg, dict), f"Token {symbol} on chain {chain_id} is not a dict"
                assert "address" in cfg, f"Token {symbol} on chain {chain_id} missing address"
                assert "decimals" in cfg, f"Token {symbol} on chain {chain_id} missing decimals"
