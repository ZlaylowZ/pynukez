"""
Basic example - store and retrieve data.

Run:
    python examples/basic.py
"""

from pynukez import Nukez

# Setup
client = Nukez(keypair_path="~/.config/solana/id.json")

# Buy storage
print("Purchasing storage...")
request = client.request_storage()
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
print(f"Receipt: {receipt.id}")

# Create locker
client.provision_locker(receipt.id)

# Store data
urls = client.create_file(receipt.id, "hello.txt")
client.upload_bytes(urls.upload_url, b"Hello, World!")
print("Uploaded!")

# Retrieve data
data = client.download_bytes(urls.download_url)
print(f"Downloaded: {data}")
