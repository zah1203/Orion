"""Offline paper recovery. No network or application/worker imports unless uploading to S3."""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"ORION-BACKUP-1\n"
CHUNK = 1024 * 1024
UID = re.compile(r"[0-9a-f]{32}")
NAME = re.compile(r"orion-\d{8}T\d{6}Z-[0-9a-f]{32}\.orb")
CONFIG_FILES = ("config.json", "contracts.json", "economics.json", "portal.env", "public-web.env", "environment")


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink directory rejected")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("Directory must be owner-only")
    return path.resolve()


def regular(path):
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Expected a regular file; symlinks are forbidden")
    return path


def key_bytes(path):
    path = regular(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Key file must be owner-only")
    key = path.read_bytes().strip()
    Fernet(key)  # Validate the same 32-byte URL-safe format as portal.key.
    return key


def sha(path):
    h = hashlib.sha256()
    with regular(path).open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def connect_ro(path):
    return sqlite3.connect(regular(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def check_db(path):
    with closing(connect_ro(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("SQLite foreign-key check failed")


def sqlite_copy(source, target, timeout=60):
    """Online backup includes committed WAL contents without checkpointing/stopping writers."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup deadline exceeded; retry later")

    with closing(connect_ro(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
    target.chmod(0o600)
    check_db(target)


def stable_copy(source, target):
    regular(source)
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, CHUNK)
    target.chmod(0o600)
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Source file changed during backup; retry")


def inventory(portal, runtime, config):
    """Allowlist durable state; fail closed on unknown portal state instead of omitting it."""
    portal, runtime, config = map(Path, (portal, runtime, config))
    if any(p.is_symlink() or not p.is_dir() for p in (portal, runtime, config)):
        raise ValueError("Source roots must be existing non-symlink directories")
    files = {}
    for path in portal.rglob("*"):
        rel = path.relative_to(portal)
        if path.is_symlink():
            raise ValueError("Symlink in portal state")
        if path.is_dir():
            if rel.parts[0].startswith("catalogue-refresh-"):
                continue
            if rel == Path("accounts") or (len(rel.parts) == 2 and rel.parts[0] == "accounts" and UID.fullmatch(rel.name)):
                continue
            raise ValueError("Unknown directory in portal state; review inventory")
        if rel.parts[0].startswith("catalogue-refresh-"):
            continue  # In-progress catalogue exports are reproducible, not application state.
        if rel.name.endswith((".lock", "-wal", "-shm", "-journal")):
            continue
        allowed = str(rel) in ("accounts.db", "key-check", "contracts.json", "economics.json")
        allowed |= len(rel.parts) == 3 and rel.parts[0] == "accounts" and bool(UID.fullmatch(rel.parts[1])) and rel.name == "paper.db"
        if not allowed:
            raise ValueError("Unknown file in portal state; review inventory")
        files["portal/" + rel.as_posix()] = (regular(path), path.suffix == ".db")
    for required in ("portal/accounts.db", "portal/key-check"):
        if required not in files:
            raise ValueError("Required portal state missing")
    for logical, source in (("runtime/paper.db", runtime / "paper.db"),
                            ("legacy/telegram.session", runtime.parent / "telegram.session")):
        if source.exists() or source.is_symlink():
            files[logical] = (regular(source), True)
    for name in CONFIG_FILES:
        source = config / name
        if source.exists() or source.is_symlink():
            files["config/" + name] = (regular(source), False)
    return files


def inspect_state(root, portal_key):
    """Validate credentials offline, without printing values or constructing a Store."""
    cipher = Fernet(portal_key)
    if cipher.decrypt((root / "portal/key-check").read_bytes()) != b"orion-portal-key-v1":
        raise ValueError("Portal key marker mismatch")
    counts = {"accounts": 0, "credential_records": 0, "broker_sessions": 0, "push_tokens": 0,
              "paper_databases": 0, "positions": 0, "open_positions": 0}
    with closing(connect_ro(root / "portal/accounts.db")) as db:
        users = {row[0] for row in db.execute("SELECT id FROM users")}
        if any(not UID.fullmatch(uid) for uid in users):
            raise ValueError("Invalid account identity")
        counts["accounts"] = len(users)
        for table, label in (("secrets", "credential_records"), ("broker_sessions", "broker_sessions")):
            for uid, ciphertext in db.execute(f"SELECT user_id,ciphertext FROM {table}"):
                item = json.loads(cipher.decrypt(ciphertext))
                if uid not in users or item["user_id"] != uid or not isinstance(item["values"], dict):
                    raise ValueError("Credential ownership/structure mismatch")
                counts[label] += 1
        for uid, ciphertext in db.execute("SELECT user_id,ciphertext FROM push_devices"):
            if uid not in users:
                raise ValueError("Push token ownership mismatch")
            cipher.decrypt(ciphertext)
            counts["push_tokens"] += 1
    paths = list((root / "portal/accounts").glob("*/paper.db"))
    if any(p.parent.name not in users for p in paths):
        raise ValueError("Orphan paper ledger; reconcile inventory before backup")
    if (root / "runtime/paper.db").exists():
        paths.append(root / "runtime/paper.db")
    for path in paths:
        with closing(connect_ro(path)) as db:
            row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
            if row is None:
                raise ValueError("Paper ledger has no state")
            state = json.loads(row[0])
            counts["paper_databases"] += 1
            counts["positions"] += len(state["positions"])
            counts["open_positions"] += sum(p["status"] == "OPEN" for p in state["positions"].values())
    return counts


def encrypt(source, target, key):
    nonce = os.urandom(12)
    enc = Cipher(algorithms.AES(base64.urlsafe_b64decode(key)), modes.GCM(nonce)).encryptor()
    enc.authenticate_additional_data(MAGIC)
    with source.open("rb") as src, target.open("xb") as dst:
        dst.write(MAGIC + nonce)
        for block in iter(lambda: src.read(CHUNK), b""):
            dst.write(enc.update(block))
        dst.write(enc.finalize() + enc.tag)
        dst.flush()
        os.fsync(dst.fileno())
    target.chmod(0o600)


def decrypt(source, target, key):
    """Authenticate before opening/extracting the decrypted tar file."""
    size = regular(source).stat().st_size
    remaining = size - len(MAGIC) - 12 - 16
    if remaining < 0:
        raise ValueError("Truncated backup")
    with source.open("rb") as src, target.open("xb") as dst:
        if src.read(len(MAGIC)) != MAGIC:
            raise ValueError("Unknown backup format")
        nonce = src.read(12)
        dec = Cipher(algorithms.AES(base64.urlsafe_b64decode(key)), modes.GCM(nonce)).decryptor()
        dec.authenticate_additional_data(MAGIC)
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ValueError("Truncated backup")
            remaining -= len(block)
            dst.write(dec.update(block))
        dst.write(dec.finalize_with_tag(src.read(16)))
    target.chmod(0o600)


def unpack(archive, root):
    # Never use extractall: reject links, duplicate names and path traversal explicitly.
    names = set()
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or str(path) != member.name or member.name in names):
                raise ValueError("Unsafe archive member")
            if path.parts[0] not in ("portal", "runtime", "legacy", "config", "manifest.json"):
                raise ValueError("Unexpected archive root")
            names.add(member.name)
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(member) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            target.chmod(0o600)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["format"] != 1 or names != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Backup manifest mismatch")
    for name, info in manifest["files"].items():
        path = root / name
        if path.stat().st_size != info["bytes"] or sha(path) != info["sha256"]:
            raise ValueError("Backup checksum mismatch")
        if info["sqlite"]:
            check_db(path)
    return manifest


def verify(backup, backup_key, portal_key, work_dir):
    with tempfile.TemporaryDirectory(prefix="verify-", dir=private_dir(work_dir)) as temp:
        temp = Path(temp)
        decrypt(Path(backup), temp / "payload.tar", backup_key)
        root = temp / "data"
        root.mkdir(mode=0o700)
        manifest = unpack(temp / "payload.tar", root)
        counts = inspect_state(root, portal_key)
        if counts != manifest["counts"]:
            raise ValueError("Recovery inventory mismatch")
        return manifest


def create(portal, runtime, config, output, backup_key, portal_key, work_dir, timeout=60):
    if backup_key == portal_key:
        raise ValueError("Backup key must be separate from the portal key")
    output, work_dir = Path(output).resolve(), Path(work_dir).resolve()
    roots = [Path(p).resolve() for p in (portal, runtime, config)]
    if any(out == root or out.is_relative_to(root) for out in (output, work_dir) for root in roots):
        raise ValueError("Backup/work directories must be outside source roots")
    output, work_dir = private_dir(output), private_dir(work_dir)
    started = datetime.now(timezone.utc).isoformat()
    files = inventory(portal, runtime, config)
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=work_dir) as temp:
        temp = Path(temp)
        root = temp / "data"
        root.mkdir(mode=0o700)
        # accounts.db is intentionally first; each database is a separate consistent snapshot.
        for name in sorted(files, key=lambda n: (n != "portal/accounts.db", n)):
            source, is_db = files[name]
            target = root / name
            if is_db:
                sqlite_copy(source, target, timeout)
            else:
                stable_copy(source, target)
        if set(inventory(portal, runtime, config)) != set(files):
            raise ValueError("Inventory changed during backup; retry")
        with closing(connect_ro(Path(portal) / "accounts.db")) as src, closing(connect_ro(root / "portal/accounts.db")) as dst:
            if src.execute("SELECT id FROM users ORDER BY id").fetchall() != dst.execute("SELECT id FROM users ORDER BY id").fetchall():
                raise ValueError("Accounts changed during backup; retry")
        manifest = {"format": 1, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                    "consistency": "per-database online snapshots; not a global point-in-time snapshot",
                    "source_roots": [str(p) for p in roots], "counts": inspect_state(root, portal_key),
                    "absent_optional": [n for n in ("runtime/paper.db", "legacy/telegram.session") if n not in files],
                    "files": {n: {"bytes": (root / n).stat().st_size, "sha256": sha(root / n), "sqlite": db}
                              for n, (_, db) in files.items()}}
        (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        with tarfile.open(temp / "payload.tar", "w") as tar:
            for name in ["manifest.json", *sorted(files)]:
                tar.add(root / name, arcname=name, recursive=False)
        name = "orion-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex + ".orb"
        pending = output / (name + ".partial")
        try:
            encrypt(temp / "payload.tar", pending, backup_key)
            verify(pending, backup_key, portal_key, work_dir)
            final = output / name
            os.rename(pending, final)
            fd = os.open(final.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
        return final, manifest


def restore(backup, destination, backup_key, portal_key, work_dir):
    destination = Path(destination).absolute()
    destination = destination.parent.resolve() / destination.name
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination must not exist")
    if destination.is_relative_to(Path("/var/lib/orion")) or destination.is_relative_to(Path("/etc/orion")):
        raise ValueError("Production restore paths are forbidden")
    if not destination.parent.is_dir():
        raise ValueError("Restore parent must be an existing private directory")
    private_dir(destination.parent)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=private_dir(work_dir)) as temp:
        temp = Path(temp)
        decrypt(Path(backup), temp / "payload.tar", backup_key)
        # Reject source overlap before creating anything in the destination tree.
        with tarfile.open(temp / "payload.tar", "r:") as tar:
            with tar.extractfile("manifest.json") as source_manifest:
                source_roots = json.load(source_manifest)["source_roots"]
        for source in source_roots:
            if destination == Path(source) or destination.is_relative_to(Path(source)):
                raise ValueError("Restore overlaps original source")
        # Reserve the final name exclusively. No existing directory may be merged or replaced.
        destination.mkdir(mode=0o700)
        try:
            # Guard even an interrupted/partially extracted recovery before any DB exists.
            (destination / "portal").mkdir(mode=0o700)
            (destination / "portal/RECOVERY_ONLY").write_text(
                "Offline recovery only. Do not run workers or network services.\n"
            )
            manifest = unpack(temp / "payload.tar", destination)
            counts = inspect_state(destination, portal_key)
            if counts != manifest["counts"]:
                raise ValueError("Recovery inventory mismatch")
            # Modify only restored control metadata. Never call set_enabled(), which cancels positions.
            with closing(sqlite3.connect(destination / "portal/accounts.db")) as db, db:
                for uid, settings in db.execute("SELECT id,settings FROM users").fetchall():
                    cfg = json.loads(settings)
                    cfg.update(mode="paper", new_entries_enabled=False, live_inputs_enabled=False)
                    db.execute("UPDATE users SET enabled=0,settings=? WHERE id=?", (json.dumps(cfg), uid))
                for table in ("sessions", "broker_sessions", "worker_status", "worker_health", "push_outbox", "push_devices"):
                    db.execute(f"DELETE FROM {table}")
            report = {"verified_before_sanitizing": counts, "source_backup_sha256": sha(Path(backup)),
                      "paper_ledgers_unchanged": True, "network_started": False, "aws_verified": False}
            # Assert ledger bytes have not changed after restoring control metadata.
            for name, info in manifest["files"].items():
                if name.endswith("paper.db") and sha(destination / name) != info["sha256"]:
                    raise ValueError("Paper ledger changed during restore")
            (destination / "recovery-report.json").write_text(json.dumps(report, indent=2) + "\n")
            return report
        except BaseException:
            shutil.rmtree(destination)
            raise


def upload(backup, bucket, prefix, s3):
    """Upload only ciphertext; verify a version-pinned readback before returning success."""
    path = regular(Path(backup))
    if not NAME.fullmatch(path.name) or not re.fullmatch(r"[a-zA-Z0-9/_-]+/", prefix):
        raise ValueError("Invalid backup name or S3 prefix")
    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    if not all(block.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        raise ValueError("S3 bucket must block all public access")
    if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise ValueError("S3 versioning is required")
    if s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"]:
        raise ValueError("Public bucket policy rejected")
    if path.stat().st_size > 5 * 1024 ** 3:
        raise ValueError("Backup exceeds single PUT limit; do not delete local backup")
    with path.open("rb") as header:
        if header.read(len(MAGIC)) != MAGIC:
            raise ValueError("Only encrypted Orion backups may be uploaded")
    digest = sha(path)
    with path.open("rb") as body:
        result = s3.put_object(Bucket=bucket, Key=prefix + path.name, Body=body,
                               ServerSideEncryption="AES256", IfNoneMatch="*",
                               ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                               Metadata={"sha256": digest})
    version = result.get("VersionId")
    if not version or version == "null":
        raise ValueError("Upload did not return a version ID")
    result = s3.get_object(Bucket=bucket, Key=prefix + path.name, VersionId=version)
    h = hashlib.sha256()
    with closing(result["Body"]) as body:
        for block in iter(lambda: body.read(CHUNK), b""):
            h.update(block)
    if h.hexdigest() != digest:
        raise ValueError("S3 readback checksum mismatch")
    return {"bucket": bucket, "key": prefix + path.name, "version_id": version, "sha256": digest,
            "ciphertext_readback_verified": True, "aws_restore_drill_verified": False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init-key")
    q.add_argument("--file", type=Path, required=True)
    for command in ("backup", "verify", "restore"):
        q = sub.add_parser(command)
        q.add_argument("--backup-key-file", type=Path, required=True)
        q.add_argument("--portal-key-file", type=Path, required=True)
        q.add_argument("--work-dir", type=Path, required=True, help="Private encrypted volume with free scratch capacity")
        if command == "backup":
            q.add_argument("--portal", type=Path, default=Path("/var/lib/orion/portal"))
            q.add_argument("--runtime", type=Path, default=Path("/var/lib/orion/runtime"))
            q.add_argument("--config", type=Path, default=Path("/etc/orion"))
            q.add_argument("--output", type=Path, required=True)
            q.add_argument("--sqlite-timeout", type=int, default=60)
            q.add_argument("--bucket")
            q.add_argument("--remove-local-after-upload", action="store_true", help="Delete local ciphertext only after verified S3 readback")
            q.add_argument("--prefix", default="daily/")
            q.add_argument("--region", default="ap-south-1")
        else:
            q.add_argument("--backup", type=Path, required=True)
        if command == "restore":
            q.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    stage = "key-validation"
    try:
        if args.command == "init-key":
            with args.file.open("xb") as f:
                f.write(Fernet.generate_key() + b"\n")
            args.file.chmod(0o600)
            print("Created recovery key; escrow separately before relying on backups.")
            return
        backup_key, portal_key = key_bytes(args.backup_key_file), key_bytes(args.portal_key_file)
        if args.command == "backup":
            if args.remove_local_after_upload and not args.bucket:
                raise ValueError("Local cleanup requires S3 upload")
            stage = "snapshot-verification"
            path, manifest = create(args.portal, args.runtime, args.config, args.output, backup_key,
                                    portal_key, args.work_dir, args.sqlite_timeout)
            result = {"backup": str(path), "sha256": sha(path), "counts": manifest["counts"], "local_verified": True}
            if args.bucket:
                import boto3
                stage = "s3-upload-readback"
                result["s3"] = upload(path, args.bucket, args.prefix, boto3.client("s3", region_name=args.region))
            if args.remove_local_after_upload:
                stage = "local-cleanup"
                path.unlink()
                result["local_ciphertext_removed"] = True
            print(json.dumps(result))
        elif args.command == "verify":
            print(json.dumps({"local_verified": True, "counts": verify(args.backup, backup_key, portal_key, args.work_dir)["counts"]}))
        else:
            print(json.dumps(restore(args.backup, args.destination, backup_key, portal_key, args.work_dir)))
    except Exception as exc:
        print("ORION_RECOVERY_STAGE=" + stage, file=__import__("sys").stderr)
        # SDK, SQL and crypto exceptions may contain private paths or values. No traceback/secret logging.
        raise SystemExit("Recovery operation failed (" + type(exc).__name__ + "); consult the runbook. No success receipt issued.") from None


if __name__ == "__main__":
    main()
