"""Manual workflow: save a concrete plan, then apply that exact reviewed binary."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import boto3

s3 = boto3.client("s3", region_name="ap-south-1")
bucket = os.environ["ARTIFACT_BUCKET"]
sha = os.environ["GITHUB_SHA"]
plan = Path("infra/reviewed.tfplan")
action = os.environ["PLAN_ACTION"]


def tf(*args, **kwargs):
    return subprocess.run(["terraform", "-chdir=infra", *args], check=True, **kwargs)


if action == "plan":
    tf("plan", "-input=false", "-lock-timeout=60s", "-out=reviewed.tfplan")
    pretty = tf("show", "-no-color", "reviewed.tfplan", capture_output=True, text=True).stdout
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    prefix = f"releases/plans/{sha}/{os.environ['GITHUB_RUN_ID']}"
    manifest = {"commit": sha, "sha256": digest, "plan_key": prefix + "/reviewed.tfplan"}
    s3.upload_file(str(plan), bucket, manifest["plan_key"])
    s3.put_object(Bucket=bucket, Key=prefix + "/plan.txt", Body=pretty.encode())
    s3.put_object(Bucket=bucket, Key=prefix + "/manifest.json", Body=json.dumps(manifest).encode())
    summary = f"Review this plan before manually running apply.\n\nCommit: `{sha}`\n\nplan_key: `{prefix}/manifest.json`\n\nplan_sha256: `{digest}`\n\n```text\n{pretty}\n```\n"
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(summary)
    print(summary)
elif action == "apply":
    key = os.environ["PLAN_KEY"]
    expected = os.environ["PLAN_SHA256"]
    if not re.fullmatch(r"releases/plans/" + re.escape(sha) + r"/[0-9]+/manifest.json", key):
        raise SystemExit("Plan must belong to the exact current main commit")
    if not re.fullmatch("[0-9a-f]{64}", expected):
        raise SystemExit("Reviewed SHA256 required")
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    if (
        manifest["commit"] != sha
        or manifest["sha256"] != expected
        or manifest["plan_key"] != key.replace("manifest.json", "reviewed.tfplan")
    ):
        raise SystemExit("Plan metadata mismatch")
    s3.download_file(bucket, manifest["plan_key"], str(plan))
    if hashlib.sha256(plan.read_bytes()).hexdigest() != expected:
        raise SystemExit("Plan checksum mismatch")
    tf("apply", "-input=false", "-lock-timeout=60s", "reviewed.tfplan")
    tf("output")
else:
    raise SystemExit("Use plan or apply")
