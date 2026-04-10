"""
Minimal CLI for pynukez — eliminates the need to curl signed endpoints directly.

Usage:
    pynukez provision --receipt-id <id> --keypair ~/.config/solana/id.json
    pynukez provision --receipt-id <id> --evm-key ~/.keys/evm_key.json
"""

import argparse
import json
import sys

from .auth import Keypair, build_signed_envelope, compute_locker_id


def _provision(args):
    """Execute a signed provision request and print the result."""
    from ._http import HTTPClient

    # Resolve signer
    if args.keypair:
        signer = Keypair(args.keypair)
    elif args.evm_key:
        from .signer import EVMSigner

        signer = EVMSigner.from_file(args.evm_key)
    else:
        print("error: provide --keypair or --evm-key", file=sys.stderr)
        sys.exit(1)

    receipt_id = args.receipt_id
    tags = args.tags or []
    body = {"receipt_id": receipt_id, "tags": tags}

    if args.operator_pubkey:
        body["operator_pubkey"] = args.operator_pubkey

    envelope = build_signed_envelope(
        signer=signer,
        receipt_id=receipt_id,
        method="POST",
        path="/v1/storage/signed_provision",
        ops=["locker:provision"],
        body=body,
    )

    http = HTTPClient(base_url=args.base_url)
    try:
        response = http.post(
            "/v1/storage/signed_provision",
            headers=envelope.headers,
            data=envelope.canonical_body.encode("utf-8"),
        )
        print(json.dumps(response, indent=2))
    finally:
        http.close()


def main():
    parser = argparse.ArgumentParser(
        prog="pynukez",
        description="PyNukez CLI — signed operations without curl",
    )
    sub = parser.add_subparsers(dest="command")

    # --- provision ---
    prov = sub.add_parser("provision", help="Provision a locker with signed envelope auth")
    prov.add_argument("--receipt-id", required=True, help="Receipt ID from confirm_storage()")
    prov.add_argument("--keypair", help="Path to Solana keypair JSON (Ed25519)")
    prov.add_argument("--evm-key", help="Path to EVM private key JSON (secp256k1)")
    prov.add_argument("--tags", nargs="*", default=[], help="Optional locker tags")
    prov.add_argument("--operator-pubkey", help="Optional operator pubkey to authorize")
    prov.add_argument(
        "--base-url",
        default="https://api.nukez.xyz",
        help="Gateway URL (default: https://api.nukez.xyz)",
    )

    args = parser.parse_args()

    if args.command == "provision":
        _provision(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
