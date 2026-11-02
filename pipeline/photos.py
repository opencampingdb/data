"""Keep the photo buckets in step with the photo records on main.

Usage:
    python -m pipeline.photos --root . [--dry-run]

Travellers' photos are uploaded by the feedback intake to the private
ocdb-photos-pending bucket as <id>/full.jpg and <id>/thumb.jpg. The photo
server serves the public ocdb-photos bucket. This makes ocdb-photos hold
exactly the visible photos in photos/*.ndjson (not hidden, not from a blocked
contributor):

- a visible photo missing from ocdb-photos is copied there from the pending
  bucket (from <id>/ or, for a photo shown again, kept/<id>/)
- a photo in ocdb-photos that is no longer visible is deleted from it, after
  a copy is kept in the pending bucket under kept/<id>/ while its record stays
- every photo on moderation/removed_photos.json is deleted from both buckets

It talks to R2 through its S3 API with the AWS CLI, using R2_ACCOUNT_ID,
R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY from the environment. Exits 1 when
a visible photo has no file to copy.
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .common import iter_photo_files, is_visible, load_blocked, load_removed_photos

SERVING = "ocdb-photos"
PENDING = "ocdb-photos-pending"
SIZES = ("full", "thumb")
KEPT = "kept/"


def photo_states(root: Path) -> tuple[set[str], set[str]]:
    """(visible, not visible) photo ids in photos/*.ndjson."""
    blocked = load_blocked(root)
    visible: set[str] = set()
    other: set[str] = set()
    for path in iter_photo_files(root):
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                photo = json.loads(line)
                (visible if is_visible(photo, blocked) else other).add(photo["id"])
    return visible, other


def photo_id(key: str) -> str:
    """The photo id in a bucket key: pho_.../full.jpg or kept/pho_.../full.jpg."""
    return key.removeprefix(KEPT).split("/", 1)[0]


@dataclass
class Plan:
    copies: list[tuple[str, str, str, str]] = field(default_factory=list)
    """(from bucket, from key, to bucket, to key), in order."""
    deletes: dict[str, list[str]] = field(default_factory=dict)
    """Bucket to keys, done after every copy."""
    missing: list[str] = field(default_factory=list)
    """Keys of visible photos with no file to copy."""

    def delete(self, bucket: str, key: str) -> None:
        keys = self.deletes.setdefault(bucket, [])
        if key not in keys:
            keys.append(key)


def plan(visible: set[str], other: set[str], removed: set[str], serving: set[str], pending: set[str]) -> Plan:
    """What to copy and delete so the buckets match the records."""
    out = Plan()
    for pid in sorted(visible - removed):
        for size in SIZES:
            key = f"{pid}/{size}.jpg"
            if key in serving:
                continue
            if key in pending:
                out.copies.append((PENDING, key, SERVING, key))
            elif KEPT + key in pending:
                out.copies.append((PENDING, KEPT + key, SERVING, key))
            else:
                out.missing.append(key)
    for key in sorted(serving):
        pid = photo_id(key)
        if pid in visible and pid not in removed:
            continue
        if pid in other and pid not in removed and KEPT + key not in pending and key not in pending:
            out.copies.append((SERVING, key, PENDING, KEPT + key))
        out.delete(SERVING, key)
    for key in sorted(pending):
        if photo_id(key) in removed:
            out.delete(PENDING, key)
    return out


class R2:
    """The few S3 calls this needs, through the AWS CLI."""

    def __init__(self, account: str):
        self.endpoint = f"https://{account}.r2.cloudflarestorage.com"
        self.env = {
            **os.environ,
            "AWS_ACCESS_KEY_ID": os.environ["R2_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["R2_SECRET_ACCESS_KEY"],
            "AWS_DEFAULT_REGION": "auto",
            # R2 does not take the checksums newer AWS CLI versions add by default.
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        }

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["aws", "s3api", *args, "--endpoint-url", self.endpoint, "--output", "json"],
            env=self.env, check=True, capture_output=True, text=True,
        )
        return result.stdout

    def keys(self, bucket: str) -> set[str]:
        out: set[str] = set()
        token = None
        while True:
            args = ["list-objects-v2", "--bucket", bucket, "--max-items", "100000"]
            if token:
                args += ["--starting-token", token]
            page = json.loads(self._run(*args) or "{}")
            out.update(obj["Key"] for obj in page.get("Contents", []))
            token = page.get("NextToken")
            if not token:
                return out

    def copy(self, from_bucket: str, from_key: str, to_bucket: str, to_key: str) -> None:
        self._run("copy-object", "--copy-source", f"{from_bucket}/{from_key}", "--bucket", to_bucket, "--key", to_key)

    def delete(self, bucket: str, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            batch = {"Objects": [{"Key": k} for k in keys[start:start + 1000]], "Quiet": True}
            self._run("delete-objects", "--bucket", bucket, "--delete", json.dumps(batch))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without changing the buckets")
    args = parser.parse_args(argv)
    root = Path(args.root)

    account = os.environ.get("R2_ACCOUNT_ID")
    if not account:
        print("R2_ACCOUNT_ID is not set, so there are no buckets to update", file=sys.stderr)
        return 0
    r2 = R2(account)
    visible, other = photo_states(root)
    todo = plan(visible, other, load_removed_photos(root), r2.keys(SERVING), r2.keys(PENDING))

    for copy in todo.copies:
        print(f"copy {copy[0]}/{copy[1]} to {copy[2]}/{copy[3]}")
        if not args.dry_run:
            r2.copy(*copy)
    for bucket, keys in todo.deletes.items():
        for key in keys:
            print(f"delete {bucket}/{key}")
        if not args.dry_run:
            r2.delete(bucket, keys)
    for key in todo.missing:
        print(f"::error::no file for visible photo {key}", file=sys.stderr)
    print(f"{len(visible)} visible photos, {len(todo.copies)} copies, {sum(len(k) for k in todo.deletes.values())} deletes")
    return 1 if todo.missing else 0


if __name__ == "__main__":
    sys.exit(main())
