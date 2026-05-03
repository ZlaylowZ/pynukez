# Quick Start

You need two things:
1. Python 3.9+
2. A way to sign owner/operator envelopes. For local scripts, the constructor
   can optionally read a key file:
   - **Solana-paid lockers**: an Ed25519 keypair JSON (e.g. a file produced by
     `solana-keygen new`).
   - **EVM-paid lockers**: an EVM private key JSON
     (`{"address": "0x...", "private_key": "0x..."}`).
   You can also provide another signer bridge instead. Nukez does not custody,
   receive, or store client keypair material.

PyNukez does not execute blockchain payments. Complete the transfer with your
own wallet, CLI, hardware signer, or custody workflow, then submit the
resulting transaction signature to confirm storage.

## Step 1: Install

```bash
pip install pynukez
```

That's it. Envelope signing for both Solana-paid and EVM-paid lockers is in the base install.

## Step 2: Choose a signer

For local Ed25519 envelope signing, any Solana JSON keypair works. If you want
to create one with the Solana CLI:

```bash
# Optional — only if you want to use solana-keygen to produce a key file
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"
solana-keygen new
```

The resulting file at `~/.config/solana/id.json` is what optional
`keypair_path` expects. If your app uses a relay, wallet bridge, HSM, or custom
signer, use that instead.

## Step 3: Test It

```python
from pynukez import Nukez

client = Nukez(
    keypair_path="~/.config/solana/id.json",  # local Ed25519 envelope signer
)

# Check price (no payment or on-chain activity)
price = client.get_price()
print(f"Storage costs: {price.amount_sol} SOL")
```

If that works, you're ready. Go read the [README](../README.md).
