"""Off-site copies of database dumps, in an S3-compatible bucket.

    python -m scripts.backup_offsite put NAME < dumpfile
    python -m scripts.backup_offsite prune --days 14
    python -m scripts.backup_offsite get NAME > dumpfile

Run inside the API container by `scripts/backup.sh`, which streams the dump in on
standard input, so the dump never has to be copied into the container.

Configured with `BACKUP_BUCKET`, and optionally `BACKUP_ENDPOINT_URL`,
`BACKUP_ACCESS_KEY_ID` and `BACKUP_SECRET_ACCESS_KEY` — separate from the media
bucket's keys, so the credentials that serve images cannot read backups, and a
leaked backup key cannot touch media. The bucket must be private.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

PREFIX = "db/"


def _client() -> tuple[Any, str]:
    import boto3

    bucket = os.environ.get("BACKUP_BUCKET", "")
    if not bucket:
        raise SystemExit("BACKUP_BUCKET is not set")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("BACKUP_ENDPOINT_URL") or None,
        aws_access_key_id=os.environ.get("BACKUP_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("BACKUP_SECRET_ACCESS_KEY") or None,
        region_name=os.environ.get("BACKUP_REGION", "auto"),
    )
    return client, bucket


def put(name: str) -> None:
    client, bucket = _client()
    client.upload_fileobj(sys.stdin.buffer, bucket, PREFIX + name)
    sys.stdout.write(f"offsite: uploaded {name}\n")


def get(name: str) -> None:
    client, bucket = _client()
    client.download_fileobj(bucket, PREFIX + name, sys.stdout.buffer)


def prune(days: int) -> None:
    client, bucket = _client()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    paginator = client.get_paginator("list_objects_v2")
    removed = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
        for item in page.get("Contents", []):
            if item["LastModified"] < cutoff:
                client.delete_object(Bucket=bucket, Key=item["Key"])
                removed += 1
    sys.stdout.write(f"offsite: pruned {removed}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Off-site database dumps.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("put").add_argument("name")
    commands.add_parser("get").add_argument("name")
    commands.add_parser("prune").add_argument("--days", type=int, required=True)
    args = parser.parse_args()
    if args.command == "put":
        put(args.name)
    elif args.command == "get":
        get(args.name)
    else:
        prune(args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
