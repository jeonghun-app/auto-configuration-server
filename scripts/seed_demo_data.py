#!/usr/bin/env python3
"""Seed demo subscribers through the admin API.

Every identifier below is from a reserved test range: MCC 001 / MNC 01 is the
IMSI test network and the MSISDNs are in a documentation range. No real
subscriber identifier should ever be committed to this repository.

    python scripts/seed_demo_data.py --base-url http://127.0.0.1:8080 \
        --admin-token local-admin-token
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEMO_SUBSCRIBERS: list[dict[str, object]] = [
    {
        "imsi": "001010000000001",
        "msisdn": "+821012345678",
        "entitled": True,
        "rcs_profile": "UP_2.4",
        "volte_enabled": True,
    },
    {
        "imsi": "001010000000002",
        "msisdn": "+821012345679",
        "entitled": True,
        "rcs_profile": "UP_1.0",
        "volte_enabled": True,
    },
    {
        "imsi": "001010000000003",
        "msisdn": "+821012345680",
        "entitled": True,
        "rcs_profile": "joyn_blackbird",
        "volte_enabled": False,
    },
    {
        # Not entitled: exercises the 403 path.
        "imsi": "001010000000009",
        "msisdn": "+821012345689",
        "entitled": False,
        "rcs_profile": "UP_2.4",
    },
    {
        # Operator-disabled with VERS=-2 (disable, wipe, do not re-query).
        "imsi": "001010000000010",
        "msisdn": "+821012345690",
        "entitled": True,
        "forced_vers": -2,
        "rcs_profile": "UP_2.4",
    },
    {
        # Per-subscriber override: a smaller file transfer limit.
        "imsi": "001010000000011",
        "msisdn": "+821012345691",
        "entitled": True,
        "rcs_profile": "UP_2.4",
        "overrides": {"APPLICATION:ap2002/MESSAGING/FT/MaxSizeFileTr": "2048"},
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed demo subscribers")
    parser.add_argument(
        "--base-url", default=os.environ.get("ACS_BASE_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument("--admin-token", default=os.environ.get("ACS_ADMIN_TOKEN", ""))
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args(argv)

    if not args.admin_token:
        print("error: an admin token is required", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.admin_token}"}
    failures = 0

    with httpx.Client(timeout=10, verify=not args.insecure) as client:
        for entry in DEMO_SUBSCRIBERS:
            imsi = str(entry.pop("imsi"))
            response = client.put(
                f"{base_url}/admin/subscribers/{imsi}", headers=headers, json=entry
            )
            if response.status_code == 200:
                print(f"  seeded {imsi} -> {entry['msisdn']} ({entry.get('rcs_profile')})")
            else:
                failures += 1
                print(f"  FAILED {imsi}: {response.status_code} {response.text}")

    print(f"\n{len(DEMO_SUBSCRIBERS) - failures} seeded, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
