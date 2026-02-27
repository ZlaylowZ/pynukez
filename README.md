# PyNukez

**Persistent storage for AI agents. Pay with SOL, store anything, get cryptographic proof.**

```bash
pip install pynukez[solana]
```

## 30-Second Example

```python
from pynukez import Nukez

client = Nukez(keypair_path="~/.config/solana/id.json")

# Buy storage
request = client.request_storage()
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)

# Use it
client.provision_locker(receipt.id)
urls = client.create_file(receipt.id, "notes.txt")
client.upload_bytes(urls.upload_url, b"Hello!")
data = client.download_bytes(urls.download_url)  # b"Hello!"
```

**That's it.** Your agent now has permanent storage with a cryptographic receipt.

---

## First Time? Start Here

### 1. Get a Solana Wallet

```bash
# Install Solana CLI
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"

# Create wallet
solana-keygen new --outfile ~/.config/solana/id.json
```

### 2. Get Test Money (Free)

```bash
solana config set --url devnet
solana airdrop 2
```

### 3. Install PyNukez

```bash
pip install pynukez[solana]
```

### 4. Store Something

```python
from pynukez import Nukez

client = Nukez(keypair_path="~/.config/solana/id.json")

# Buy storage (costs ~0.001 SOL on devnet)
request = client.request_storage()
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)

print(f"Your receipt: {receipt.id}")  # Save this!

# Create your locker
client.provision_locker(receipt.id)

# Store a file
urls = client.create_file(receipt.id, "my_file.txt")
client.upload_bytes(urls.upload_url, b"My agent's data")

# Read it back
data = client.download_bytes(urls.download_url)
print(data)  # b"My agent's data"
```

---

## Quick Reference

| What you want | Code |
|--------------|------|
| Buy storage | `request = client.request_storage()` |
| Pay | `transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)` |
| Get receipt | `receipt = client.confirm_storage(request.pay_req_id, transfer.signature)` |
| Setup locker | `client.provision_locker(receipt.id)` |
| Store data | `urls = client.create_file(receipt.id, "file.txt")` then `client.upload_bytes(urls.upload_url, data)` |
| Get data | `data = client.download_bytes(urls.download_url)` |
| List files | `files = client.list_files(receipt.id)` |
| Delete file | `client.delete_file(receipt.id, "file.txt")` |
| Verify | `result = client.verify_storage(receipt.id)` |
| Merkle proof | `proof = client.get_merkle_proof(receipt.id, "file.txt")` |

---

## Sandboxed App Uploads

If your agent runs in a proxied app sandbox (for example, `/mnt/data` path restrictions), path uploads can fail even when locker auth is valid.

Use the sandbox ingest flow instead:

```python
job = client.sandbox_create_ingest_job(
    receipt_id=receipt.id,
    files=[{"filename": "image.png", "content_type": "image/png"}],
)

client.sandbox_append_ingest_part(
    receipt_id=receipt.id,
    job_id=job["job_id"],
    file_id=job["files"][0]["file_id"],
    part_no=0,
    payload_b64="<chunk-0-base64>",
    is_last=True,
)

result = client.sandbox_complete_ingest_job(
    receipt_id=receipt.id,
    job_id=job["job_id"],
)
```

Convenience helpers are available:
- `client.sandbox_upload_bytes(...)`
- `client.sandbox_upload_base64(...)`
- `client.sandbox_upload_file_path(...)`

Important: if a valid `receipt_id` already exists, reuse it. Do not purchase storage again unless explicitly requested.

---

## Important

**Save your `receipt.id`** — you need it for everything.

```python
# First time
receipt = client.confirm_storage(...)
print(receipt.id)  # Save this string somewhere!

# Later
files = client.list_files("your_saved_receipt_id")
```

---

## Going to Production

Change one line:

```python
# Devnet (testing)
client = Nukez(keypair_path="~/.config/solana/id.json", network="devnet")

# Mainnet (production)
client = Nukez(keypair_path="~/.config/solana/id.json", network="mainnet-beta")
```

---

## Common Issues

| Problem | Fix |
|---------|-----|
| "Transaction not found" | Wait 3 seconds and retry `confirm_storage()` |
| "Insufficient funds" | Run `solana airdrop 2` (devnet only) |
| "URL expired" | Call `client.get_file_urls(receipt_id, filename)` for fresh URLs |
| "File not found" | Check `client.list_files(receipt_id)` to see what exists |

---

## Links

- [Examples](./examples/) — Working code you can copy
- [API Reference](./docs/API.md) — Every method explained
- [Source Code](./pynukez/) — Read it yourself

---

## License

MIT
