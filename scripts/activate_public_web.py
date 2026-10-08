"""Activate a reviewed public endpoint through SSM, without credentials in workflow inputs."""
import os
import re
import shlex
import time
import boto3
from configure_public_web import public_host

origin = os.environ["PUBLIC_WEB_URL"]
host = public_host(origin)
instance = os.environ["INSTANCE_ID"]
commit = os.environ["GITHUB_SHA"]
if not re.fullmatch(r"i-[0-9a-f]+", instance) or not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("Invalid deployment identifiers")
api = boto3.client("apigatewayv2", region_name="ap-south-1").get_api(ApiId=host.split(".")[0])
if api["ApiEndpoint"] != origin or api["Name"] != "orion-paper-web" or api.get("Tags", {}).get("Project") != "orion-india":
    raise SystemExit("URL does not identify this account's Orion public API")
ec2 = boto3.client("ec2", region_name="ap-south-1")
result = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]
if {t["Key"]: t["Value"] for t in result.get("Tags", [])}.get("Project") != "orion-india":
    raise SystemExit("Instance is not tagged for Orion")
private_ip = result["PrivateIpAddress"]
args = shlex.join(["--origin", origin, "--private-ip", private_ip])
command = (
    "set -eu\n"
    f'test "$(readlink -f /opt/orion/current)" = /opt/orion/releases/{commit}\n'
    f"/opt/orion/current/.venv/bin/python /opt/orion/current/scripts/configure_public_web.py {args}"
)
ssm = boto3.client("ssm", region_name="ap-south-1")
command_id = ssm.send_command(
    InstanceIds=[instance], DocumentName="AWS-RunShellScript",
    Parameters={"commands": [command], "executionTimeout": ["600"]}, TimeoutSeconds=600,
)["Command"]["CommandId"]
print("SSM activation command:", command_id, flush=True)
for _ in range(130):
    time.sleep(5)
    try:
        outcome = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
    except ssm.exceptions.InvocationDoesNotExist:
        continue
    if outcome["Status"] in ("Pending", "InProgress", "Delayed"):
        continue
    print(outcome.get("StandardOutputContent", ""))
    print(outcome.get("StandardErrorContent", ""))
    if outcome["Status"] != "Success":
        raise SystemExit("Activation failed; inspect the SSM output before retrying")
    break
else:
    raise SystemExit("Activation polling timed out; inspect the existing SSM command")
print("Configured URL:", origin)
