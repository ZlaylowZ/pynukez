# Quick Start

You need two things:
1. Python 3.9+
2. A keypair you use to sign envelopes on the locker owner side.
   - **Solana-paid lockers**: an Ed25519 keypair JSON (e.g. a file produced by
     `solana-keygen new`).
   - **EVM-paid lockers**: an EVM private key JSON (`pip install pynukez[evm]`).

pynukez does NOT move funds. You execute payments out-of-band (wallet,
CLI, hardware signer, etc.) and hand us the resulting tx signature.

## Step 1: Install

```bash
pip install pynukez
# Add EVM envelope signing if you will own EVM-paid lockers:
pip install pynukez[evm]
```

## Step 2: Provide a keypair

Any Ed25519 keypair in the Solana JSON format works. If you don't have one:

```bash
# Optional — only if you want to use solana-keygen to produce a key file
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"
solana-keygen new
```

The resulting file at `~/.config/solana/id.json` is what `keypair_path`
expects.

## Step 3: Test It

```python
from pynukez import Nukez

client = Nukez(keypair_path="~/.config/solana/id.json")

# Check price (no payment or on-chain activity)
price = client.get_price()
print(f"Storage costs: {price.amount_sol} SOL")
```

If that works, you're ready. Go read the [README](../README.md).
