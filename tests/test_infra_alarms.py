"""The CloudWatch alarms in ``infra/app.yaml`` watch what the service emits.

A CloudWatch alarm sees a metric only when its namespace, name and complete
dimension set match exactly. EMF publishes each metric under the single dimension
set its document declares, so an alarm that names one dimension too few watches a
metric that never receives a data point and, with ``TreatMissingData:
notBreaching``, stays ``OK`` for ever. cfn-lint cannot see that, so these tests
drive the real request path, capture the EMF document, and compare it with the
alarm the template declares.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMSI, TEST_MSISDN, base_query
from tests.test_dm_session import DM_PASSWORD, PACKAGE_1_BODY, basic_cred, package

from acs.config import Settings
from acs.domain.models import Subscriber
from acs.domain.service import ProvisioningService
from acs.protocol.omadm import syncml, wbxml
from acs.sms.base import SmsDeliveryFailed, SmsRequest, SmsResult
from acs.store.base import OtpStoreContention
from acs.store.memory import MemoryStore

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "infra" / "app.yaml"

EXISTING_ALARMS = ("UnhealthyTargetsAlarm", "ServerErrorAlarm", "OtpVolumeAlarm")
NOTIFICATION_KEYS = ("AlarmActions", "OKActions", "InsufficientDataActions")
ANY_OCCURRENCE_METRICS = ("OtpDeliveryFailed", "DmEncodingError")


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
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def resources() -> dict[str, Any]:
    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506
    return dict(template["Resources"])


def alarm_on(resources: dict[str, Any], metric: str) -> dict[str, Any]:
    matches = [
        resource["Properties"]
        for resource in resources.values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
        and resource["Properties"].get("MetricName") == metric
    ]
    assert len(matches) == 1, f"expected exactly one alarm on {metric}, found {len(matches)}"
    return dict(matches[0])


def container_environment(resources: dict[str, Any]) -> dict[str, Any]:
    (container,) = resources["TaskDefinition"]["Properties"]["ContainerDefinitions"]
    return {entry["Name"]: entry["Value"] for entry in container["Environment"]}


class _FailingSender:
    name = "failing"

    def send(self, request: SmsRequest) -> SmsResult:
        raise SmsDeliveryFailed("smsc rejected the bind")


def _otp_delivery_fails(
    client: TestClient, settings: Settings, store: MemoryStore, _monkeypatch: pytest.MonkeyPatch
) -> int:
    state = client.app.state.acs  # type: ignore[attr-defined]
    state.provisioning = ProvisioningService(settings, store, _FailingSender())
    return client.get("/config", params=base_query()).status_code


def _otp_store_is_contended(
    client: TestClient, _settings: Settings, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> int:
    def contended(*_args: object, **_kwargs: object) -> None:
        raise OtpStoreContention("busy")

    monkeypatch.setattr(store, "issue_otp", contended)
    return client.get("/config", params=base_query()).status_code


def _dm_response_cannot_be_encoded(
    client: TestClient, _settings: Settings, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> int:
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, dm_password=DM_PASSWORD))
    wire = wbxml.encode(package(1, PACKAGE_1_BODY, cred=basic_cred()))
    monkeypatch.setattr(wbxml, "MAX_ENCODE_BYTES", 1)
    response = client.post("/dm", content=wire, headers={"Content-Type": syncml.CONTENT_TYPE_WBXML})
    return response.status_code


Trigger = Callable[[TestClient, Settings, MemoryStore, pytest.MonkeyPatch], int]

TRIGGERS: dict[str, tuple[Trigger, int]] = {
    "OtpDeliveryFailed": (_otp_delivery_fails, 503),
    "OtpStoreContention": (_otp_store_is_contended, 503),
    "DmEncodingError": (_dm_response_cannot_be_encoded, 500),
}


def emitted(
    metric: str,
    client: TestClient,
    settings: Settings,
    store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, dict[str, str], str]:
    """Return the namespace, dimensions and unit *metric* is published under."""
    trigger, status = TRIGGERS[metric]
    capsys.readouterr()
    assert trigger(client, settings, store, monkeypatch) == status
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith("{"):
            continue
        document = json.loads(line)
        for directive in document.get("_aws", {}).get("CloudWatchMetrics", []):
            for entry in directive["Metrics"]:
                if entry["Name"] != metric:
                    continue
                (dimension_set,) = directive["Dimensions"]
                dimensions = {key: document[key] for key in dimension_set}
                return directive["Namespace"], dimensions, entry.get("Unit", "None")
    raise AssertionError(f"no EMF document published {metric}")


@pytest.mark.parametrize("metric", sorted(TRIGGERS))
def test_the_alarm_matches_the_namespace_and_dimensions_the_metric_is_published_under(
    metric: str,
    resources: dict[str, Any],
    client: TestClient,
    settings: Settings,
    seeded_store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace, dimensions, _unit = emitted(
        metric, client, settings, seeded_store, monkeypatch, capsys
    )
    alarm = alarm_on(resources, metric)
    environment = container_environment(resources)

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


@pytest.mark.parametrize("metric", sorted(TRIGGERS))
def test_the_alarm_does_not_filter_on_a_unit_the_metric_is_not_published_in(
    metric: str,
    resources: dict[str, Any],
    client: TestClient,
    settings: Settings,
    seeded_store: MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An alarm that names a Unit sees only data points published in that unit,
    # so a mismatch leaves it silent exactly like a missing dimension.
    _namespace, _dimensions, unit = emitted(
        metric, client, settings, seeded_store, monkeypatch, capsys
    )
    assert alarm_on(resources, metric).get("Unit", unit) == unit


@pytest.mark.parametrize("metric", sorted(TRIGGERS))
def test_the_alarm_stays_ok_while_the_metric_is_silent(
    metric: str, resources: dict[str, Any]
) -> None:
    # The metric exists only once something fails; silence is the healthy state.
    assert alarm_on(resources, metric)["TreatMissingData"] == "notBreaching"


@pytest.mark.parametrize("metric", sorted(TRIGGERS))
def test_the_alarm_notifies_exactly_like_the_existing_alarms(
    metric: str, resources: dict[str, Any]
) -> None:
    alarm = alarm_on(resources, metric)
    for name in EXISTING_ALARMS:
        existing = resources[name]["Properties"]
        for key in NOTIFICATION_KEYS:
            assert alarm.get(key) == existing.get(key), f"{metric} {key} differs from {name}"


@pytest.mark.parametrize("metric", ANY_OCCURRENCE_METRICS)
def test_one_failure_breaches_the_alarm_and_it_clears_only_after_five_quiet_minutes(
    metric: str, resources: dict[str, Any]
) -> None:
    alarm = alarm_on(resources, metric)
    assert alarm["Statistic"] == "Sum"
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["Threshold"] == 0
    assert alarm.get("DatapointsToAlarm", alarm["EvaluationPeriods"]) == 1
    # 1 of 5: a persistent fault at low traffic holds ALARM rather than
    # flapping to OK between requests.
    assert alarm["Period"] * alarm["EvaluationPeriods"] == 300


def test_store_contention_alarms_on_a_sustained_rate_not_on_one_burst(
    resources: dict[str, Any],
) -> None:
    alarm = alarm_on(resources, "OtpStoreContention")
    datapoints = alarm.get("DatapointsToAlarm", alarm["EvaluationPeriods"])
    assert alarm["Statistic"] == "Sum"
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    # A burst for one MSISDN fits inside one period; it must not be enough.
    assert datapoints >= 2
    assert datapoints <= alarm["EvaluationPeriods"]
