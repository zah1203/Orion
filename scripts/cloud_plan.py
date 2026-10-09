"""GitHub-only saved-plan lifecycle for application and recovery stacks."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REGION = "ap-south-1"
CONFIRM = {"application": "DESTROY ORION APPLICATION", "recovery": "DELETE ORION BACKUPS AND KEYS"}
STACKS = {"application": Path("infra"), "recovery": Path("infra/backup")}


def check_request(stack, action, confirmation):
    if stack not in STACKS or action not in ("plan", "apply", "plan-destroy", "destroy", "check-destroy", "outputs"):
        raise ValueError("Unknown lifecycle request")
    if action in ("plan-destroy", "destroy", "check-destroy") and confirmation != CONFIRM[stack]:
        raise ValueError("Exact destructive confirmation is required")


def validate_manifest(manifest, key, digest, commit, stack, operation, inputs):
    pattern = rf"releases/lifecycle/{stack}/{commit}/[0-9]+/manifest.json"
    if not re.fullmatch(pattern, key) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Exact current-commit plan key and SHA256 required")
    expected = dict(commit=commit, stack=stack, operation=operation, sha256=digest,
                    plan_key=key.removesuffix("manifest.json") + "reviewed.tfplan", inputs=inputs)
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise ValueError("Reviewed plan metadata mismatch")


def changes_only_destroy(plan):
    changes = [r for r in plan.get("resource_changes", []) if r.get("mode") == "managed"]
    if any(r["change"]["actions"] not in (["delete"], ["no-op"]) for r in changes):
        raise ValueError("Destroy plan contains non-delete changes")
    return changes


def purge_bucket(s3, bucket):
    # Dedicated data bucket only; no access to state/release objects here.
    if bucket in (os.environ["STATE_BUCKET"], os.environ["ARTIFACT_BUCKET"]):
        raise ValueError("Control-plane bucket purge forbidden")
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        objects = [{"Key": x["Key"], "VersionId": x["VersionId"]}
                   for field in ("Versions", "DeleteMarkers") for x in page.get(field, [])]
        for i in range(0, len(objects), 1000):
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[i:i + 1000], "Quiet": True})
            if result.get("Errors"):
                raise ValueError("Backup version deletion failed; stop and inspect")
    for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        for upload in page.get("Uploads", []):
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


def main():
    import boto3
    stack, action = os.environ["LIFECYCLE_STACK"], os.environ["LIFECYCLE_ACTION"]
    check_request(stack, action, os.environ.get("CONFIRMATION", ""))
    if stack == "recovery":
        bucket_name = os.environ["TF_VAR_bucket_name"]
        if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket_name)
                or bucket_name in (os.environ["STATE_BUCKET"], os.environ["ARTIFACT_BUCKET"])):
            raise ValueError("A distinct, valid BACKUP_BUCKET is required")
    commit = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid source commit")
    operation = "destroy" if action in ("plan-destroy", "destroy", "check-destroy") else "apply"
    cache = Path(tempfile.gettempdir()) / "orion-tf-providers"
    cache.mkdir(exist_ok=True, mode=0o700)
    os.environ["TF_PLUGIN_CACHE_DIR"] = str(cache)
    s3 = boto3.client("s3", region_name=REGION)
    inputs = hashlib.sha256(json.dumps({k: v for k, v in os.environ.items()
                                       if k.startswith("TF_VAR_") or k == "STATE_BUCKET"}, sort_keys=True).encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="orion-tf-") as tmp:
        root = Path(tmp)
        for source in STACKS[stack].iterdir():
            if source.is_file() and (source.suffix == ".tf" or source.name.endswith((".tftpl", ".hcl"))):
                content = source.read_text()
                if operation == "destroy" and source.suffix == ".tf":
                    # Literal lifecycle guards stay in Git. Relax only in this reviewed destroy workspace.
                    content = content.replace("lifecycle { prevent_destroy = true }", "lifecycle { prevent_destroy = false }")
                (root / source.name).write_text(content)

        def tf(*args, capture=True):
            return subprocess.run(["terraform", f"-chdir={root}", *args], check=True,
                                  capture_output=capture, text=True).stdout

        tf("init", "-input=false", "-lockfile=readonly", "-backend-config=bucket=" + os.environ["STATE_BUCKET"])
        tf("validate")
        if action == "outputs":
            values = {k: v["value"] for k, v in json.loads(tf("output", "-json")).items()}
            Path(os.environ["OUTPUT_FILE"]).write_text(json.dumps(values))
            return
        bucket = os.environ["ARTIFACT_BUCKET"]
        # Remote state identity is checked before any deletion outside Terraform.
        state = json.loads(tf("state", "pull") or "{}") if operation == "destroy" else None
        state_identity = {k: state.get(k) for k in ("lineage", "serial")} if state else None
        plan_path = root / "reviewed.tfplan"
        if action in ("plan", "plan-destroy"):
            flags = ["-destroy"] if operation == "destroy" else []
            tf("plan", "-input=false", "-lock-timeout=60s", *flags, "-out=reviewed.tfplan")
            plan = json.loads(tf("show", "-json", "reviewed.tfplan"))
            if operation == "destroy":
                changes_only_destroy(plan)
            digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            prefix = f"releases/lifecycle/{stack}/{commit}/{os.environ['GITHUB_RUN_ID']}"
            manifest = dict(commit=commit, stack=stack, operation=operation, sha256=digest,
                            inputs=inputs, plan_key=prefix + "/reviewed.tfplan", state_identity=state_identity)
            s3.upload_file(str(plan_path), bucket, manifest["plan_key"])
            s3.put_object(Bucket=bucket, Key=prefix + "/manifest.json", Body=json.dumps(manifest).encode())
            pretty = tf("show", "-no-color", "reviewed.tfplan")
            summary = f"## Review {stack} {operation} plan\n\nplan_key: `{prefix}/manifest.json`\n\nplan_sha256: `{digest}`\n\n```text\n{pretty}\n```\n"
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
                out.write(summary)
            print("Plan saved. Review the job summary before applying.")
            return
        key, digest = os.environ["PLAN_KEY"], os.environ["PLAN_SHA256"]
        # Validate key before reading an arbitrary S3 path.
        if not re.fullmatch(rf"releases/lifecycle/{stack}/{commit}/[0-9]+/manifest.json", key):
            raise ValueError("Invalid reviewed plan location")
        manifest = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        validate_manifest(manifest, key, digest, commit, stack, operation, inputs)
        s3.download_file(bucket, manifest["plan_key"], str(plan_path))
        if hashlib.sha256(plan_path.read_bytes()).hexdigest() != digest:
            raise ValueError("Plan checksum mismatch")
        plan = json.loads(tf("show", "-json", "reviewed.tfplan"))
        if operation == "destroy" and manifest.get("state_identity") != state_identity:
            raise ValueError("State changed since destroy plan; make a fresh plan")
        if action == "check-destroy":
            changes_only_destroy(plan)
            return
        retained = []
        if operation == "destroy":
            changes = changes_only_destroy(plan)
            if stack == "recovery":
                # Require a separately completed runtime disable receipt from this workflow run.
                if not Path("/tmp/orion-backup-disabled.json").is_file():
                    raise ValueError("Disable backup scheduling before purging recovery")
                buckets = [r["change"]["before"]["bucket"] for r in changes
                           if r["type"] == "aws_s3_bucket" and r["change"]["actions"] == ["delete"]]
                if buckets != [os.environ["TF_VAR_bucket_name"]]:
                    raise ValueError("Unexpected recovery bucket in destroy plan")
                purge_bucket(s3, buckets[0])
            else:
                evidence = json.loads(Path("/tmp/orion-final-drill.json").read_text())
                if evidence.get("instance_id") != os.environ["INSTANCE_ID"] or not evidence.get("isolated_restore_verified"):
                    raise ValueError("Fresh final backup and isolated drill required")
                ec2 = boto3.client("ec2", region_name=REGION)
                for resource in changes:
                    if resource["type"] == "aws_instance" and resource["change"]["actions"] == ["delete"]:
                        before = resource["change"]["before"]
                        if before["id"] != os.environ["INSTANCE_ID"]:
                            raise ValueError("Destroy instance does not match final backup")
                        retained.extend(b["volume_id"] for b in before["root_block_device"])
                        ec2.create_tags(Resources=retained, Tags=[{"Key": "OrionRetained", "Value": "true"}])
                        ec2.modify_instance_attribute(InstanceId=before["id"], DisableApiTermination={"Value": False})
        try:
            tf("apply", "-input=false", "-lock-timeout=60s", "reviewed.tfplan", capture=False)
        except Exception:
            if stack == "application" and operation == "destroy":
                try:
                    ec2.modify_instance_attribute(InstanceId=os.environ["INSTANCE_ID"], DisableApiTermination={"Value": True})
                except Exception:
                    print("Check EC2 termination protection after failed teardown.")
            raise
        if stack == "application" and operation == "destroy":
            receipt = dict(instance_id=os.environ["INSTANCE_ID"], volume_ids=retained, plan_sha256=digest,
                           account=boto3.client("sts").get_caller_identity()["Account"], region=REGION)
            receipt_key = key.removesuffix("manifest.json") + "retained-volumes.json"
            s3.put_object(Bucket=bucket, Key=receipt_key, Body=json.dumps(receipt).encode())
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
                out.write(f"Application destroyed. Recovery resources and root EBS volumes retained.\n\nVolume cleanup receipt: `{receipt_key}`\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Lifecycle failed: " + type(exc).__name__ + ". Inspect reviewed plan and AWS status before retrying.") from None
