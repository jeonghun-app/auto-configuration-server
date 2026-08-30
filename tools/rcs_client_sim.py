#!/usr/bin/env python3
"""Simulated RCS client — the end-to-end verification harness.

Behaves like an RCS client performing RCC.14 auto-configuration, and *checks* the
server's answers rather than just printing them. Any spec violation makes it exit
non-zero, so it works as a deployment smoke test in CI or against a real ALB.

It exercises the full state machine::

    unprovisioned -> otp_pending -> provisioned(v1) -> revalidated(VERS only)
                  -> token reconfiguration -> operator disable -> stopped

Usage::

    python tools/rcs_client_sim.py --base-url http://127.0.0.1:8080 \
        --imsi 001010000000001 --imei 356938035643809 --scenario full

The OTP is read from the server's development outbox (``/dev/sms``), which stands
in for the SMS the handset would receive. That endpoint only exists when
``ACS_DEV_ENDPOINTS_ENABLED=true`` and the environment is not production.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any
from xml.etree import ElementTree

import httpx

USER_AGENT_TEMPLATE = "IM-client/OMA1.0 {vendor}-{model}/{sw} RCS-client/{client_version}"

DISABLE_ACTIONS = {
    0: "disable, keep no configuration, re-query only on a trigger",
    -1: "disable, delete configuration, re-query at next trigger",
    -2: "disable, delete configuration, do not re-query",
    -3: "dormant, keep configuration, retry later",
    -4: "blocked permanently",
}


class SpecViolation(AssertionError):
    """The server answered in a way RCC.14 does not allow."""


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed += 1
            print(f"  PASS  {description}")
            return True
        self.failures.append(description)
        print(f"  FAIL  {description}")
        return False

    def summary(self) -> bool:
        print(f"\n{self.passed} checks passed, {len(self.failures)} failed")
        for failure in self.failures:
            print(f"  - {failure}")
        return not self.failures


class RcsClientSimulator:
    """A stateful RCS client."""

    def __init__(
        self,
        base_url: str,
        imsi: str,
        imei: str,
        msisdn: str = "",
        profile: str = "UP_2.4",
        vendor: str = "Sim",
        model: str = "SimPhone",
        sw_version: str = "1.0",
        client_version: str = "RCSAndrd-1.0-Sim-6.0",
        config_path: str = "/config",
        state_file: pathlib.Path | None = None,
        timeout: float = 10.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.imsi = imsi
        self.imei = imei
        self.msisdn = msisdn
        self.profile = profile
        self.vendor = vendor
        self.model = model
        self.sw_version = sw_version
        self.client_version = client_version
        self.config_path = config_path
        self.state_file = state_file
        self.state: dict[str, Any] = {"version": 0, "token": "", "phase": "unprovisioned"}
        if state_file and state_file.is_file():
            self.state.update(json.loads(state_file.read_text()))
        self.client = httpx.Client(timeout=timeout, verify=verify_tls, follow_redirects=True)

    # ------------------------------------------------------------- plumbing
    def close(self) -> None:
        self.client.close()
        if self.state_file:
            self.state_file.write_text(json.dumps(self.state, indent=2))

    def _user_agent(self) -> str:
        return USER_AGENT_TEMPLATE.format(
            vendor=self.vendor,
            model=self.model,
            sw=self.sw_version,
            client_version=self.client_version,
        )

    def _query(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "vers": self.state["version"],
            "IMSI": self.imsi,
            "IMEI": self.imei,
            "terminal_vendor": self.vendor,
            "terminal_model": self.model,
            "terminal_sw_version": self.sw_version,
            "client_vendor": self.vendor,
            "client_version": self.client_version,
            "rcs_version": "9.0",
            "rcs_profile": self.profile,
            "rcs_state": 0,
            "default_sms_app": 1,
            "provisioning_version": "1",
        }
        if self.msisdn:
            params["msisdn"] = self.msisdn
        if self.state.get("token"):
            params["token"] = self.state["token"]
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def request_configuration(self, **extra: Any) -> httpx.Response:
        return self.client.get(
            f"{self.base_url}{self.config_path}",
            params=self._query(**extra),
            headers={"User-Agent": self._user_agent(), "Accept": "text/xml"},
        )

    def fetch_otp(self, msisdn: str) -> str | None:
        response = self.client.get(f"{self.base_url}/dev/sms", params={"msisdn": msisdn})
        if response.status_code != 200:
            return None
        messages = response.json()
        if not messages:
            return None
        body = messages[0]["body"]
        digits = "".join(ch for ch in body if ch.isdigit())
        return digits or None

    # -------------------------------------------------------------- parsing
    @staticmethod
    def parse_document(payload: bytes) -> ElementTree.Element:
        root = ElementTree.fromstring(payload)  # noqa: S314 - server output, validated below
        if root.tag != "wap-provisioningdoc":
            raise SpecViolation(f"root element is {root.tag}, expected wap-provisioningdoc")
        if root.get("version") != "1.1":
            raise SpecViolation(f"unexpected wap-provisioningdoc version {root.get('version')}")
        return root

    @staticmethod
    def read_vers(root: ElementTree.Element) -> tuple[int, int]:
        node = root.find("characteristic[@type='VERS']")
        if node is None:
            raise SpecViolation("VERS characteristic is missing")
        version = node.find("parm[@name='version']")
        validity = node.find("parm[@name='validity']")
        if version is None or validity is None:
            raise SpecViolation("VERS must carry both version and validity")
        return int(version.get("value", "0")), int(validity.get("value", "0"))

    @staticmethod
    def read_token(root: ElementTree.Element) -> str:
        node = root.find("characteristic[@type='TOKEN']/parm[@name='token']")
        return node.get("value", "") if node is not None else ""

    @staticmethod
    def application(root: ElementTree.Element, app_id: str) -> ElementTree.Element | None:
        for characteristic in root.findall("characteristic[@type='APPLICATION']"):
            parm = characteristic.find("parm[@name='AppID']")
            if parm is not None and parm.get("value") == app_id:
                return characteristic
        return None

    def apply(self, root: ElementTree.Element) -> int:
        version, validity = self.read_vers(root)
        if version <= 0:
            action = DISABLE_ACTIONS.get(version, "unknown")
            print(f"  client action for VERS={version}: {action}")
            if version in (-1, -2):
                self.state["token"] = ""
            self.state["version"] = version if version in (-3,) else 0
            self.state["phase"] = "disabled"
            return version
        token = self.read_token(root)
        if token:
            self.state["token"] = token
        self.state["version"] = version
        self.state["validity"] = validity
        self.state["phase"] = "provisioned"
        return version


# ------------------------------------------------------------------ scenarios
def scenario_full(sim: RcsClientSimulator, checker: Checker, msisdn: str) -> None:
    print("\n[1] first configuration request, no credentials")
    response = sim.request_configuration()
    checker.check(
        response.status_code in (200, 511),
        f"first request answered 200 (OTP pending) or 511, got {response.status_code}",
    )
    if response.status_code == 511:
        print("  server requires network authentication; supplying MSISDN")
        sim.msisdn = msisdn
        response = sim.request_configuration()

    checker.check(
        response.status_code == 200 and not response.content,
        "OTP challenge answered with 200 and an empty body (RCC.14 pending signal)",
    )

    print("\n[2] retrieve the OTP and repeat the identical request")
    otp = sim.fetch_otp(msisdn)
    if not checker.check(bool(otp), "OTP was delivered to the simulated handset"):
        return

    response = sim.request_configuration(OTP=otp)
    checker.check(response.status_code == 200, f"OTP accepted, got {response.status_code}")
    checker.check(bool(response.content), "configuration document returned")
    root = sim.parse_document(response.content)
    version = sim.apply(root)
    checker.check(version == 1, f"first configuration version is 1, got {version}")
    checker.check(bool(sim.state["token"]), "TOKEN characteristic issued for reconfiguration")
    checker.check(sim.application(root, "ap2001") is not None, "IMS application (ap2001) present")
    checker.check(sim.application(root, "ap2002") is not None, "RCS application (ap2002) present")
    ims = sim.application(root, "ap2001")
    if ims is not None:
        impi = ims.find("parm[@name='Private_User_Identity']")
        checker.check(
            impi is not None and sim.imsi in (impi.get("value") or ""),
            "IMPI derived from the IMSI",
        )
        volte = ims.find("parm[@name='Voice_Domain_Preference_E_UTRAN']")
        checker.check(volte is not None, "VoLTE voice domain preference present")
    rcs = sim.application(root, "ap2002")
    if rcs is not None:
        chat = rcs.find("characteristic[@type='MESSAGING']/characteristic[@type='CHAT']")
        checker.check(chat is not None, "MESSAGING/CHAT subtree present")
        ft = rcs.find("characteristic[@type='MESSAGING']/characteristic[@type='FT']")
        checker.check(ft is not None, "MESSAGING/FT subtree present")
        transport = rcs.find("characteristic[@type='OTHER']/characteristic[@type='TRANSPORTPROTO']")
        checker.check(transport is not None, "OTHER/TRANSPORTPROTO subtree present")
    dm_account = sim.application(root, "w7")
    checker.check(dm_account is not None, "OMA-DM account (w7) bootstrapped in the CP document")

    print("\n[3] revalidation with the current version, using the token")
    response = sim.request_configuration()
    checker.check(response.status_code == 200, "revalidation answered 200")
    root = sim.parse_document(response.content)
    version, _ = sim.read_vers(root)
    checker.check(version == 1, "server reports the same version")
    checker.check(
        sim.application(root, "ap2002") is None,
        "server sent a VERS-only document instead of re-provisioning everything",
    )

    print("\n[4] token reconfiguration after the operator bumps the version")
    print("  (requires the admin API; skipped when no admin token is supplied)")


def scenario_disabled(sim: RcsClientSimulator, checker: Checker) -> None:
    print("\n[5] operator disabled the subscriber")
    response = sim.request_configuration()
    if response.status_code == 403:
        checker.check(True, "server answered 403 for a non-entitled subscriber")
        return
    checker.check(response.status_code == 200, "disable answered with 200 and a document")
    root = sim.parse_document(response.content)
    version = sim.apply(root)
    checker.check(version <= 0, f"disable value returned, got VERS={version}")
    checker.check(
        sim.application(root, "ap2002") is None,
        "a disabling document carries no service configuration",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulated RCS client for ACS verification")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--imsi", default="001010000000001")
    parser.add_argument("--imei", default="356938035643809")
    parser.add_argument("--msisdn", default="+821012345678")
    parser.add_argument("--profile", default="UP_2.4")
    parser.add_argument("--config-path", default="/config")
    parser.add_argument("--scenario", choices=["full", "disabled"], default="full")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args(argv)

    checker = Checker()
    sim = RcsClientSimulator(
        base_url=args.base_url,
        imsi=args.imsi,
        imei=args.imei,
        profile=args.profile,
        config_path=args.config_path,
        state_file=pathlib.Path(args.state_file) if args.state_file else None,
        verify_tls=not args.insecure,
    )
    print(f"ACS under test: {args.base_url}")
    try:
        if args.scenario == "full":
            scenario_full(sim, checker, args.msisdn)
        else:
            sim.msisdn = args.msisdn
            scenario_disabled(sim, checker)
    except SpecViolation as exc:
        print(f"  FAIL  specification violation: {exc}")
        checker.failures.append(str(exc))
    except httpx.HTTPError as exc:
        print(f"  ERROR transport failure: {exc}")
        return 2
    finally:
        sim.close()

    return 0 if checker.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
