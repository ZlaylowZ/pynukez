"""
Persistent agent - save and load state across runs.

First run:
    python examples/persistent_agent.py

Later runs (with saved receipt):
    python examples/persistent_agent.py --receipt YOUR_RECEIPT_ID
"""

import json
import argparse
from pynukez import Nukez, NukezFileNotFoundError

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", help="Existing receipt ID")
    args = parser.parse_args()
    
    client = Nukez(keypair_path="~/.config/solana/id.json")
    
    # Get or create storage
    if args.receipt:
        receipt_id = args.receipt
    else:
        print("Purchasing storage...")
        request = client.request_storage()
        transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
        receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
        receipt_id = receipt.id
        client.provision_locker(receipt_id)
        print(f"Save this receipt: {receipt_id}")
    
    # Load existing state (or start fresh)
    try:
        urls = client.get_file_urls(receipt_id, "state.json")
        state = json.loads(client.download_bytes(urls.download_url))
        print(f"Loaded state: {state}")
    except NukezFileNotFoundError:
        state = {"runs": 0}
        print("Starting fresh")
    
    # Update state
    state["runs"] += 1
    state["last_message"] = f"Run #{state['runs']}"
    
    # Save state
    urls = client.create_file(receipt_id, "state.json")
    client.upload_bytes(urls.upload_url, json.dumps(state))
    print(f"Saved state: {state}")

if __name__ == "__main__":
    main()
