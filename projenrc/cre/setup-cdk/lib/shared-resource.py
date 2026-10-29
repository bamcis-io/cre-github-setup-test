"""Custom resource for the account-wide resources every CRE project shares. Every create and update
makes them if missing and (re)applies what setup needs, idempotently. Delete does nothing: other
projects use them. The stack sends a hash of this file, so a change here runs an update everywhere.

Everything it applies is fixed here, not taken from the event: only Kind is read.
  ExecutionRole  CreCfnExecution: trusts only cloudformation.amazonaws.com, AdministratorAccess attached
  OidcProvider   token.actions.githubusercontent.com, with the sts.amazonaws.com audience
"""
import json

import boto3
import cfnresponse

iam = boto3.client("iam")
EXEC_ROLE = "CreCfnExecution"
OIDC_HOST = "token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
TAGS = [{"Key": "cre:managed-by", "Value": "cre-setup"}]
CFN_TRUST = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "cloudformation.amazonaws.com"}, "Action": "sts:AssumeRole"}]})


def execution_role(partition, account):
    try:
        arn = iam.get_role(RoleName=EXEC_ROLE)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=EXEC_ROLE, PolicyDocument=CFN_TRUST)
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=EXEC_ROLE, AssumeRolePolicyDocument=CFN_TRUST, Tags=TAGS)["Role"]["Arn"]
    iam.attach_role_policy(RoleName=EXEC_ROLE, PolicyArn=f"arn:{partition}:iam::aws:policy/AdministratorAccess")
    return arn


def oidc_provider(partition, account):
    arn = f"arn:{partition}:iam::{account}:oidc-provider/{OIDC_HOST}"
    try:
        iam.add_client_id_to_open_id_connect_provider(OpenIDConnectProviderArn=arn, ClientID=AUDIENCE)
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_open_id_connect_provider(Url=f"https://{OIDC_HOST}", ClientIDList=[AUDIENCE],
                                                  Tags=TAGS)["OpenIDConnectProviderArn"]
    return arn


KINDS = {"ExecutionRole": execution_role, "OidcProvider": oidc_provider}


def handler(event, context):
    physical = event.get("PhysicalResourceId") or event["LogicalResourceId"]
    try:
        data = {}
        if event["RequestType"] in ("Create", "Update"):
            arn = context.invoked_function_arn.split(":")   # arn:<partition>:lambda:<region>:<account>:...
            physical = KINDS[event["ResourceProperties"]["Kind"]](arn[1], arn[4])
            data = {"Arn": physical}
        cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical)
    except Exception as e:  # noqa: BLE001: CloudFormation must always get an answer
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical, reason=f"{type(e).__name__}: {e}")
