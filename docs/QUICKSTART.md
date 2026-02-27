# Quick Start

You need three things:
1. Python
2. A Solana wallet
3. Some SOL (free on devnet)

## Step 1: Install

```bash
pip install pynukez[solana]
```

## Step 2: Create a Wallet

If you don't have a Solana wallet:

```bash
# Install Solana tools
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"

# Create wallet (writes to ~/.config/solana/id.json)
solana-keygen new
```

## Step 3: Get Free SOL

```bash
solana config set --url devnet
solana airdrop 2
```

## Step 4: Test It

```python
from pynukez import Nukez

client = Nukez(keypair_path="~/.config/solana/id.json")

# Check balance
wallet = client.get_wallet_info()
print(f"Balance: {wallet.balance_sol} SOL")

# Check price
price = client.get_price()
print(f"Storage costs: {price.amount_sol} SOL")
```

If that works, you're ready. Go read the [README](./README.md).
