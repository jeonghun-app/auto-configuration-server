#!/usr/bin/env python3
"""Verify a running ACS end to end.

Seeds a demo subscriber through the admin API, drives the RCS configuration
plane, then runs a full OMA-DM session using the password the ACS itself
provisioned in the ``w7`` characteristic — the same chain a handset follows.

Two verification paths, chosen automatically:

**OTP path** — used when the mock SMS outbox (``/dev/sms``) is reachable, i.e. a
development deployment. Exercises the complete RCC.14 challenge flow: empty 200,
OTP retrieval, re-request, configuration.

**Token path** — used otherwise, which is every staging and production
deployment, because the mock outbox does not exist there and driving a real SMS
costs money and needs an origination identity. The operator mints a provisioning
token through the admin API and the client presents it. Everything after
authentication is identical, so document generation, versioning and the DM
bootstrap are all still verified.

Exits non-zero on any failure, so it works as a deployment gate.

    python scripts/verify_stack.py --base-url https://acs.example.com --admin-token ...
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.dm_client_sim import Checker as DmChecker  # noqa: E402
from tools.dm_client_sim import DmClientSimulator, run_session  # noqa: E402
from tools.rcs_client_sim import (  # noqa: E402
    Checker,
    RcsClientSimulator,
    scenario_full,
    scenario_token,
)

DEMO_IMSI = "001010000000001"
DEMO_MSISDN = "+821012345678"
DEMO_IMEI = "356938035643809"


def wait_for_health(base_url: str, timeout: float = 90.0, verify_tls: bool = True) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=5, verify=verify_tls)
            if response.status_code == 200:
                print(f"  healthy: {response.json()}")
                return
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise SystemExit(f"ACS did not become healthy within {timeout:.0f}s ({last})")


def report_readiness(base_url: str, verify_tls: bool = True) -> None:
    response = httpx.get(f"{base_url}/readyz", timeout=10, verify=verify_tls)
    body = response.json()
    print(f"  readyz: {response.status_code} {body.get('status')}")
    for key, value in (body.get("checks") or {}).items():
        print(f"    {key}: {value}")
    if response.status_code != 200:
        raise SystemExit("ACS is not ready")


def seed_subscriber(base_url: str, admin_token: str, verify_tls: bool = True) -> None:
    if not admin_token:
        print("  no admin token supplied; assuming the subscriber already exists")
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
        timeout=15,
        verify=verify_tls,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"failed to seed the demo subscriber: {response.status_code} {response.text}"
        )
    print(f"  seeded demo subscriber {DEMO_IMSI}")


def otp_outbox_available(base_url: str, verify_tls: bool = True) -> bool:
    try:
        response = httpx.get(
            f"{base_url}/dev/sms", params={"msisdn": DEMO_MSISDN}, timeout=10, verify=verify_tls
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def mint_token(base_url: str, admin_token: str, verify_tls: bool = True) -> str:
    response = httpx.post(
        f"{base_url}/admin/subscribers/{DEMO_IMSI}/issue-token",
        params={"imei": DEMO_IMEI},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=15,
        verify=verify_tls,
    )
    if response.status_code != 200:
        raise SystemExit(f"failed to mint a token: {response.status_code} {response.text}")
    return str(response.json()["token"])


def dm_password_from(sim: RcsClientSimulator, payload: bytes) -> str:
    root = sim.parse_document(payload)
    account = sim.application(root, "w7")
    if account is None:
        return ""
    secret = account.find("parm[@name='AAUTHSECRET']")
    return (secret.get("value") or "") if secret is not None else ""


def report_coverage(base_url: str, admin_token: str, verify_tls: bool = True) -> None:
    if not admin_token:
        return
    response = httpx.get(
        f"{base_url}/admin/coverage",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=15,
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

    print("\n--- Health and readiness ---")
    wait_for_health(base_url, verify_tls=verify_tls)
    report_readiness(base_url, verify_tls)
    seed_subscriber(base_url, args.admin_token, verify_tls)

    use_otp = otp_outbox_available(base_url, verify_tls)
    if use_otp:
        print("\n--- RCS auto-configuration via the SMS OTP flow (RCC.14 / OMA-CP) ---")
    else:
        print("\n--- RCS auto-configuration via a pre-issued token (RCC.14 / OMA-CP) ---")
        print("  the mock SMS outbox is not exposed, so the OTP cannot be read here")
        if not args.admin_token:
            print("  FAIL  an admin token is required to mint a provisioning token")
            return 1

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
        if use_otp:
            scenario_full(sim, checker, DEMO_MSISDN)
        else:
            scenario_token(sim, checker, mint_token(base_url, args.admin_token, verify_tls))

        # Harvest the OMA-DM credentials the ACS provisioned. A VERS-only answer
        # carries none, so force a fresh full document first.
        if args.admin_token:
            httpx.post(
                f"{base_url}/admin/subscribers/{DEMO_IMSI}/invalidate",
                headers={"Authorization": f"Bearer {args.admin_token}"},
                timeout=15,
                verify=verify_tls,
            )
        response = sim.request_configuration()
        if response.status_code == 200 and not response.content and use_otp:
            otp = sim.fetch_otp(DEMO_MSISDN)
            if otp:
                response = sim.request_configuration(OTP=otp)
        if response.content:
            dm_password = dm_password_from(sim, response.content)
    finally:
        sim.close()

    rcs_ok = checker.summary()

    dm_ok = True
    if not args.skip_dm:
        print("\n--- OMA-DM device management (SyncML DM 1.2) ---")
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
    print(f"RCS plane:    {'PASS' if rcs_ok else 'FAIL'}")
    print(f"OMA-DM plane: {'PASS' if dm_ok else 'FAIL' if not args.skip_dm else 'skipped'}")
    if rcs_ok and dm_ok:
        print("RESULT: PASS — both planes behave as specified")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
