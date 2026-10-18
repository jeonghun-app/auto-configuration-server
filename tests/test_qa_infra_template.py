"""What ``infra/app.yaml`` resolves to for each FixedEgress, SmsProvider and SmppTls value.

``test_infra_template.py`` checks the shape of the new resources. These resolve the
conditions, rules and ``Fn::If`` branches for concrete parameter values, the way
CloudFormation would, so a wrong ``Fn::Select`` index, a route to the wrong NAT
gateway or a missing endpoint attachment fails here. cfn-lint accepts all of those:
it validates the template, not what a given set of parameters makes of it.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from cfnlint.decode import cfn_yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE: dict[str, Any] = json.loads(json.dumps(cfn_yaml.load(str(ROOT / "infra" / "app.yaml"))))
CA_ARN = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rcs-acs/smsc-ca-AbC12z"
FOUR_RANGES = "192.0.2.0/28,192.0.2.16/28,198.51.100.0/28,203.0.113.0/28"
NO_VALUE = {"Ref": "AWS::NoValue"}


class Stack:
    """The template resolved for one set of parameter values.

    Only the intrinsics the template uses on parameters are evaluated. A ``Ref`` to
    a resource stays ``{"Ref": name}``, so the result says which resource is wired
    where without needing AWS.
    """

    def __init__(self, **overrides: str) -> None:
        self.params = {
            name: str(spec.get("Default", "")) for name, spec in TEMPLATE["Parameters"].items()
        }
        self.params.update(ImageUri="x", AllowedCidr="198.51.100.0/24", **overrides)
        self.conditions: dict[str, bool] = {}
        for name, expr in TEMPLATE["Conditions"].items():
            self.conditions[name] = bool(self.resolve(expr))
        self.resources = {
            name: self.resolve(spec)
            for name, spec in TEMPLATE["Resources"].items()
            if self.conditions.get(spec.get("Condition", ""), True)
        }

    def resolve(self, node: Any) -> Any:
        if isinstance(node, list):
            return [v for v in (self.resolve(item) for item in node) if v != NO_VALUE]
        if not isinstance(node, dict):
            return node
        if len(node) == 1:
            ((fn, arg),) = node.items()
            if fn == "Ref":
                return self.params.get(arg, node)
            if fn == "Condition":
                return self.conditions[arg]
            if fn == "Fn::If":
                return self.resolve(arg[1] if self.conditions[arg[0]] else arg[2])
            if fn == "Fn::Equals":
                return self.resolve(arg[0]) == self.resolve(arg[1])
            if fn == "Fn::Not":
                return not self.resolve(arg[0])
            if fn == "Fn::And":
                return all(self.resolve(item) for item in arg)
            if fn == "Fn::Or":
                return any(self.resolve(item) for item in arg)
            if fn in ("Fn::Join", "Fn::Split", "Fn::Select"):
                return self._string_function(fn, arg)
        return {
            key: v for key, v in ((k, self.resolve(v)) for k, v in node.items()) if v != NO_VALUE
        }

    def _string_function(self, fn: str, arg: list[Any]) -> Any:
        resolved = self.resolve(arg[1])
        if fn == "Fn::Join":
            if all(isinstance(part, str) for part in resolved):
                return arg[0].join(resolved)
            # A join over resource references: keep the references, in order.
            return {"Fn::Join": [arg[0], resolved]}
        if fn == "Fn::Split":
            if isinstance(resolved, str):
                return resolved.split(arg[0])
            return {"Fn::Split": [arg[0], resolved]}
        if isinstance(resolved, list):
            return resolved[int(arg[0])]
        if isinstance(resolved, dict) and "Fn::Split" in resolved:
            # Select over Split over Join of references: the reference at that index.
            return resolved["Fn::Split"][1]["Fn::Join"][1][int(arg[0])]
        return {"Fn::Select": [arg[0], resolved]}

    def failed_rules(self) -> list[str]:
        failed = []
        for rule in TEMPLATE["Rules"].values():
            if "RuleCondition" in rule and not self.resolve(rule["RuleCondition"]):
                continue
            failed += [
                a["AssertDescription"] for a in rule["Assertions"] if not self.resolve(a["Assert"])
            ]
        return failed

    def of_type(self, kind: str) -> dict[str, Any]:
        return {n: r for n, r in self.resources.items() if r["Type"] == kind}

    def props(self, name: str) -> Any:
        return self.resources[name]["Properties"]


def smpp(**overrides: str) -> Stack:
    values = {
        "SmsProvider": "smpp",
        "SmppHost": "smsc.example.net",
        "SmppSystemId": "acs",
        "SmppSmscCidr": "198.51.100.0/28",
    }
    values.update(overrides)
    return Stack(**values)


# --------------------------------------------------------------- SMSC ranges
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_each_smsc_range_opens_the_smpp_port_to_that_range_only(count: int) -> None:
    ranges = FOUR_RANGES.split(",")[:count]
    stack = smpp(SmppSmscCidr=",".join(ranges), SmppPort="3550")
    rules = stack.of_type("AWS::EC2::SecurityGroupEgress").values()
    opened = [(r["Properties"]["CidrIp"], r["Properties"]["FromPort"]) for r in rules]
    assert sorted(opened) == sorted((cidr, "3550") for cidr in ranges)


def test_a_stack_without_smpp_opens_no_smsc_range_whatever_the_parameter_holds() -> None:
    stack = Stack(SmsProvider="eum", SmppSmscCidr=FOUR_RANGES)
    assert stack.of_type("AWS::EC2::SecurityGroupEgress") == {}


def test_smpp_without_a_range_is_refused_by_a_rule() -> None:
    assert "SmsProvider=smpp requires SmppSmscCidr." in smpp(SmppSmscCidr="").failed_rules()


# -------------------------------------------------------------- fixed egress
@pytest.mark.parametrize("provider", ["eum", "sns", "smpp"])
def test_no_fixed_egress_leaves_the_app_subnets_public(provider: str) -> None:
    stack = smpp(SmsProvider=provider)
    assert stack.of_type("AWS::EC2::NatGateway") == {}
    assert stack.of_type("AWS::EC2::EIP") == {}
    for subnet in ("A", "B"):
        assert stack.props(f"RouteAssociationApp{subnet}")["RouteTableId"] == {"Ref": "RouteTable"}
    awsvpc = stack.props("Service")["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "ENABLED"
    assert "S3Endpoint" not in stack.resources


@pytest.mark.parametrize(
    ("mode", "nat_for_b"), [("single", "NatGatewayA"), ("per-az", "NatGatewayB")]
)
def test_fixed_egress_routes_each_app_subnet_through_its_nat_gateway(
    mode: str, nat_for_b: str
) -> None:
    stack = smpp(FixedEgress=mode)
    assert stack.props("NatGatewayA")["SubnetId"] == {"Ref": "PublicSubnetA"}
    assert stack.props("PrivateDefaultRouteA")["NatGatewayId"] == {"Ref": "NatGatewayA"}
    assert stack.props("PrivateDefaultRouteB")["NatGatewayId"] == {"Ref": nat_for_b}
    if mode == "per-az":
        assert stack.props("NatGatewayB")["SubnetId"] == {"Ref": "PublicSubnetB"}
    for subnet in ("A", "B"):
        association = stack.props(f"RouteAssociationApp{subnet}")
        assert association["SubnetId"] == {"Ref": f"AppSubnet{subnet}"}
        assert association["RouteTableId"] == {"Ref": f"PrivateRouteTable{subnet}"}
        # The move must wait for the NAT route, so the route is referenced too.
        assert f"PrivateDefaultRoute{subnet}" in json.dumps(
            TEMPLATE["Resources"][f"RouteAssociationApp{subnet}"]
        )
    awsvpc = stack.props("Service")["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "DISABLED"


@pytest.mark.parametrize("mode", ["single", "per-az"])
def test_both_private_route_tables_reach_s3_and_dynamodb_without_the_nat_gateway(
    mode: str,
) -> None:
    stack = Stack(FixedEgress=mode)
    private = [{"Ref": "PrivateRouteTableA"}, {"Ref": "PrivateRouteTableB"}]
    for endpoint in ("S3Endpoint", "DynamoDbEndpoint"):
        tables = stack.props(endpoint)["RouteTableIds"]
        assert all(table in tables for table in private), endpoint


@pytest.mark.parametrize(
    ("mode", "addresses"),
    [("single", ["EgressIpA"]), ("per-az", ["EgressIpA", "EgressIpB"])],
)
def test_the_egress_output_lists_every_elastic_ip_the_tasks_leave_from(
    mode: str, addresses: list[str]
) -> None:
    stack = Stack(FixedEgress=mode)
    assert sorted(stack.of_type("AWS::EC2::EIP")) == addresses
    value = stack.resolve(TEMPLATE["Outputs"]["EgressIps"]["Value"])
    referenced = [value] if "Ref" in value else value["Fn::Join"][1]
    assert referenced == [{"Ref": address} for address in addresses]


# ----------------------------------------------------------------- CA bundle
@pytest.mark.parametrize(
    ("mode", "provider", "tls"),
    list(
        itertools.product(["none", "single", "per-az"], ["eum", "sns", "smpp"], ["true", "false"])
    ),
)
def test_the_ca_secret_is_accepted_only_for_smpp_over_tls(
    mode: str, provider: str, tls: str
) -> None:
    stack = smpp(FixedEgress=mode, SmsProvider=provider, SmppTls=tls, SmppTlsCaSecretArn=CA_ARN)
    accepted = not stack.failed_rules()
    assert accepted == (provider == "smpp" and tls == "true")
    if accepted:
        secrets = stack.props("TaskDefinition")["ContainerDefinitions"][0]["Secrets"]
        assert {"Name": "ACS_SMPP_TLS_CA_PEM", "ValueFrom": CA_ARN} in secrets
        [statement] = stack.props("ExecutionRole")["Policies"][0]["PolicyDocument"]["Statement"]
        assert CA_ARN in statement["Resource"]


def test_without_a_ca_secret_neither_the_task_nor_the_role_names_one() -> None:
    stack = smpp()
    secrets = stack.props("TaskDefinition")["ContainerDefinitions"][0]["Secrets"]
    assert "ACS_SMPP_TLS_CA_PEM" not in json.dumps(secrets)
    [statement] = stack.props("ExecutionRole")["Policies"][0]["PolicyDocument"]["Statement"]
    assert "" not in statement["Resource"]
