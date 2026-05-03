# PyNukez

**Persistent storage for AI agents. Store anything, receive a cryptographic receipt, and confirm payment with the transaction signature from your own wallet, CLI, or signing workflow. PyNukez never executes transfers or takes custody of keys.**

[![PyPI](https://img.shields.io/pypi/v/pynukez.svg)](https://pypi.org/project/pynukez/)
[![Python](https://img.shields.io/pypi/pyversions/pynukez.svg)](https://pypi.org/project/pynukez/)
[![License](https://img.shields.io/pypi/l/pynukez.svg)](https://github.com/Nukez-xyz/pynukez/blob/main/LICENSE)

```bash
pip install pynukez
```

Native support for both Solana and Monad blockchains. Thus, this includes support for Ed25519 and secp256k1 keypairs. The entire PyNukez library is designed and built for direct integration with agentic systems. The landmark agentic storage protocol is optimized for use by autonomous agents. Compatible with any model provider, agentic platform, or other integrations.

Requires Python 3.9+.

## How it works

1. `request_storage()` asks the gateway for a quote. You receive payment instructions — address, amount, asset, chain.
2. **You execute the transfer with your own wallet, CLI, hardware signer, or signing workflow.** PyNukez never touches funds or custody keys.
3. `confirm_storage(pay_req_id, tx_sig=<your_tx_sig>)` closes the loop and returns a receipt.
4. Use the receipt to provision a locker and upload / download / verify files.

The PyNukez SDK does not execute blockchain payments. That boundary is intentional: payment keys stay in the wallet, CLI, signer, or custody system you choose. Please visit https://nukez.xyz/docs/pynukez/helpers for examples and external helpers to facilitate cryptographic operations for agentic workflows.

## 30-Second Example

```python
from pathlib import Path
from pynukez import Nukez

# Instantiate an instance of the Nukez class.
# keypair_path is one supported signer source. Use it when you want PyNukez
# to sign protected gateway envelopes with a local Solana keypair.
# Omit it only when providing signing_key or evm_private_key_path instead.
client = Nukez(
    keypair_path="~/.config/solana/id.json",  # local Ed25519 envelope signer
)

# Request the x402 payment instructions from the Nukez gateway.
# Pass the preferred storage provider and quantity of storage units.
# If no storage provider is set, PyNukez defaults to "gcs".
request = client.request_storage(units=1, provider="gcs")

# Print details for next step
print(request.next_step)
# -> "Transfer 0.001 SOL to <addr> on solana-devnet,
#     then call confirm_storage(pay_req_id='...', tx_sig=<your_tx_signature>)"

# Using the x402 payment details assigned to the request variable
# Complete transfer via preferred method
# Assign transaction signature from above transfer to variable like so:
tx_sig = "..."

# Issue receipt object by confirming payment with the Nukez Gateway
receipt = client.confirm_storage(request.pay_req_id, tx_sig=tx_sig)

# Provision storage locker instance via the receipt
client.provision_locker(receipt.id)

# Upload an actual local file by path. PyNukez reads bytes from disk;
# the file contents do not need to pass through your prompt or notebook.
local_file = Path("~/Documents/report.pdf").expanduser()
uploaded = client.upload_file_path(
    receipt.id,
    str(local_file),
    content_type="application/pdf",
)

# Fetch fresh URLs when you want to read it back.
urls = client.get_file_urls(receipt.id, uploaded["filename"])
data = client.download_bytes(urls.download_url)
```

### Async version

```python
from pynukez import AsyncNukez

async with AsyncNukez(
    keypair_path="~/.config/solana/id.json",  # local Ed25519 envelope signer
) as client:
    request = await client.request_storage(units=1)
    # ... execute the transfer externally ...
    receipt = await client.confirm_storage(request.pay_req_id, tx_sig=tx_sig)
    # ... same methods as sync, just awaited
```

---

## Quick Reference

| What you want | Code |
|--------------|------|
| Buy storage (quote) | `request = client.request_storage(units=1)` |
| Confirm payment | `receipt = client.confirm_storage(request.pay_req_id, tx_sig=<your_tx_sig>)` |
| Setup locker | `client.provision_locker(receipt.id)` |
| Store bytes | `urls = client.create_file(receipt.id, "file.txt")` then `client.upload_bytes(urls.upload_url, data)` |
| Store file | `client.upload_file_path(receipt.id, "/path/to/file.pdf")` |
| Batch upload | `client.bulk_upload_paths(receipt.id, [{"filepath": "a.pdf"}, {"filepath": "b.txt"}])` |
| Store directory | `client.upload_directory(receipt.id, "/path/to/dir", pattern="*.pdf", recursive=True)` |
| Confirm hash | `client.confirm_file(receipt.id, "file.txt", confirm_url=urls.confirm_url)` |
| Get data | `data = client.download_bytes(urls.download_url)` |
| List files | `files = client.list_files(receipt.id)` |
| Delete file | `client.delete_file(receipt.id, "file.txt")` |
| Receipt hash | `check = client.verify_receipt_hash(receipt.id)` |
| Verify | `result = client.verify_storage(receipt.id)` |
| Attest | `att = client.attest(receipt.id)` |
| Merkle proof | `proof = client.get_merkle_proof(receipt.id, "file.txt")` |
| Files manifest | `client.get_files_manifest(receipt.id)` |
| Locker record | `client.get_locker_record(receipt.id)` |
| Delegate | `client.add_operator(receipt.id, operator_pubkey)` |
| Viewer link | `client.get_owner_viewer_url(receipt.id)` |

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

# Later — fresh process, reconstructed client:
client.bind_receipt(receipt)          # or: bind_receipt(receipt_id=..., owner_identity=...)
files = client.list_files(receipt.id)
```

`confirm_storage()` primes per-receipt state automatically in the same
process. Across kernel restarts, subprocesses, or receipts loaded from
disk/DB, call `bind_receipt(receipt)` before owner-only ops
(`add_operator`, `remove_operator`) — on dual-key clients, the SDK
refuses to guess which signer to use and raises `ReceiptStateNotBoundError`
instead.

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
| "Transaction not found" | The tx hasn't propagated yet. Wait a few seconds and retry `confirm_storage()` |
| "URL expired" | Call `client.get_file_urls(receipt_id, filename)` for fresh URLs |
| "File not found" | Check `client.list_files(receipt_id)` to see what exists |
| `ReceiptStateNotBoundError` | Call `client.bind_receipt(receipt)` before the op (cross-session / fresh-client flows) |
| `AuthenticationError: Envelope sig_alg '...' incompatible with ... network` | Dual-key client picked wrong signer — call `client.bind_receipt(receipt)` first |

---

## Links

- [Full SDK Reference](https://github.com/Nukez-xyz/pynukez/blob/main/docs/SDK_REFERENCE.md) — Every method, type, and error documented
- [Examples](https://github.com/Nukez-xyz/pynukez/tree/main/examples) — Working code you can copy
- [PyPI](https://pypi.org/project/pynukez/) — Published releases
- [GitHub](https://github.com/Nukez-xyz/pynukez) — Source code, issues, releases
- [Contributing](https://github.com/Nukez-xyz/pynukez/blob/main/CONTRIBUTING.md) — Dev setup and PR workflow

---

## License

MIT
