"""CI artifact store on Cloudflare R2, replacing GitHub Actions artifact storage.

GitHub blocked every artifact upload on this account on Fri 30 Oct 2026: on Free and Pro,
artifacts and Packages share a 0.5 to 1 GB pool billed in GB-hours over the month, so
deleting artifacts does not unblock it. This module moves every artifact to R2 instead.

Requirements:
- upload: tar.gz every file matched by newline-separated globs, rooted at their least common
  ancestor (upload-artifact parity), and PUT it to `ci/<repo>/<run_id>/<name>.tar.gz`, or to
  `ci/<repo>/shared/<name>.tar.gz` with --shared (cross-run lookups keyed by SHA). ✔︎
  [if] a glob matches nothing and --if-no-files-found=error [then ⛔️ exit 1]
  [if] the PUT returns non-2xx [then ⛔️ exit 1, never a warning]
- download: GET the object and extract it into --dest. ✔︎
  [if] the object is absent [then ⛔️ exit 1 naming the key]
- exists: exit 0 when present, 3 when absent, 1 on any other failure (never "absent"). ✔︎
  [if] credentials are wrong [then ⛔️ exit 1, not 3]
- Credentials come from env CI_ARTIFACT_R2_ACCOUNT_ID, CI_ARTIFACT_R2_ACCESS_KEY_ID, CI_ARTIFACT_R2_SECRET_ACCESS_KEY, CI_ARTIFACT_R2_BUCKET;
  a missing one fails loudly. Stdlib only (SigV4 by hand) so it runs on every runner,
  hosted or self-hosted, macOS, Linux or Windows, without the AWS CLI. ✔︎
- Retention: 14-day lifecycle rule `ci-14d` on prefix `ci/`, set on the bucket. ✔︎
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import hmac
import http.client
import io
import os
import sys
import tarfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PREFIX = "ci"
UNSIGNED = "UNSIGNED-PAYLOAD"
EXIT_ABSENT = 3
TIMEOUT_S = 300


class R2Error(RuntimeError):
    pass


# ----------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Creds:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @property
    def host(self) -> str:
        return f"{self.account_id}.r2.cloudflarestorage.com"


def _creds_from_env() -> Creds:
    names = (
        "CI_ARTIFACT_R2_ACCOUNT_ID",
        "CI_ARTIFACT_R2_ACCESS_KEY_ID",
        "CI_ARTIFACT_R2_SECRET_ACCESS_KEY",
        "CI_ARTIFACT_R2_BUCKET",
    )
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise R2Error(f"R2 credentials unset: {', '.join(missing)} (repo secrets ARTIFACT_ARCHIVE_R2_*)")
    return Creds(*(os.environ[n] for n in names))


def object_key(repo: str, name: str, run_id: str | None) -> str:
    scope = run_id if run_id else "shared"
    return f"{PREFIX}/{repo}/{scope}/{name}.tar.gz"


# ----------------------------------------------------------------------------- sigv4


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signed_headers(creds: Creds, method: str, path: str, now: dt.datetime) -> dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    headers = {"host": creds.host, "x-amz-content-sha256": UNSIGNED, "x-amz-date": amz_date}
    names = ";".join(sorted(headers))
    canonical = "\n".join([method, path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", names, UNSIGNED])
    scope = f"{day}/auto/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_access_key}".encode(), day), "auto"), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, SignedHeaders={names}, Signature={signature}"
    )
    return headers


def _request(creds: Creds, method: str, key: str, body: bytes | None = None) -> tuple[int, bytes]:
    path = "/" + urllib.parse.quote(f"{creds.bucket}/{key}", safe="/-_.~")
    headers = _signed_headers(creds, method, path, dt.datetime.now(dt.timezone.utc))  # noqa: UP017 - dt.UTC is 3.11+, runners may run 3.9
    conn = http.client.HTTPSConnection(creds.host, timeout=TIMEOUT_S)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# ----------------------------------------------------------------------------- tar


def _matched_files(patterns: list[str], include_hidden: bool) -> tuple[list[Path], Path | None]:
    """Matched files plus their archive root: the least common ancestor of every matched
    directory and every matched file's parent, as upload-artifact roots them."""
    files: set[Path] = set()
    anchors: set[Path] = set()
    for pattern in patterns:
        for hit in glob.glob(pattern, recursive=True):
            p = Path(hit).resolve()
            if p.is_dir():
                anchors.add(p)
                files.update(f for f in p.rglob("*") if f.is_file() and (include_hidden or not _is_hidden(f, p)))
            elif p.is_file():
                anchors.add(p.parent)
                files.add(p)
    root = Path(os.path.commonpath([str(a) for a in anchors])) if files else None
    return sorted(files), root


def _is_hidden(f: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in f.relative_to(root).parts)


def _tarball(files: list[Path], root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            tar.add(f, arcname=f.relative_to(root).as_posix())
    return buf.getvalue()


# ----------------------------------------------------------------------------- commands


def upload(creds: Creds, key: str, patterns: list[str], include_hidden: bool, if_no_files: str) -> str:
    files, root = _matched_files(patterns, include_hidden)
    if root is None:
        msg = f"no files matched {patterns}"
        if if_no_files == "error":
            raise R2Error(msg)
        if if_no_files == "warn":
            print(f"::warning title=r2-artifact::{msg}; nothing uploaded")
            return "skipped"
        if if_no_files == "ignore":
            return "skipped"
        raise R2Error(f"unknown if-no-files-found value {if_no_files!r}")
    blob = _tarball(files, root)
    status, body = _request(creds, "PUT", key, blob)
    if not 200 <= status < 300:
        raise R2Error(f"PUT {key} returned {status}: {body[:500].decode(errors='replace')}")
    print(f"[r2-artifact] uploaded {len(files)} file(s), {len(blob)} bytes -> {creds.bucket}/{key}")
    return "uploaded"


def fetch(creds: Creds, key: str) -> bytes:
    status, body = _request(creds, "GET", key)
    if status == 404:
        raise R2Error(f"{creds.bucket}/{key} does not exist")
    if not 200 <= status < 300:
        raise R2Error(f"GET {key} returned {status}: {body[:500].decode(errors='replace')}")
    return body


def download(creds: Creds, key: str, dest: Path) -> None:
    blob = fetch(creds, key)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)  # runner Python predates extraction filters (< 3.11.4)
    print(f"[r2-artifact] extracted {creds.bucket}/{key} ({len(blob)} bytes) -> {dest}")


def exists(creds: Creds, key: str) -> bool:
    status, _ = _request(creds, "HEAD", key)
    if status == 404:
        return False
    if 200 <= status < 300:
        return True
    raise R2Error(f"HEAD {key} returned {status}")


def read_member(creds: Creds, key: str, member: str) -> bytes:
    """One file out of an artifact tarball, for cross-run readers."""
    with tarfile.open(fileobj=io.BytesIO(fetch(creds, key)), mode="r:gz") as tar:
        try:
            extracted = tar.extractfile(member)
        except KeyError as exc:
            raise R2Error(f"{key} has no {member}") from exc
        if extracted is None:
            raise R2Error(f"{key}:{member} is not a file")
        return extracted.read()


# ----------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("upload", "download", "exists"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--shared", action="store_true", help="key by name only, not run id")
    parser.add_argument("--path", default="", help="newline-separated globs (upload)")
    parser.add_argument("--dest", type=Path, default=Path("."), help="extract dir (download)")
    parser.add_argument("--include-hidden-files", default="false")
    parser.add_argument("--if-no-files-found", default="error", choices=("error", "warn", "ignore"))
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if not args.shared and not args.run_id:
        parser.error("--run-id or GITHUB_RUN_ID is required unless --shared")
    key = object_key(args.repo, args.name, None if args.shared else args.run_id)
    try:
        creds = _creds_from_env()
        if args.mode == "upload":
            patterns = [line.strip() for line in args.path.splitlines() if line.strip()]
            if not patterns:
                parser.error("--path is required for upload")
            upload(creds, key, patterns, args.include_hidden_files.lower() == "true", args.if_no_files_found)
        elif args.mode == "download":
            download(creds, key, args.dest)
        elif args.mode == "exists":
            return 0 if exists(creds, key) else EXIT_ABSENT
    except (R2Error, OSError) as exc:
        print(f"::error title=r2-artifact::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
