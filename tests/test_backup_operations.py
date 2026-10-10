import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from scripts import backup_host as host
from scripts import backup_operations as ops
from scripts import cloud_plan as plan
from scripts.cleanup_retained_volumes import validate_receipt


class BackupOperationsTests(unittest.TestCase):
    def test_bootstrap_allows_secret_kms_validation_with_scoped_data_key(self):
        template = json.loads((Path(__file__).resolve().parents[1] /
                               "bootstrap/github-recovery-permissions.json").read_text())
        statements = template["Resources"]["RecoveryPermissions"]["Properties"]["PolicyDocument"]["Statement"]
        for action in ("kms:GenerateDataKey", "kms:Decrypt"):
            grants = [s for s in statements if action in s["Action"]]
            self.assertTrue(grants, action + " required by Secrets Manager CreateSecret")
            for grant in grants:
                self.assertEqual(grant["Effect"], "Allow")
                self.assertEqual(grant["Condition"]["StringEquals"]["aws:ResourceTag/Purpose"],
                                 "orion-recovery")
                self.assertEqual(grant["Resource"]["Fn::Sub"],
                                 "arn:${AWS::Partition}:kms:ap-south-1:${AWS::AccountId}:key/*")
        data_key = next(s for s in statements if "kms:GenerateDataKey" in s["Action"])
        self.assertEqual(data_key["Condition"]["StringEquals"]["kms:ViaService"],
                         "secretsmanager.ap-south-1.amazonaws.com")

    def test_subprocess_diagnostics_hide_sensitive_output(self):
        for stage, stderr, expected in [
            ("runtime-dependencies", "private token", "runtime-dependencies"),
            ("backup-subprocess", "private token\nORION_RECOVERY_STAGE=s3-upload-readback",
             "backup-s3-upload-readback"),
            ("backup-subprocess", "ORION_RECOVERY_STAGE=secret-value", "backup-subprocess"),
        ]:
            with self.subTest(stage=stage, expected=expected), patch.object(
                    host.subprocess, "run", side_effect=subprocess.CalledProcessError(
                        1, ["private-command"], output="private token", stderr=stderr)):
                with self.assertRaises(host.SafeHostFailure) as caught:
                    host.checked_process(stage, ["test"])
                self.assertEqual(str(caught.exception), expected)

    def config(self):
        return dict(backup_bucket="orion-test-backups", portal_key_secret="portal-arn",
                    archive_key_secret="archive-arn", escrow_kms_key="kms-arn", runtime_role_name="runtime")

    def test_escrow_create_idempotent_and_never_overwrite_existing_key(self):
        client = Mock()
        key = Fernet.generate_key()
        client.describe_secret.return_value = {"VersionIdsToStages": {}}
        client.put_secret_value.return_value = {"VersionId": "new-version"}
        client.get_secret_value.return_value = {"SecretBinary": key}
        self.assertEqual(host.escrow(client, "arn", key), "new-version")
        self.assertEqual(client.put_secret_value.call_args.kwargs["SecretBinary"], key)
        client.reset_mock()
        client.describe_secret.return_value = {"VersionIdsToStages": {"existing": ["AWSCURRENT"]}}
        self.assertEqual(host.escrow(client, "arn", key), "existing")
        client.put_secret_value.assert_not_called()
        client.get_secret_value.return_value = {"SecretBinary": Fernet.generate_key()}
        with self.assertRaisesRegex(ValueError, "mismatch"):
            host.escrow(client, "arn", key)
        client.put_secret_value.assert_not_called()

    def test_escrow_noncurrent_versions_require_review(self):
        client = Mock()
        client.describe_secret.return_value = {"VersionIdsToStages": {"old": ["AWSPREVIOUS"]}}
        with self.assertRaisesRegex(ValueError, "operator review"):
            host.escrow(client, "arn", Fernet.generate_key())
        client.put_secret_value.assert_not_called()

    def test_temporary_grant_removed_on_failure(self):
        iam = Mock()
        with self.assertRaises(RuntimeError):
            with ops.escrow_grant(iam, self.config()):
                raise RuntimeError("host failed")
        iam.delete_role_policy.assert_called_once_with(RoleName="runtime", PolicyName=ops.POLICY)
        policy = json.loads(iam.put_role_policy.call_args.kwargs["PolicyDocument"])
        self.assertEqual(policy["Statement"][0]["Resource"], ["portal-arn", "archive-arn"])
        self.assertNotIn("secretsmanager:DeleteSecret", policy["Statement"][0]["Action"])

    def test_ssm_failure_output_is_not_relayed(self):
        client = Mock()
        client.send_command.return_value = {"Command": {"CommandId": "test-command"}}
        client.get_command_invocation.return_value = {"Status": "Failed", "StandardErrorContent": "sensitive payload"}
        with patch.object(ops.time, "sleep"), self.assertRaises(RuntimeError) as result:
            ops.send(client, "i-123", "harmless command")
        self.assertNotIn("sensitive", str(result.exception))

    def test_receipt_rejects_other_bucket_secret_and_missing_version(self):
        receipt = {"s3": {"bucket": "orion-test-backups", "key": "daily/orion-20261009T000000Z-" + "a" * 32 + ".orb",
                           "version_id": "v1", "sha256": "a" * 64}, "portal_key_secret": "portal-arn", "archive_key_secret": "archive-arn"}
        ops.validate_receipt(receipt, self.config())
        for field, value in (("bucket", "another-bucket"), ("key", "daily/../escape"), ("version_id", "")):
            copy = json.loads(json.dumps(receipt))
            copy["s3"][field] = value
            with self.assertRaises(ValueError):
                ops.validate_receipt(copy, self.config())
        receipt["archive_key_secret"] = "unexpected-secret"
        with self.assertRaises(ValueError):
            ops.validate_receipt(receipt, self.config())

    def test_drill_downloads_pinned_version_and_strips_network_and_credentials(self):
        from orion.recovery import sha
        config = self.config()
        receipt = {"s3": {"bucket": config["backup_bucket"], "key": "daily/orion-20261009T000000Z-" + "b" * 32 + ".orb",
                           "version_id": "object-version", "sha256": __import__('hashlib').sha256(b"ciphertext").hexdigest()},
                   "portal_key_secret": "portal-arn", "archive_key_secret": "archive-arn",
                   "key_versions": {"portal": "portal-version", "archive": "archive-version"}}
        s3, secrets = Mock(), Mock()
        s3.download_file.side_effect = lambda bucket, key, filename, **kw: Path(filename).write_bytes(b"ciphertext")
        secrets.get_secret_value.return_value = {"SecretBinary": Fernet.generate_key()}
        observed = {}

        def run(command, **kwargs):
            observed["command"] = command
            self.assertIn("unshare", command)
            self.assertIn("--net", command)
            self.assertIn("setpriv", command)
            self.assertIn("-i", command)
            self.assertNotIn("AWS_ACCESS_KEY_ID", " ".join(command))
            dest = Path(command[command.index("--destination") + 1])
            self.assertFalse(dest.exists())
            key = Path(command[command.index("--portal-key-file") + 1])
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(command, 0, json.dumps({"paper_ledgers_unchanged": True,
                                               "network_started": False, "verified_before_sanitizing": {"accounts": 2}}))

        with patch.object(ops.subprocess, "run", side_effect=run):
            report = ops.drill(s3, secrets, config, receipt)
        self.assertTrue(report["isolated_restore_verified"])
        self.assertEqual(s3.download_file.call_args.kwargs, {"ExtraArgs": {"VersionId": "object-version"}})
        self.assertEqual([c.kwargs["VersionId"] for c in secrets.get_secret_value.call_args_list],
                         ["portal-version", "archive-version"])
        self.assertFalse(Path(observed["command"][-1]).parent.exists())

    def test_installer_does_not_touch_application_services_or_restart_workers(self):
        command = ops.installer(self.config(), "a" * 40)
        self.assertLess(len(command), 64000)
        # Decode the installer payload instead of trusting only the outer shell wrapper.
        import ast, base64
        program = command.split("\n", 1)[1].rsplit("ORION_INSTALL", 1)[0]
        assignment = ast.parse(program).body[1].value
        encoded = ast.literal_eval(assignment.args[0].args[0])
        payload = json.loads(base64.b64decode(encoded))
        self.assertEqual(set(payload["files"]), {"orion/recovery.py", "scripts/backup_host.py",
                                                "scripts/orion-backup.service", "scripts/orion-backup.timer"})
        self.assertIn("backup_host.py backup", payload["files"]["scripts/orion-backup.service"])
        self.assertNotIn("systemctl enable", command)
        self.assertNotIn("systemctl restart", command)

    def test_destroy_requires_scope_specific_confirmation(self):
        for stack in plan.STACKS:
            for action in ("plan-destroy", "destroy", "check-destroy"):
                with self.assertRaises(ValueError):
                    plan.check_request(stack, action, "")
                plan.check_request(stack, action, plan.CONFIRM[stack])
        with self.assertRaises(ValueError):
            plan.check_request("other", "plan", "")

    def test_saved_plan_rejects_wrong_commit_operation_or_inputs(self):
        sha, digest = "a" * 40, "b" * 64
        key = f"releases/lifecycle/recovery/{sha}/123/manifest.json"
        manifest = dict(commit=sha, stack="recovery", operation="destroy", sha256=digest,
                        plan_key=key.replace("manifest.json", "reviewed.tfplan"), inputs="same")
        plan.validate_manifest(manifest, key, digest, sha, "recovery", "destroy", "same")
        for override in ({"commit": "c" * 40}, {"operation": "apply"}, {"inputs": "changed"}):
            with self.assertRaises(ValueError):
                plan.validate_manifest({**manifest, **override}, key, digest, sha, "recovery", "destroy", "same")
        with self.assertRaises(ValueError):
            plan.changes_only_destroy({"resource_changes": [{"mode": "managed", "change": {"actions": ["delete", "create"]}}]})

    def test_purge_keeps_control_plane_and_checks_partial_deletion_errors(self):
        s3 = Mock()
        with patch.dict(os.environ, {"STATE_BUCKET": "state", "ARTIFACT_BUCKET": "artifacts"}):
            for bucket in ("state", "artifacts"):
                with self.assertRaises(ValueError):
                    plan.purge_bucket(s3, bucket)
            s3.get_paginator.return_value.paginate.return_value = [{"Versions": [{"Key": "daily/a", "VersionId": "v"}]}]
            s3.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
            with self.assertRaises(ValueError):
                plan.purge_bucket(s3, "backups")

    def test_volume_receipt_account_boundary(self):
        receipt = dict(account="123456789012", region="ap-south-1", instance_id="i-abc", volume_ids=["vol-abc"])
        self.assertEqual(validate_receipt(receipt, "123456789012"), ["vol-abc"])
        with self.assertRaises(ValueError):
            validate_receipt(receipt, "999999999999")


if __name__ == "__main__":
    unittest.main()
