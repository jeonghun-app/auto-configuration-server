"""The OTP volume alarm in ``infra/app.yaml`` watches the ``OtpSent`` the service emits.

A CloudWatch alarm sees a metric only when its namespace, name and complete
dimension set match exactly, and EMF publishes ``OtpSent`` under one dimension set
only. An alarm naming fewer dimensions watches a metric that never receives a data
point and, with ``TreatMissingData: notBreaching``, stays ``OK`` for ever — which
is how the SMS-pumping alarm shipped (#42). cfn-lint cannot see that, so this test
drives the real OTP path, captures the EMF document and compares it with the
template.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from tests.conftest import base_query

from acs.config import Settings

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "infra" / "app.yaml"


class _CfnLoader(yaml.SafeLoader):
    """A safe loader that reads CloudFormation short-form intrinsics as data."""


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        assert isinstance(node, yaml.MappingNode)
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def resources() -> dict[str, Any]:
    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506
    return dict(template["Resources"])


def emitted_otp_sent(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> tuple[str, dict[str, str]]:
    """Return the namespace and dimensions ``OtpSent`` is published under."""
    capsys.readouterr()
    assert client.get("/config", params=base_query()).status_code == 200
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith("{"):
            continue
        document = json.loads(line)
        for directive in document.get("_aws", {}).get("CloudWatchMetrics", []):
            if any(entry["Name"] == "OtpSent" for entry in directive["Metrics"]):
                (dimension_set,) = directive["Dimensions"]
                return directive["Namespace"], {key: document[key] for key in dimension_set}
    raise AssertionError("no EMF document published OtpSent")


def test_the_otp_volume_alarm_matches_the_dimensions_otp_sent_is_published_under(
    resources: dict[str, Any],
    client: TestClient,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace, dimensions = emitted_otp_sent(client, capsys)
    alarm = resources["OtpVolumeAlarm"]["Properties"]
    (container,) = resources["TaskDefinition"]["Properties"]["ContainerDefinitions"]
    environment = {entry["Name"]: entry["Value"] for entry in container["Environment"]}

    assert alarm["MetricName"] == "OtpSent"
    # The task's namespace and environment come from the template; the test
    # settings stand in for them, so pin that the template wires them through.
    assert environment["ACS_METRICS_NAMESPACE"] == alarm["Namespace"]
    assert namespace == settings.metrics_namespace
    assert environment["ACS_ENV"] == {"Ref": "Environment"}
    stack_values = {"Environment": settings.env}

    declared = {
        dimension["Name"]: (
            stack_values[dimension["Value"]["Ref"]]
            if isinstance(dimension["Value"], dict)
            else dimension["Value"]
        )
        for dimension in alarm.get("Dimensions", [])
    }
    assert declared == dimensions
