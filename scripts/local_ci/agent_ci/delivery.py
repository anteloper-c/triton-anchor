"""Small immutable results and independently retryable Gitee Release evidence.

API contract: https://gitee.com/sdk/gitee5j/blob/main/docs/RepositoriesApi.md
Production attachment permissions, quotas and deletion still require deployment acceptance.
"""

from __future__ import annotations

import gzip
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .protocol import ContractError, canonical, within

DELIVERY_SCHEMA = "triton-anchor-delivery/v1"
EXECUTIONS_SCHEMA = "triton-anchor-executions/v1"
MAX_ATTACHMENT = 32 * 1024 * 1024
MAX_SMALL_JSON = 2 * 1024 * 1024
LOG_EXCERPT = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".log",
    ".txt",
    ".json",
    ".jsonl",
    ".xml",
    ".md",
    ".rst",
    ".csv",
    ".tsv",
    ".py",
    ".sh",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".ll",
    ".mlir",
}


class DeliveryPending(RuntimeError):
    """The sealed test outcome is unchanged; only delivery needs another attempt."""


@contextmanager
def delivery_lock(run_directory: Path):
    """Publication and the independent retention timer share one index writer."""
    import fcntl

    with (Path(run_directory) / ".delivery.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_artifacts(
    directory: Path,
    records: list[dict],
    redact=None,
    *,
    max_attachment: int = MAX_ATTACHMENT,
) -> list[dict]:
    """Export bounded gzip evidence, preserving full original logs on the host.

    Large logs publish a labelled first/last excerpt. Other necessary reports are
    compressed and split into ordered parts. Known secrets are removed from
    textual exports when a redactor is supplied; host originals are unchanged.
    Wheels are optional and
    omitted when too large. No publication limit can make a log retry forever.
    """
    directory = Path(directory)
    manifest = []
    for record in records:
        source = Path(record.get("artifact_dir", ""))
        files = (
            [
                (file, file.relative_to(source).as_posix())
                for file in sorted(source.rglob("*"))
            ]
            if record.get("artifact_dir") and source.is_dir()
            else []
        )
        log = Path(record.get("log_path", ""))
        if (
            record.get("log_path")
            and not record.get("source_execution_id")
            and log.is_file()
        ):
            files.append((log, "command.log"))
        execution_id = record["execution_id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", execution_id):
            raise ContractError("Invalid artifact execution id")
        for file, relative in files:
            if file.is_symlink():
                raise ContractError("Evidence cannot contain symlinks")
            if not file.is_file():
                continue
            origin = (
                "observed-log"
                if record.get("log_path") and file == log
                else "tool-artifact"
            )
            logical = f"{execution_id}/{origin}/{relative}"
            artifact_id = hashlib.sha256(logical.encode()).hexdigest()
            original_size = file.stat().st_size
            optional = file.suffix.lower() in {".whl", ".so", ".a", ".o"}
            textual = file.suffix.lower() in TEXT_SUFFIXES

            def public_bytes(value):
                if redact is not None and textual:
                    return redact(value.decode("utf-8", errors="replace")).encode(
                        "utf-8"
                    )
                return value

            common = {
                "artifact_id": artifact_id,
                "execution_id": execution_id,
                "source_path": redact(relative) if redact else relative,
                "source_size": original_size,
                "required": not optional,
                "text_redaction_applied": redact is not None and textual,
            }
            if optional and original_size > max_attachment:
                manifest.append(
                    {**common, "omitted": "optional artifact exceeds attachment budget"}
                )
                continue
            destination = directory / "artifacts" / execution_id
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile() as packed:
                omitted = 0
                with (
                    gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as zipped,
                    file.open("rb") as incoming,
                ):
                    if (
                        file.suffix.lower() in {".log", ".txt"}
                        and original_size > LOG_EXCERPT
                    ):
                        half = LOG_EXCERPT // 2
                        excerpt = incoming.read(half)
                        omitted = original_size - LOG_EXCERPT
                        excerpt += f"\n[local-ci: omitted {omitted} middle bytes; full log retained on CI host]\n".encode()
                        incoming.seek(-half, os.SEEK_END)
                        excerpt += incoming.read(half)
                        zipped.write(public_bytes(excerpt))
                    elif redact is not None and textual:
                        zipped.write(public_bytes(incoming.read()))
                    else:
                        shutil.copyfileobj(incoming, zipped, 1024 * 1024)
                packed.seek(0, os.SEEK_END)
                packed_size = packed.tell()
                parts = max(1, (packed_size + max_attachment - 1) // max_attachment)
                packed.seek(0)
                for part in range(parts):
                    path = destination / f"{artifact_id}.gz.part{part + 1:03d}"
                    with path.open("wb") as part_stream:
                        part_stream.write(packed.read(max_attachment))
                        part_stream.flush()
                        os.fsync(part_stream.fileno())
                    manifest.append(
                        {
                            **common,
                            "artifact_id": f"{artifact_id}.{part + 1}",
                            "path": path.relative_to(directory).as_posix(),
                            "sha256": file_digest(path),
                            "size": path.stat().st_size,
                            "encoding": "gzip",
                            "part": part + 1,
                            "parts": parts,
                            "omitted_bytes": omitted,
                        }
                    )
    return manifest


class GiteeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        allowed = ("gitee.com", "giteeusercontent.com")
        if parsed.scheme != "https" or not any(
            parsed.hostname == host or (parsed.hostname or "").endswith("." + host)
            for host in allowed
        ):
            raise ContractError("Release download redirected outside Gitee")
        if parsed.netloc != urlsplit(req.full_url).netloc:
            # Do not forward the Gitee API credential to attachment/CDN hosts.
            query = "&".join(
                part
                for part in parsed.query.split("&")
                if unquote(part.split("=", 1)[0]) != "access_token"
            )
            newurl = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if parsed.netloc != urlsplit(req.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GiteeReleaseClient:
    """Documented v5 Release API, with query-before-upload and verified content."""

    def __init__(self, repository_url: str, token: str | None = None):
        parsed = urlsplit(repository_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "gitee.com"
            or parsed.username
            or parsed.password
        ):
            raise ContractError(
                "Release storage must be a credential-free HTTPS Gitee repository"
            )
        repository = parsed.path.strip("/").removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ContractError("Invalid Gitee Release repository")
        self.base = f"https://gitee.com/api/v5/repos/{repository}"
        self.token = token if token is not None else os.getenv("GITEE_TOKEN", "")
        self.opener = build_opener(GiteeRedirects())

    def request(
        self,
        path: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str = "application/json",
        *,
        binary: bool = False,
    ):
        endpoint = path.split("?", 1)[0]
        if method in {"GET", "DELETE"} and self.token:
            path += ("&" if "?" in path else "?") + urlencode(
                {"access_token": self.token}
            )
        elif data is not None and content_type == "application/json":
            data = canonical({**json.loads(data), "access_token": self.token})
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": content_type,
                "User-Agent": "triton-anchor-local-ci",
            },
        )
        try:
            with self.opener.open(request, timeout=90) as response:
                body = response.read(
                    MAX_ATTACHMENT + 1 if binary else MAX_SMALL_JSON + 1
                )
                if len(body) > (MAX_ATTACHMENT if binary else MAX_SMALL_JSON):
                    raise ContractError(
                        "Gitee response exceeds configured evidence limit"
                    )
                return body if binary else (json.loads(body) if body else None)
        except HTTPError as error:
            # Never include token-bearing requests or untrusted response text.
            if error.code == 404 and method in {"GET", "DELETE"}:
                return None
            raise DeliveryPending(
                f"Gitee Release {method} {endpoint} failed with HTTP {error.code}"
            ) from None
        except OSError:
            raise DeliveryPending(
                f"Gitee Release {method} {endpoint} request failed"
            ) from None

    def release(self, task: dict, run_id: str, result_digest: str) -> dict:
        tag = f"local-ci/{task['task_id']}/{run_id}"
        identity = (
            f"Local CI task={task['task_id']} run={run_id} result={result_digest}"
        )
        path = "/releases/tags/" + quote(tag, safe="")
        release = self.request(path)
        if release is None:
            try:
                release = self.request(
                    "/releases",
                    "POST",
                    canonical(
                        {
                            "tag_name": tag,
                            "name": f"Local CI {task['task_id'][:12]} {run_id}",
                            "body": identity,
                            "target_commitish": task["tested_sha"],
                            "prerelease": True,
                        }
                    ),
                )
            except (OSError, DeliveryPending):
                # A response may have been lost after the server accepted the create.
                release = self.request(path)
                if release is None:
                    raise
        if release.get("body") != identity or not release.get("id"):
            raise ContractError(
                "Existing Gitee Release does not match the sealed result"
            )
        return release

    def attachments(self, release_id: int) -> list[dict]:
        rows = []
        for page in range(1, 101):
            batch = self.request(
                f"/releases/{release_id}/attach_files?per_page=100&page={page}"
            )
            if not isinstance(batch, list):
                raise ContractError("Invalid Gitee attachment listing")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise DeliveryPending("Release attachment listing exceeded pagination budget")

    def upload(self, release_id: int, name: str, path: Path) -> dict:
        boundary = "localci" + os.urandom(16).hex()
        payload = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="access_token"\r\n\r\n{self.token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        payload += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            f"/releases/{release_id}/attach_files",
            "POST",
            payload,
            "multipart/form-data; boundary=" + boundary,
        )

    def verified(self, release_id: int, attachment: dict, expected: dict) -> bool:
        data = self.request(
            f"/releases/{release_id}/attach_files/{attachment['id']}/download",
            binary=True,
        )
        return (
            data is not None
            and len(data) == expected["size"]
            and hashlib.sha256(data).hexdigest() == expected["sha256"]
        )

    def delete(self, release_id: int, attachment_id: int) -> None:
        self.request(f"/releases/{release_id}/attach_files/{attachment_id}", "DELETE")


def publish_evidence(
    client,
    task: dict,
    run_id: str,
    directory: Path,
    result: dict,
    result_digest: str,
    saved: dict | None = None,
    *,
    optional_only: bool = False,
) -> dict:
    """Return delivery facts even when upload fails; caller can publish pending."""
    previous = saved if saved and saved.get("result_digest") == result_digest else {}
    if optional_only and not previous:
        raise ContractError(
            "Optional retries require an existing matching delivery index"
        )
    if optional_only and (
        previous.get("status") != "ready"
        or not any(
            row.get("required") is False and row.get("status") == "pending"
            for row in previous.get("artifacts", [])
        )
    ):
        return previous
    index = {
        "schema": DELIVERY_SCHEMA,
        "task_id": task["task_id"],
        "run_id": run_id,
        "result_digest": result_digest,
        "status": "pending",
        "artifacts": [],
    }
    artifacts = result.get("artifacts", [])
    prior = {row["artifact_id"]: row for row in previous.get("artifacts", [])}
    release, attachments, release_error = None, [], ""

    def selected(row):
        return not row.get("omitted") and (
            not optional_only
            or (
                not row["required"]
                and prior.get(row["artifact_id"], {}).get("status") == "pending"
            )
        )

    if any(selected(row) for row in artifacts):
        try:
            if client is None:
                raise DeliveryPending("Configure Gitee Release attachment storage")
            release = client.release(task, run_id, result_digest)
            attachments = client.attachments(release["id"])
        except (OSError, RuntimeError) as error:
            release_error = type(error).__name__ + ": attachment service unavailable"
    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        if optional_only and not selected(artifact):
            index["artifacts"].append(dict(prior[artifact_id]))
            continue
        entry = {key: artifact[key] for key in ("artifact_id", "required")}
        if artifact.get("omitted"):
            entry.update(status="omitted", reason=artifact["omitted"])
        else:
            entry.update(
                sha256=artifact["sha256"], size=artifact["size"], status="pending"
            )
            old = prior.get(artifact_id, {})
            if old.get("status") == "expired":
                entry.update(old, status="expired", reason="retention_expired")
            elif (
                release_error
                and old.get("status") == "ready"
                and old.get("verified_sha256") == artifact["sha256"]
            ):
                # Optional retries cannot erase previously confirmed delivery facts.
                entry.update(old)
            elif release_error:
                entry["reason"] = release_error
            else:
                try:
                    path = within(directory, artifact["path"], must_exist=True)
                    if (
                        path.stat().st_size != artifact["size"]
                        or file_digest(path) != artifact["sha256"]
                    ):
                        raise ContractError("Sealed artifact content changed")
                    if artifact["size"] > MAX_ATTACHMENT:
                        raise ContractError("Sealed artifact exceeds attachment limit")
                    name = f"{artifact_id}-{artifact['sha256'][:16]}.bin"
                    matching = [
                        row
                        for row in attachments
                        if row.get("name", row.get("file_name")) == name
                    ]
                    attachment = next(
                        (
                            row
                            for row in matching
                            if row.get("id") == old.get("attachment_id")
                        ),
                        None,
                    )
                    already_verified = bool(
                        attachment
                        and old.get("status") == "ready"
                        and old.get("sha256") == artifact["sha256"]
                    )
                    if attachment is None:
                        attachment = next(iter(matching), None)
                    if attachment is None:
                        try:
                            attachment = client.upload(release["id"], name, path)
                            attachments.append(attachment)
                        except (OSError, RuntimeError):
                            attachments = client.attachments(release["id"])
                            attachment = next(
                                (
                                    row
                                    for row in attachments
                                    if row.get("name", row.get("file_name")) == name
                                ),
                                None,
                            )
                            if attachment is None:
                                raise
                    if not already_verified and not client.verified(
                        release["id"], attachment, artifact
                    ):
                        raise ContractError("Uploaded attachment hash/size mismatch")
                    entry.update(
                        status="ready",
                        release_id=release["id"],
                        attachment_id=attachment["id"],
                        url=attachment.get("browser_download_url", ""),
                        verified_sha256=artifact["sha256"],
                    )
                except (OSError, RuntimeError, ContractError) as error:
                    entry["reason"] = (
                        "Attachment hash/identity validation failed"
                        if isinstance(error, ContractError)
                        else type(error).__name__ + ": attachment upload will retry"
                    )
        index["artifacts"].append(entry)
    if all(row["status"] == "ready" for row in index["artifacts"] if row["required"]):
        index["status"] = "ready"
    return index
