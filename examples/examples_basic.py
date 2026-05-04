"""
Basic example - store and retrieve data.

Prerequisites:
    pip install pynukez
    # A signer source for protected gateway envelopes.
    # This example uses an Ed25519 Solana keypair JSON file produced by
    # solana-keygen, but PyNukez also supports EVM envelope signing via
    # evm_private_key_path or a custom signing_key.

PyNukez signs gateway envelopes. It does not execute blockchain payments or
take custody of payment keys. Complete the transfer with your own wallet, CLI,
hardware signer, or custody workflow, then pass the resulting transaction
signature to confirm_storage().

Run:
    python examples/examples_basic.py
"""

from pynukez import Nukez

# Instantiate an SDK client.
# keypair_path is used to sign protected gateway envelopes with a local
# Ed25519 keypair. It is not used by PyNukez to move funds.
client = Nukez(keypair_path="~/.config/solana/id.json", network="devnet")

# Step 1: Request x402 payment instructions from the Nukez gateway.
# Pass the storage provider and number of storage units. If provider is
# omitted, the gateway currently defaults to "gcs".
print("Requesting storage quote...")
request = client.request_storage(units=1, provider="gcs")
print(f"Pay {request.amount or request.amount_sol} {request.pay_asset} "
      f"to {request.pay_to_address} on {request.network}")
print(f"Next step: {request.next_step}")

if request.payment_options:
    print("\nPayment options offered by this quote:")
    for option in request.payment_options:
        print(
            f"  {option.get('pay_asset'):<6} "
            f"{option.get('network'):<50} "
            f"amount={option.get('human_amount') or option.get('amount')}"
        )

# Step 2: Execute the transfer with your preferred payment method.
# Then assign the resulting transaction signature below.
tx_sig = input("Paste the transaction signature when the transfer is confirmed: ").strip()

# Step 3: Issue a receipt by confirming payment with the gateway.
receipt = client.confirm_storage(request.pay_req_id, tx_sig=tx_sig)
print(f"Receipt: {receipt.id}  (primary SDK handle for this locker)")

# Step 4: Provision the locker instance via the receipt.
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
