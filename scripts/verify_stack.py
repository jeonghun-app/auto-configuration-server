#!/usr/bin/env python3
"""Verify a running ACS end to end.

Seeds a demo subscriber through the admin API, then drives both client
simulators: the RCS client (RCC.14 / OMA-CP) and the OMA-DM client, chaining
them exactly as a handset does — the DM password comes from the ``w7``
characteristic the ACS emitted during RCS provisioning.

Exits non-zero on any failure, so it works as a deployment gate:

    python scripts/verify_stack.py --base-url https://acs.example.com --admin-token ...

Environment variables ``ACS_BASE_URL`` and ``ACS_ADMIN_TOKEN`` are used as
defaults, which is how the docker-compose ``verify`` profile invokes it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from tools.dm_client_sim import Checker as DmChecker  # noqa: E402
from tools.dm_client_sim import DmClientSimulator, run_session  # noqa: E402
from tools.rcs_client_sim import Checker, RcsClientSimulator, scenario_full  # noqa: E402

DEMO_IMSI = "001010000000001"
DEMO_MSISDN = "+821012345678"
DEMO_IMEI = "356938035643809"


def wait_for_health(base_url: str, timeout: float = 60.0, verify_tls: bool = True) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=5, verify=verify_tls)
            if response.status_code == 200:
                print(f"ACS healthy: {response.json()}")
                return
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(1)
    raise SystemExit(f"ACS did not become healthy within {timeout:.0f}s ({last})")


def seed_subscriber(base_url: str, admin_token: str, verify_tls: bool = True) -> None:
    if not admin_token:
        print("no admin token supplied; assuming the subscriber already exists")
        return
    response = httpx.put(
        f"{base_url}/admin/subscribers/{DEMO_IMSI}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "msisdn": DEMO_MSISDN,
            "entitled": True,
            "rcs_profile": "UP_2.4",
            "provisioning_version": 1,
            "volte_enabled": True,
        },
        timeout=10,
        verify=verify_tls,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"failed to seed the demo subscriber: {response.status_code} {response.text}"
        )
    print(f"seeded demo subscriber {DEMO_IMSI}")


def report_coverage(base_url: str, admin_token: str, verify_tls: bool = True) -> None:
    if not admin_token:
        return
    response = httpx.get(
        f"{base_url}/admin/coverage",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
        verify=verify_tls,
    )
    if response.status_code != 200:
        return
    body = response.json()
    omacp = body["omacp"]
    omadm = body["omadm"]
    print("\nSpecification coverage as reported by the server:")
    print(
        f"  OMA-CP  {omacp['parameters']} parameters "
        f"({omacp['verified']} cross-checked), profile {omacp['profile']}"
    )
    print(
        f"  OMA-DM  {omadm['management_objects']} management objects, "
        f"{omadm['nodes']} nodes ({omadm['verified']} cross-checked)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end ACS verification")
    parser.add_argument(
        "--base-url", default=os.environ.get("ACS_BASE_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument("--admin-token", default=os.environ.get("ACS_ADMIN_TOKEN", ""))
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("--skip-dm", action="store_true", help="verify the RCS plane only")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    verify_tls = not args.insecure

    print("=" * 72)
    print(f"Verifying ACS at {base_url}")
    print("=" * 72)

    wait_for_health(base_url, verify_tls=verify_tls)
    seed_subscriber(base_url, args.admin_token, verify_tls)

    print("\n--- RCS auto-configuration (RCC.14 / OMA-CP) ---")
    checker = Checker()
    dm_password = ""
    sim = RcsClientSimulator(
        base_url=base_url,
        imsi=DEMO_IMSI,
        imei=DEMO_IMEI,
        profile="UP_2.4",
        verify_tls=verify_tls,
    )
    try:
        scenario_full(sim, checker, DEMO_MSISDN)
        # Harvest the DM credentials the ACS just provisioned.
        response = sim.request_configuration()
        if response.content:
            root = sim.parse_document(response.content)
            account = sim.application(root, "w7")
            if account is not None:
                secret = account.find("parm[@name='AAUTHSECRET']")
                dm_password = (secret.get("value") or "") if secret is not None else ""
    finally:
        sim.close()

    rcs_ok = checker.summary()

    dm_ok = True
    if not args.skip_dm:
        print("\n--- OMA-DM device management (SyncML DM 1.2) ---")
        if not dm_password:
            # Re-provision to obtain the w7 account; a VERS-only answer carries none.
            print("  re-provisioning to obtain the OMA-DM account")
            sim = RcsClientSimulator(
                base_url=base_url,
                imsi=DEMO_IMSI,
                imei=DEMO_IMEI,
                profile="UP_2.4",
                verify_tls=verify_tls,
            )
            try:
                if args.admin_token:
                    httpx.post(
                        f"{base_url}/admin/subscribers/{DEMO_IMSI}/invalidate",
                        headers={"Authorization": f"Bearer {args.admin_token}"},
                        timeout=10,
                        verify=verify_tls,
                    )
                response = sim.request_configuration()
                if response.status_code == 200 and not response.content:
                    otp = sim.fetch_otp(DEMO_MSISDN)
                    response = sim.request_configuration(OTP=otp)
                if response.content:
                    root = sim.parse_document(response.content)
                    account = sim.application(root, "w7")
                    if account is not None:
                        secret = account.find("parm[@name='AAUTHSECRET']")
                        dm_password = (secret.get("value") or "") if secret is not None else ""
            finally:
                sim.close()

        if not dm_password:
            print("  FAIL  could not obtain the OMA-DM password from the w7 characteristic")
            dm_ok = False
        else:
            print("  OMA-DM credentials obtained from the OMA-CP w7 characteristic")
            dm_checker = DmChecker()
            dm_sim = DmClientSimulator(
                base_url=base_url,
                imsi=DEMO_IMSI,
                imei=DEMO_IMEI,
                password=dm_password,
                auth="basic",
                verify_tls=verify_tls,
            )
            try:
                run_session(dm_sim, dm_checker)
            finally:
                dm_sim.close()
            dm_ok = dm_checker.summary()

    report_coverage(base_url, args.admin_token, verify_tls)

    print("\n" + "=" * 72)
    if rcs_ok and dm_ok:
        print("RESULT: PASS — both planes behave as specified")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
