"""
Persistent agent - save and load state across runs.

First run (creates a new receipt, prints it so you can reuse it later):
    python examples/examples_persistent_agent.py

Later runs (with saved receipt):
    python examples/examples_persistent_agent.py --receipt YOUR_RECEIPT_ID

PyNukez uses the configured signer for protected gateway envelopes. It does
not execute blockchain payments or take custody of payment keys.
"""

import json
import argparse
from pynukez import Nukez, NukezFileNotFoundError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", help="Existing receipt ID")
    parser.add_argument(
        "--provider",
        default="gcs",
        help="Storage provider for new receipts (default: gcs)",
    )
    args = parser.parse_args()

    client = Nukez(keypair_path="~/.config/solana/id.json", network="devnet")

    # Get or create storage
    if args.receipt:
        receipt_id = args.receipt
    else:
        print("Requesting storage quote...")
        request = client.request_storage(units=1, provider=args.provider)
        print(f"Pay {request.amount or request.amount_sol} {request.pay_asset} "
              f"to {request.pay_to_address} on {request.network}")
        # Complete the transfer with your own wallet, CLI, hardware signer, or
        # custody workflow, then paste the resulting transaction signature.
        tx_sig = input("Paste tx signature once confirmed: ").strip()
        receipt = client.confirm_storage(request.pay_req_id, tx_sig=tx_sig)
        receipt_id = receipt.id
        client.provision_locker(receipt_id)
        print(f"Receipt ID: {receipt_id}  (primary SDK handle for this locker)")

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

    # Save state — upload_bytes expects bytes, so encode the JSON string
    urls = client.create_file(receipt_id, "state.json", content_type="application/json")
    client.upload_bytes(urls.upload_url, json.dumps(state).encode("utf-8"))

    # Confirm the file so the gateway records its content hash
    if urls.confirm_url:
        client.confirm_file(receipt_id, urls.filename, confirm_url=urls.confirm_url)
    else:
        client.confirm_file(receipt_id, urls.filename)

    print(f"Saved state: {state}")


if __name__ == "__main__":
    main()
