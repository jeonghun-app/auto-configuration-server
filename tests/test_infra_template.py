"""Properties of ``infra/app.yaml`` and ``scripts/deploy.sh`` that cfn-lint cannot see.

cfn-lint checks that the template is valid CloudFormation. These check that it
does what the deployment documentation promises: fixed egress costs nothing until
it is asked for, the SMSC CA bundle is readable from exactly one secret, every
SMPP setting reaches the task with the application's own default, and
``deploy.sh`` (which passes every parameter on every run) cannot drift from the
template it deploys.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from cfnlint.decode import cfn_yaml

from acs.config import Settings

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "infra" / "app.yaml"
DEPLOY_SH = (ROOT / "scripts" / "deploy.sh").read_text()

# The SMSC address ranges the template opens SmppPort to. A CloudFormation
# template cannot loop or count a list without a transform, so it has this many
# fixed egress rules and the parameter pattern refuses more entries than that.
SMSC_CIDR_SLOTS = 4


def _plain(node: Any) -> Any:
    # cfn-lint's nodes carry line marks; plain dicts compare and print cleanly.
    return json.loads(json.dumps(node))


TEMPLATE: dict[str, Any] = _plain(cfn_yaml.load(str(TEMPLATE_PATH)))
PARAMETERS: dict[str, Any] = TEMPLATE["Parameters"]
RESOURCES: dict[str, Any] = TEMPLATE["Resources"]
CONTAINER: dict[str, Any] = RESOURCES["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]


def _deploy_params() -> dict[str, str]:
    """Map each stack parameter deploy.sh passes to the shell variable it reads."""
    block = DEPLOY_SH[DEPLOY_SH.index("PARAMS=(") : DEPLOY_SH.index("\n)\n")]
    return dict(re.findall(r'"(\w+)=\$\{(\w+)\}"', block))


def _shell_default(variable: str) -> str | None:
    match = re.search(rf'^{variable}="([^"$]*)"$', DEPLOY_SH, re.MULTILINE)
    return None if match is None else match.group(1)


# ------------------------------------------------------------ fixed egress
def test_fixed_egress_is_off_by_default() -> None:
    assert PARAMETERS["FixedEgress"]["Default"] == "none"


def test_every_nat_resource_exists_only_when_fixed_egress_is_asked_for() -> None:
    # An existing stack updated with the default must gain no hourly charge.
    billable = {"AWS::EC2::EIP", "AWS::EC2::NatGateway"}
    nat_plumbing = {"AWS::EC2::RouteTable", "AWS::EC2::Route", "AWS::EC2::VPCEndpoint"}
    pre_existing = {"RouteTable", "DefaultRoute", "DynamoDbEndpoint"}
    found = {
        name: resource.get("Condition")
        for name, resource in RESOURCES.items()
        if resource["Type"] in billable
        or (resource["Type"] in nat_plumbing and name not in pre_existing)
    }
    assert {"EgressIpA", "NatGatewayA", "EgressIpB", "NatGatewayB"} <= found.keys()
    assert set(found.values()) <= {"UseFixedEgress", "NatPerAz"}, found


def test_tasks_keep_their_public_ip_unless_egress_is_fixed() -> None:
    awsvpc = RESOURCES["Service"]["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == {"Fn::If": ["UseFixedEgress", "DISABLED", "ENABLED"]}


def test_the_service_waits_for_the_app_subnets_to_be_rerouted() -> None:
    # Otherwise tasks without a public IP can start in a subnet that still routes to
    # the internet gateway, fail to pull their image, and trip the circuit breaker.
    depends_on = RESOURCES["Service"]["DependsOn"]
    assert {"RouteAssociationAppA", "RouteAssociationAppB"} <= set(depends_on)


def test_the_egress_addresses_are_an_output_for_the_smsc_allow_list() -> None:
    output = TEMPLATE["Outputs"]["EgressIps"]
    assert output["Condition"] == "UseFixedEgress"


# --------------------------------------------------------------- SMSC ranges
SMSC_CIDR_PATTERN = re.compile(PARAMETERS["SmppSmscCidr"]["AllowedPattern"])


@pytest.mark.parametrize(
    "value",
    [
        "",
        "198.51.100.0/28",  # the single range 1.4.0 accepted keeps working
        "198.51.100.0/28,203.0.113.8/29",
        "192.0.2.1/32,192.0.2.2/32,192.0.2.3/32,192.0.2.4/32",
    ],
)
def test_the_smsc_range_parameter_admits_up_to_the_egress_slots(value: str) -> None:
    assert SMSC_CIDR_PATTERN.fullmatch(value)


@pytest.mark.parametrize(
    "value",
    [
        "192.0.2.1/32,192.0.2.2/32,192.0.2.3/32,192.0.2.4/32,192.0.2.5/32",
        "198.51.100.0/28, 203.0.113.8/29",
        "198.51.100.0/28,,203.0.113.8/29",
        "198.51.100.0/28,",
        "198.51.100.0",
    ],
)
def test_the_smsc_range_parameter_refuses_what_would_be_silently_dropped(value: str) -> None:
    assert not SMSC_CIDR_PATTERN.fullmatch(value)


def test_there_is_one_smsc_egress_rule_per_admitted_range() -> None:
    rules = [r for r in RESOURCES.values() if r["Type"] == "AWS::EC2::SecurityGroupEgress"]
    assert len(rules) == SMSC_CIDR_SLOTS
    # The first keeps its 1.4.0 logical ID, so an existing stack does not replace it.
    assert RESOURCES["TaskEgressToSmsc"]["Condition"] == "UseSmpp"


# ---------------------------------------------------------------- CA bundle
def test_the_execution_role_reads_only_the_one_ca_secret_and_only_when_it_is_set() -> None:
    statements = RESOURCES["ExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]
    [statement] = statements["Statement"]
    assert statement["Action"] == ["secretsmanager:GetSecretValue"]
    assert {
        "Fn::If": ["HasSmppTlsCa", {"Ref": "SmppTlsCaSecretArn"}, {"Ref": "AWS::NoValue"}]
    } in statement["Resource"]
    assert "*" not in json.dumps(statement["Resource"])


def test_the_ca_bundle_reaches_the_task_as_a_secret_not_an_environment_value() -> None:
    assert {
        "Fn::If": [
            "HasSmppTlsCa",
            {"Name": "ACS_SMPP_TLS_CA_PEM", "ValueFrom": {"Ref": "SmppTlsCaSecretArn"}},
            {"Ref": "AWS::NoValue"},
        ]
    } in CONTAINER["Secrets"]
    assert "ACS_SMPP_TLS_CA_PEM" not in json.dumps(CONTAINER["Environment"])


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rcs-acs/smsc-ca",
        "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rcs-acs/smsc-ca-AbC12z:ca::",
        "rcs-acs/smsc-ca",
    ],
)
def test_the_ca_secret_must_be_named_by_its_complete_arn(arn: str) -> None:
    # The IAM grant is on this exact value. A partial ARN (no random suffix) or a
    # JSON-key reference would be accepted by ECS but match no resource in IAM,
    # so every task would fail to start.
    assert not re.fullmatch(PARAMETERS["SmppTlsCaSecretArn"]["AllowedPattern"], arn)


@pytest.mark.parametrize(
    "arn",
    ["", "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rcs-acs/smsc-ca-AbC12z"],
)
def test_the_ca_secret_is_optional_or_a_complete_arn(arn: str) -> None:
    assert re.fullmatch(PARAMETERS["SmppTlsCaSecretArn"]["AllowedPattern"], arn)


# ------------------------------------------------------- SMPP settings parity
_ENVIRONMENT = {item["Name"]: item["Value"] for item in CONTAINER["Environment"]}
# A file path cannot be used on the read-only root filesystem; the bundle arrives
# as PEM text instead. The two secrets arrive through Secrets.
_NOT_IN_ENVIRONMENT = {"smpp_password", "smpp_tls_ca_pem", "smpp_tls_ca_file"}
SMPP_FIELDS = sorted(
    name
    for name in Settings.model_fields
    if name.startswith("smpp_") and name not in _NOT_IN_ENVIRONMENT
)


@pytest.mark.parametrize("field", SMPP_FIELDS)
def test_every_smpp_setting_is_a_stack_parameter_with_the_application_default(
    field: str,
) -> None:
    value = _ENVIRONMENT[f"ACS_{field.upper()}"]
    assert set(value) == {"Ref"}, f"{field} is not taken from a parameter"
    parameter = PARAMETERS[value["Ref"]]
    default = Settings.model_fields[field].default
    # Compared as the application would parse the environment string.
    if isinstance(default, bool):
        assert parameter["Default"] == str(default).lower()
    elif isinstance(default, int | float):
        assert float(parameter["Default"]) == default
    else:
        assert parameter["Default"] == default


@pytest.mark.parametrize("field", SMPP_FIELDS)
def test_deploy_sh_passes_every_smpp_parameter(field: str) -> None:
    parameter = _ENVIRONMENT[f"ACS_{field.upper()}"]["Ref"]
    assert parameter in _deploy_params()


# ------------------------------------------------------------ deploy.sh parity
def test_every_parameter_deploy_sh_passes_is_declared_by_the_template() -> None:
    assert _deploy_params().keys() <= PARAMETERS.keys()


def test_deploy_sh_defaults_equal_the_template_defaults() -> None:
    # deploy.sh passes every parameter on every run, so a default that differs from
    # the template's changes a stack that was deployed without the flag.
    for parameter, variable in _deploy_params().items():
        shell_default = _shell_default(variable)
        if shell_default is None or "Default" not in PARAMETERS[parameter]:
            continue
        assert shell_default == str(PARAMETERS[parameter]["Default"]), parameter


def test_every_parameter_deploy_sh_may_omit_has_a_default() -> None:
    required = {name for name, spec in PARAMETERS.items() if "Default" not in spec}
    assert required == {"ImageUri", "AllowedCidr"}
