"""One-time bootstrap via existing local AWS credentials; no local Terraform state."""

import argparse
import json
from pathlib import Path
import boto3

p = argparse.ArgumentParser()
p.add_argument("--prefix", required=True, help="Globally unique lowercase bucket prefix")
p.add_argument(
    "--oidc-subject",
    required=True,
    help="Exact GitHub main-branch OIDC sub, including immutable IDs if applicable",
)
a = p.parse_args()
if not a.oidc_subject.endswith(":ref:refs/heads/main") or "*" in a.oidc_subject:
    raise SystemExit("Require exact main-branch subject, no wildcards")
region = "ap-south-1"
session = boto3.Session(region_name=region)
s3 = session.client("s3")
iam = session.client("iam")
secrets = session.client("secretsmanager")
account = session.client("sts").get_caller_identity()["Account"]
state = f"{a.prefix}-orion-state"
artifacts = f"{a.prefix}-orion-artifacts"
for bucket in (state, artifacts):
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            k: True
            for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        },
    )
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            }
        ),
    )
try:
    secret = secrets.create_secret(
        Name="orion-india/credentials",
        Description="Populate privately after bootstrap; no value in Terraform",
    )["ARN"]
except secrets.exceptions.ResourceExistsException:
    secret = secrets.describe_secret(SecretId="orion-india/credentials")["ARN"]
provider = f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"
try:
    iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider)
except iam.exceptions.NoSuchEntityException:
    iam.create_open_id_connect_provider(
        Url="https://token.actions.githubusercontent.com", ClientIDList=["sts.amazonaws.com"]
    )


def role(name, trust):
    try:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    return f"arn:aws:iam::{account}:role/{name}"


role(
    "orion-india-ec2",
    {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    },
)
iam.attach_role_policy(
    RoleName="orion-india-ec2", PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
)
iam.put_role_policy(
    RoleName="orion-india-ec2",
    PolicyName="orion-runtime",
    PolicyDocument=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret},
                {
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{artifacts}/releases/*",
                },
            ],
        }
    ),
)
try:
    iam.create_instance_profile(InstanceProfileName="orion-india-ec2")
except iam.exceptions.EntityAlreadyExistsException:
    pass
if not iam.get_instance_profile(InstanceProfileName="orion-india-ec2")["InstanceProfile"]["Roles"]:
    iam.add_role_to_instance_profile(InstanceProfileName="orion-india-ec2", RoleName="orion-india-ec2")
arn = role(
    "orion-india-github",
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub": a.oidc_subject,
                    }
                },
            }
        ],
    },
)
ec2_actions = [
    "Describe*",
    "RunInstances",
    "TerminateInstances",
    "StopInstances",
    "StartInstances",
    "ModifyInstanceAttribute",
    "ModifyInstanceMetadataOptions",
    "CreateVpc",
    "DeleteVpc",
    "ModifyVpcAttribute",
    "CreateSubnet",
    "DeleteSubnet",
    "ModifySubnetAttribute",
    "CreateInternetGateway",
    "DeleteInternetGateway",
    "AttachInternetGateway",
    "DetachInternetGateway",
    "CreateRouteTable",
    "DeleteRouteTable",
    "CreateRoute",
    "DeleteRoute",
    "AssociateRouteTable",
    "DisassociateRouteTable",
    "ReplaceRouteTableAssociation",
    "CreateSecurityGroup",
    "DeleteSecurityGroup",
    "AuthorizeSecurityGroupEgress",
    "RevokeSecurityGroupEgress",
    "AllocateAddress",
    "ReleaseAddress",
    "AssociateAddress",
    "DisassociateAddress",
    "CreateTags",
    "DeleteTags",
]
policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["ec2:" + x for x in ec2_actions],
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": region}},
        },
        {
            "Effect": "Allow",
            "Action": ["iam:GetInstanceProfile"],
            "Resource": f"arn:aws:iam::{account}:instance-profile/orion-india-ec2",
        },
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": f"arn:aws:iam::{account}:role/orion-india-ec2",
            "Condition": {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
        },
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:aws:s3:::{state}", f"arn:aws:s3:::{artifacts}"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": [f"arn:aws:s3:::{state}/orion/*", f"arn:aws:s3:::{artifacts}/releases/*"],
        },
        {"Effect": "Allow", "Action": "s3:DeleteObject", "Resource": f"arn:aws:s3:::{state}/orion/*.tflock"},
        {
            "Effect": "Allow",
            "Action": "ssm:SendCommand",
            "Resource": f"arn:aws:ssm:{region}::document/AWS-RunShellScript",
        },
        {
            "Effect": "Allow",
            "Action": "ssm:SendCommand",
            "Resource": f"arn:aws:ec2:{region}:{account}:instance/*",
            "Condition": {"StringEquals": {"ssm:resourceTag/Project": "orion-india"}},
        },
        {
            "Effect": "Allow",
            "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
            "Resource": "*",
        },
    ],
}
iam.put_role_policy(
    RoleName="orion-india-github", PolicyName="orion-deploy", PolicyDocument=json.dumps(policy)
)
result = {
    "STATE_BUCKET": state,
    "ARTIFACT_BUCKET": artifacts,
    "AWS_ROLE_ARN": arn,
    "SECRET_ARN": secret,
    "INSTANCE_PROFILE": "orion-india-ec2",
}
Path("bootstrap-outputs.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
