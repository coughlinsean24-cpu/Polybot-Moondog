"""
Polybot Snipez — API Credential Derivation

Polymarket's L2 API credentials (key / secret / passphrase) are not issued by
support or any web dashboard. They are derived from your wallet's private key
by signing a message, which is what this script does.

Run it ONCE after putting POLYMARKET_PRIVATE_KEY in your .env:

    python derive_creds.py

Then paste the three lines it prints into .env. If you have derived creds
before, this returns the same ones — it is safe to run again.

Nothing is written to disk and no order is ever placed. Your private key is
used only to sign the auth message sent to Polymarket's CLOB.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
CHAIN_ID = 137  # Polygon mainnet


def main() -> int:
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()

    if not private_key:
        print(
            "\nPOLYMARKET_PRIVATE_KEY is not set.\n\n"
            "  1. Copy .env.example to .env\n"
            "  2. Put your wallet's private key in POLYMARKET_PRIVATE_KEY\n"
            "  3. Run this script again\n"
        )
        return 1

    from py_clob_client.client import ClobClient

    try:
        client = ClobClient(host=CLOB_URL, chain_id=CHAIN_ID, key=private_key)
    except Exception as e:
        print(f"\nCould not load that private key: {e}")
        print("Check that POLYMARKET_PRIVATE_KEY is a full hex key starting with 0x.\n")
        return 1

    print(f"\nDeriving API credentials for {client.get_address()} ...")

    try:
        creds = client.create_or_derive_api_creds()
    except Exception as e:
        print(f"\nDerivation failed: {e}")
        print(
            "Most common cause: this wallet has never been used on Polymarket.\n"
            "Deposit and place one trade through the website first, then retry.\n"
        )
        return 1

    print("\nDone. Paste these three lines into your .env:\n")
    print("-" * 62)
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
    print("-" * 62)
    print(
        "\nStill needed before the bot can trade: POLYMARKET_PROXY_ADDRESS.\n"
        "That is your Polymarket profile address (the one holding your USDC),\n"
        "not the wallet address shown above.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
