#!/usr/bin/env python3
"""Simulated OMA-DM client — verifies the device management plane.

Runs a complete SyncML DM 1.2 session against the ACS and checks the server's
behaviour: authentication challenge handling, ``Status`` codes, the ``Get``
inventory request, and the ``Replace`` commands that carry the VoLTE / IMS
configuration.

Usage::

    python tools/dm_client_sim.py --base-url http://127.0.0.1:8080 \
        --imsi 001010000000001 --imei 356938035643809 --password <dm password>

The DM password is the one the ACS provisioned in the OMA-CP ``w7``
characteristic, so a realistic run does RCS provisioning first, reads
``AAUTHSECRET`` from the document, and passes it here — exactly what a handset
does.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from typing import Any
from xml.etree import ElementTree

import httpx

DM_CONTENT_TYPE = "application/vnd.syncml.dm+xml"
SYNCML_NS = "SYNCML:SYNCML1.2"
METINF_NS = "syncml:metinf"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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


class DmClientSimulator:
    """A minimal but protocol-correct OMA-DM client."""

    def __init__(
        self,
        base_url: str,
        imsi: str,
        imei: str,
        password: str,
        dm_path: str = "/dm",
        auth: str = "basic",
        manufacturer: str = "SimCorp",
        model: str = "SimPhone",
        sw_version: str = "SIM-1.0",
        timeout: float = 10.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.dm_path = dm_path
        self.imsi = imsi
        self.imei = imei
        self.password = password
        self.auth = auth
        self.manufacturer = manufacturer
        self.model = model
        self.sw_version = sw_version
        self.session_id = "42"
        self.msg_id = 0
        self.nonce = ""
        self.client = httpx.Client(timeout=timeout, verify=verify_tls)
        self.node_values: dict[str, str] = {
            "./DevInfo/DevId": f"IMEI:{imei}",
            "./DevInfo/Man": manufacturer,
            "./DevInfo/Mod": model,
            "./DevInfo/DmV": "1.2",
            "./DevInfo/Lang": "en",
            "./DevDetail/DevTyp": "phone",
            "./DevDetail/OEM": manufacturer,
            "./DevDetail/FwV": sw_version,
            "./DevDetail/SwV": sw_version,
            "./DevDetail/HwV": "rev1",
            "./DevDetail/LrgObj": "true",
            "./DevDetail/URI/MaxDepth": "16",
            "./DevDetail/URI/MaxTotLen": "512",
            "./DevDetail/URI/MaxSegLen": "64",
        }
        self.received: dict[str, str] = {}

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------ credentials
    def _credential(self) -> tuple[str, str]:
        if self.auth == "md5":
            inner = hashlib.md5(f"{self.imsi}:{self.password}".encode()).digest()  # noqa: S324
            digest = hashlib.md5(  # noqa: S324
                base64.b64encode(inner) + b":" + self.nonce.encode()
            ).digest()
            return "syncml:auth-md5", base64.b64encode(digest).decode()
        raw = base64.b64encode(f"{self.imsi}:{self.password}".encode()).decode()
        return "syncml:auth-basic", raw

    # -------------------------------------------------------------- packaging
    def _envelope(self, with_credentials: bool = True) -> tuple[Any, Any]:
        self.msg_id += 1
        root = ElementTree.Element("SyncML", {"xmlns": SYNCML_NS})
        header = ElementTree.SubElement(root, "SyncHdr")
        ElementTree.SubElement(header, "VerDTD").text = "1.2"
        ElementTree.SubElement(header, "VerProto").text = "DM/1.2"
        ElementTree.SubElement(header, "SessionID").text = self.session_id
        ElementTree.SubElement(header, "MsgID").text = str(self.msg_id)
        target = ElementTree.SubElement(header, "Target")
        ElementTree.SubElement(target, "LocURI").text = f"{self.base_url}{self.dm_path}"
        source = ElementTree.SubElement(header, "Source")
        ElementTree.SubElement(source, "LocURI").text = f"IMEI:{self.imei}"
        ElementTree.SubElement(source, "LocName").text = self.imsi
        if with_credentials:
            cred = ElementTree.SubElement(header, "Cred")
            meta = ElementTree.SubElement(cred, "Meta")
            ElementTree.SubElement(meta, "Format", {"xmlns": METINF_NS}).text = "b64"
            auth_type, data = self._credential()
            ElementTree.SubElement(meta, "Type", {"xmlns": METINF_NS}).text = auth_type
            ElementTree.SubElement(cred, "Data").text = data
        meta = ElementTree.SubElement(header, "Meta")
        ElementTree.SubElement(meta, "MaxMsgSize", {"xmlns": METINF_NS}).text = "16384"
        body = ElementTree.SubElement(root, "SyncBody")
        return root, body

    @staticmethod
    def _add_alert(body: Any, cmd_id: int, code: str) -> None:
        alert = ElementTree.SubElement(body, "Alert")
        ElementTree.SubElement(alert, "CmdID").text = str(cmd_id)
        ElementTree.SubElement(alert, "Data").text = code

    def _add_replace(self, body: Any, cmd_id: int, uris: list[str]) -> None:
        replace = ElementTree.SubElement(body, "Replace")
        ElementTree.SubElement(replace, "CmdID").text = str(cmd_id)
        for uri in uris:
            item = ElementTree.SubElement(replace, "Item")
            source = ElementTree.SubElement(item, "Source")
            ElementTree.SubElement(source, "LocURI").text = uri
            meta = ElementTree.SubElement(item, "Meta")
            ElementTree.SubElement(meta, "Format", {"xmlns": METINF_NS}).text = "chr"
            ElementTree.SubElement(meta, "Type", {"xmlns": METINF_NS}).text = "text/plain"
            ElementTree.SubElement(item, "Data").text = self.node_values.get(uri, "")

    def _add_results(self, body: Any, cmd_id: int, cmd_ref: str, uris: list[str]) -> None:
        results = ElementTree.SubElement(body, "Results")
        ElementTree.SubElement(results, "CmdID").text = str(cmd_id)
        ElementTree.SubElement(results, "MsgRef").text = str(self.msg_id - 1)
        ElementTree.SubElement(results, "CmdRef").text = cmd_ref
        for uri in uris:
            item = ElementTree.SubElement(results, "Item")
            source = ElementTree.SubElement(item, "Source")
            ElementTree.SubElement(source, "LocURI").text = uri
            ElementTree.SubElement(item, "Data").text = self.node_values.get(uri, "")

    @staticmethod
    def _add_status(body: Any, cmd_id: int, msg_ref: str, cmd_ref: str, cmd: str) -> None:
        status = ElementTree.SubElement(body, "Status")
        ElementTree.SubElement(status, "CmdID").text = str(cmd_id)
        ElementTree.SubElement(status, "MsgRef").text = msg_ref
        ElementTree.SubElement(status, "CmdRef").text = cmd_ref
        ElementTree.SubElement(status, "Cmd").text = cmd
        ElementTree.SubElement(status, "Data").text = "200"

    @staticmethod
    def _serialise(root: Any) -> bytes:
        return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)

    def post(self, payload: bytes) -> httpx.Response:
        return self.client.post(
            f"{self.base_url}{self.dm_path}",
            content=payload,
            headers={"Content-Type": DM_CONTENT_TYPE, "Accept": DM_CONTENT_TYPE},
        )

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def parse(payload: bytes) -> dict[str, Any]:
        root = ElementTree.fromstring(payload)  # noqa: S314
        out: dict[str, Any] = {
            "statuses": [],
            "gets": [],
            "replaces": {},
            "final": False,
            "challenge": "",
        }
        for element in root.iter():
            name = _local(element.tag)
            if name == "Final":
                out["final"] = True
            elif name == "Status":
                data = ""
                cmd = ""
                chal = False
                for child in element:
                    child_name = _local(child.tag)
                    if child_name == "Data":
                        data = (child.text or "").strip()
                    elif child_name == "Cmd":
                        cmd = (child.text or "").strip()
                    elif child_name == "Chal":
                        chal = True
                        for sub in child.iter():
                            if _local(sub.tag) == "NextNonce":
                                out["challenge"] = (sub.text or "").strip()
                out["statuses"].append({"cmd": cmd, "code": data, "chal": chal})
            elif name == "Get":
                for item in element:
                    if _local(item.tag) != "Item":
                        continue
                    for sub in item.iter():
                        if _local(sub.tag) == "LocURI":
                            out["gets"].append((sub.text or "").strip())
            elif name == "Replace":
                cmd_ref = ""
                for child in element:
                    if _local(child.tag) == "CmdID":
                        cmd_ref = (child.text or "").strip()
                    if _local(child.tag) != "Item":
                        continue
                    uri = ""
                    data = ""
                    for sub in child.iter():
                        sub_name = _local(sub.tag)
                        if sub_name == "LocURI":
                            uri = (sub.text or "").strip()
                        elif sub_name == "Data":
                            data = (sub.text or "").strip()
                    if uri:
                        out["replaces"][uri] = data
                out["replace_cmd_ref"] = cmd_ref
        return out


def run_session(sim: DmClientSimulator, checker: Checker) -> None:
    print("\n[1] package 1: client initiated management session")
    root, body = sim._envelope()
    sim._add_alert(body, 1, "1201")
    sim._add_replace(
        body,
        2,
        ["./DevInfo/DevId", "./DevInfo/Man", "./DevInfo/Mod", "./DevInfo/DmV", "./DevInfo/Lang"],
    )
    ElementTree.SubElement(body, "Final")
    response = sim.post(sim._serialise(root))
    checker.check(response.status_code == 200, f"server answered 200, got {response.status_code}")
    parsed = sim.parse(response.content)

    if parsed["challenge"] or any(s["code"] == "401" for s in parsed["statuses"]):
        print("  server issued an authentication challenge; retrying with credentials")
        sim.nonce = parsed["challenge"]
        sim.auth = "md5" if sim.nonce else sim.auth
        root, body = sim._envelope()
        sim._add_alert(body, 1, "1201")
        sim._add_replace(body, 2, ["./DevInfo/DevId", "./DevInfo/Man", "./DevInfo/Mod"])
        ElementTree.SubElement(body, "Final")
        response = sim.post(sim._serialise(root))
        parsed = sim.parse(response.content)

    hdr_status = next((s for s in parsed["statuses"] if s["cmd"] == "SyncHdr"), None)
    checker.check(
        hdr_status is not None and hdr_status["code"] in ("200", "212"),
        f"SyncHdr acknowledged with 200/212, got {hdr_status['code'] if hdr_status else 'none'}",
    )
    checker.check(bool(parsed["gets"]), "server requested the device inventory with Get")
    checker.check(parsed["final"], "server package ended with Final")

    print("\n[2] package 3: results for the inventory Get")
    requested = parsed["gets"]
    root, body = sim._envelope()
    sim._add_status(body, 1, str(sim.msg_id - 1), "0", "SyncHdr")
    sim._add_results(body, 2, "2", requested)
    ElementTree.SubElement(body, "Final")
    response = sim.post(sim._serialise(root))
    checker.check(response.status_code == 200, "server answered 200 to the results package")
    parsed = sim.parse(response.content)
    sim.received = parsed["replaces"]
    checker.check(bool(sim.received), "server pushed configuration with Replace")

    ims_nodes = [uri for uri in sim.received if uri.startswith("./3GPP_IMS/")]
    checker.check(bool(ims_nodes), "3GPP IMS management object nodes pushed")
    checker.check(
        "./3GPP_IMS/1/Voice_Domain_Preference_E_UTRAN" in sim.received,
        "VoLTE voice domain preference pushed over OMA-DM",
    )
    checker.check(
        "./3GPP_IMS/1/SMS_Over_IP_Networks_Indication" in sim.received,
        "SMSoIP indication pushed over OMA-DM",
    )
    impi = sim.received.get("./3GPP_IMS/1/Private_User_Identity", "")
    checker.check(sim.imsi in impi, "IMPI in the DM push derives from the IMSI")
    checker.check(
        any(uri.startswith("./RCS/") for uri in sim.received),
        "RCS extension management object nodes pushed",
    )

    print("\n[3] package 5: statuses for the Replace, ending the session")
    root, body = sim._envelope()
    sim._add_status(body, 1, str(sim.msg_id - 1), "0", "SyncHdr")
    sim._add_status(body, 2, str(sim.msg_id - 1), parsed.get("replace_cmd_ref", "1"), "Replace")
    ElementTree.SubElement(body, "Final")
    response = sim.post(sim._serialise(root))
    checker.check(response.status_code == 200, "server answered 200 to the final package")
    parsed = sim.parse(response.content)
    checker.check(parsed["final"], "server closed the session with Final and no new commands")
    checker.check(not parsed["gets"], "no further commands were issued")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulated OMA-DM client for ACS verification")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--dm-path", default="/dm")
    parser.add_argument("--imsi", default="001010000000001")
    parser.add_argument("--imei", default="356938035643809")
    parser.add_argument("--password", required=True, help="DM password from the w7 characteristic")
    parser.add_argument("--auth", choices=["basic", "md5", "none"], default="basic")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args(argv)

    checker = Checker()
    sim = DmClientSimulator(
        base_url=args.base_url,
        dm_path=args.dm_path,
        imsi=args.imsi,
        imei=args.imei,
        password=args.password,
        auth=args.auth,
        verify_tls=not args.insecure,
    )
    print(f"OMA-DM endpoint under test: {args.base_url}{args.dm_path}")
    try:
        run_session(sim, checker)
    except httpx.HTTPError as exc:
        print(f"  ERROR transport failure: {exc}")
        return 2
    finally:
        sim.close()
    return 0 if checker.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
