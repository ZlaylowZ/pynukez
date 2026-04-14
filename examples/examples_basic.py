"""
Basic example - store and retrieve data.

Prerequisites:
    pip install pynukez[solana]
    solana-keygen new --outfile ~/.config/solana/id.json  (if you don't have one)
    solana config set --url devnet && solana airdrop 2   (for devnet testing)

Run:
    python examples/examples_basic.py
"""

from pynukez import Nukez

# Setup (devnet by default)
client = Nukez(keypair_path="~/.config/solana/id.json")

# Step 1: Buy storage (3-step x402 payment flow)
print("Purchasing storage...")
request = client.request_storage(units=1)
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
print(f"Receipt: {receipt.id}  (SAVE THIS — it's your root credential)")

# Step 2: Provision the locker (one-time, per receipt)
client.provision_locker(receipt.id)

# Step 3: Create a file entry and get signed upload/download URLs
urls = client.create_file(receipt.id, "hello.txt", content_type="text/plain")

# Step 4: Upload raw bytes to the signed URL
client.upload_bytes(urls.upload_url, b"Hello, World!")
print("Uploaded!")

# Step 5: Confirm the file so the gateway records its content hash
#   (optional but recommended — enables attestation + merkle proofs)
if urls.confirm_url:
    # v3.4.0+ gateways return an absolute confirm_url in the create response
    client.confirm_file(receipt.id, urls.filename, confirm_url=urls.confirm_url)
else:
    # Older gateways: call without confirm_url, SDK falls back to hardcoded path
    client.confirm_file(receipt.id, urls.filename)

# Step 6: Retrieve the data
data = client.download_bytes(urls.download_url)
print(f"Downloaded: {data}")
