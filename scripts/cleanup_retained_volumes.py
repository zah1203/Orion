"""Optional confirmed deletion of detached volumes recorded by application teardown."""
import json
import os
import re
import boto3


def validate_receipt(receipt, account):
    if receipt.get("account") != account or receipt.get("region") != "ap-south-1":
        raise ValueError("Receipt account/region mismatch")
    if not re.fullmatch(r"i-[0-9a-f]+", receipt.get("instance_id", "")):
        raise ValueError("Invalid recorded instance")
    volumes = receipt.get("volume_ids", [])
    if not volumes or any(not re.fullmatch(r"vol-[0-9a-f]+", v) for v in volumes):
        raise ValueError("Invalid recorded volumes")
    return volumes


def main():
    if os.environ.get("CONFIRMATION") != "DELETE RETAINED ORION VOLUMES":
        raise ValueError("Exact permanent-volume-deletion confirmation required")
    key = os.environ["VOLUME_RECEIPT"]
    if not re.fullmatch(r"releases/lifecycle/application/[0-9a-f]{40}/[0-9]+/retained-volumes.json", key):
        raise ValueError("Invalid teardown receipt key")
    s3, ec2 = [boto3.client(s, region_name="ap-south-1") for s in ("s3", "ec2")]
    receipt = json.loads(s3.get_object(Bucket=os.environ["ARTIFACT_BUCKET"], Key=key)["Body"].read())
    volumes = validate_receipt(receipt, boto3.client("sts").get_caller_identity()["Account"])
    try:
        instances = ec2.describe_instances(InstanceIds=[receipt["instance_id"]])["Reservations"]
    except ec2.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
            raise
        instances = []
    if any(i["State"]["Name"] != "terminated" for r in instances for i in r["Instances"]):
        raise ValueError("Recorded source instance has not terminated")
    for volume in volumes:
        try:
            item = ec2.describe_volumes(VolumeIds=[volume])["Volumes"][0]
        except ec2.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidVolume.NotFound":
                continue
            raise
        tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
        if item["State"] != "available" or item.get("Attachments") or tags.get("OrionRetained") != "true":
            raise ValueError("Volume is attached or not tagged for Orion teardown")
        ec2.delete_volume(VolumeId=volume)
    print("Recorded detached Orion root volumes deleted. Backups and key escrow are unchanged.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Volume cleanup failed: " + type(exc).__name__) from None
