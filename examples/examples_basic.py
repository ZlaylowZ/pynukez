"""
Basic example - store and retrieve data.

Prerequisites:
    pip install pynukez
    # An Ed25519 keypair JSON file (e.g. one produced by solana-keygen)
    # used to sign envelopes for Solana-paid lockers.

pynukez does NOT move funds. You execute the transfer yourself (wallet,
CLI, another tool) and hand the resulting tx signature to confirm_storage().

Run:
    python examples/examples_basic.py
"""

from pynukez import Nukez

# Setup (devnet by default)
client = Nukez(keypair_path="~/.config/solana/id.json")

# Step 1: Request a storage quote. This returns payment instructions:
# address, amount, asset, chain. pynukez will NOT execute the transfer.
print("Requesting storage quote...")
request = client.request_storage(units=1)
print(f"Pay {request.amount or request.amount_sol} {request.pay_asset} "
      f"to {request.pay_to_address} on {request.network}")
print(f"Next step: {request.next_step}")

# Step 2: Execute the transfer externally (wallet, CLI, another tool).
# Then capture the resulting transaction signature and paste it below.
tx_sig = input("Paste the transaction signature when the transfer is confirmed: ").strip()

# Step 3: Close the loop with the gateway.
receipt = client.confirm_storage(request.pay_req_id, tx_sig=tx_sig)
print(f"Receipt: {receipt.id}  (SAVE THIS — it's your root credential)")

# Step 4: Provision the locker (one-time, per receipt)
client.provision_locker(receipt.id)

# Step 5: Create a file entry and get signed upload/download URLs
urls = client.create_file(receipt.id, "hello.txt", content_type="text/plain")

# Step 6: Upload raw bytes to the signed URL
client.upload_bytes(urls.upload_url, b"Hello, World!")
print("Uploaded!")

# Step 7: Confirm the file so the gateway records its content hash
#   (optional but recommended — enables attestation + merkle proofs)
if urls.confirm_url:
    # v3.4.0+ gateways return an absolute confirm_url in the create response
    client.confirm_file(receipt.id, urls.filename, confirm_url=urls.confirm_url)
else:
    # Older gateways: call without confirm_url, SDK falls back to hardcoded path
    client.confirm_file(receipt.id, urls.filename)

# Step 8: Retrieve the data
data = client.download_bytes(urls.download_url)
print(f"Downloaded: {data}")
