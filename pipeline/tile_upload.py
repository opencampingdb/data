"""Changed place geometry tiles to an S3-compatible bucket (Cloudflare R2).

    python -m pipeline.tile_upload --build build/

Reads build/changed.json from pipeline.place_geometry, puts each tile in
its "put" list from build/tiles/ and then deletes each key in its "delete"
list. Tiles that did not change are not sent, so a weekly rebuild makes
few calls. The bucket and its credentials come from the environment:

    R2_ACCOUNT_ID         the account, for https://<account>.r2.cloudflarestorage.com
    R2_GEOMETRY_BUCKET    the bucket
    R2_ACCESS_KEY_ID      an R2 API token's access key, Object Read and Write on that bucket
    R2_SECRET_ACCESS_KEY  and its secret

Any failed put or delete exits with code 1 after the rest have been tried.
A failed put stops before the deletes. Every call can be repeated, so the
next run, which diffs against the same previous tile list, finishes the job.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The most keys one DeleteObjects call takes.
DELETE_BATCH = 1000
# Puts in flight at once.
WORKERS = 32
CONTENT_TYPE = "application/x-ndjson"
TILE_KEY = re.compile(r"(water|roads)/-?\d+/-?\d+\.ndjson")
ENV = ("R2_ACCOUNT_ID", "R2_GEOMETRY_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


class UploadFailed(Exception):
    """A tile the bucket did not take, or a key that is not a tile."""


def upload(build: Path, client, bucket: str, workers: int = WORKERS) -> dict:
    """Put the changed tiles in build and delete the removed ones. Returns the counts.

    client is an S3 client (boto3's, or a fake in tests): put_object and
    delete_objects are the only calls made.
    """
    changes = json.loads((build / "changed.json").read_text())
    put, delete = list(changes["put"]), list(changes["delete"])
    bad = [key for key in put + delete if not TILE_KEY.fullmatch(key)]
    if bad:
        raise UploadFailed(f"not a tile key: {', '.join(bad[:5])}")

    def put_one(key: str) -> str | None:
        try:
            client.put_object(Bucket=bucket, Key=key, Body=(build / "tiles" / key).read_bytes(), ContentType=CONTENT_TYPE)
        except Exception as error:  # noqa: BLE001 - every failure is reported, then the upload fails
            return f"{key}: {error}"
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        failed = [message for message in pool.map(put_one, put) if message]
    if failed:
        raise UploadFailed(f"{len(failed)} of {len(put)} puts failed, nothing deleted: " + "; ".join(failed[:5]))

    failed = []
    for start in range(0, len(delete), DELETE_BATCH):
        keys = delete[start : start + DELETE_BATCH]
        try:
            answer = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True})
        except Exception as error:  # noqa: BLE001
            failed += [f"{key}: {error}" for key in keys]
            continue
        failed += [f"{item['Key']}: {item.get('Code')} {item.get('Message')}" for item in answer.get("Errors", [])]
    if failed:
        raise UploadFailed(f"{len(failed)} of {len(delete)} deletes failed: " + "; ".join(failed[:5]))
    return {"put": len(put), "delete": len(delete)}


def r2_client(account: str, key_id: str, secret: str):
    """A boto3 S3 client for R2's S3 endpoint."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(
            retries={"max_attempts": 5, "mode": "standard"},
            max_pool_connections=WORKERS,
            # R2 does not need the default checksums, so send them only where S3 requires one.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args(argv)
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        print(f"tile upload: {', '.join(missing)} not set", file=sys.stderr)
        return 1
    client = r2_client(os.environ["R2_ACCOUNT_ID"], os.environ["R2_ACCESS_KEY_ID"], os.environ["R2_SECRET_ACCESS_KEY"])
    try:
        result = upload(args.build, client, os.environ["R2_GEOMETRY_BUCKET"])
    except UploadFailed as error:
        print(f"tile upload: {error}", file=sys.stderr)
        return 1
    print(f"tile upload: put {result['put']}, deleted {result['delete']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
