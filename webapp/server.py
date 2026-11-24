"""Local page for bulk offline license requests.

Run from the repository root, with the same virtualenv as the command line:

    python webapp/server.py

The page is served from this folder only. Chassis calls go through
bulk_offline_lic and do not use HTTP_PROXY. Credentials stay in the request
and are not written to crews.txt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bulk_offline_lic as lic  # noqa: E402

STATIC = Path(__file__).resolve().parent
WORK = STATIC / "work"
MAX_BODY = 32 * 1024 * 1024
MAX_CONCURRENCY = 64
STATIC_SUFFIXES = {".html", ".css", ".js", ".woff2", ".txt"}
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
ZIP_NAME = re.compile(r"^offline-requests-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\.zip$")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    report: str
    rows: list[dict]
    problems: list[str]
    archive_name: str | None = None


def safe_relative(name: str) -> Path | None:
    """A relative path with no parent segments, or None when it is unsafe."""
    if not name or "\x00" in name:
        return None
    raw = name.replace("\\", "/")
    if raw.startswith("/"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return Path(*parts)


def parse_concurrency(value: object) -> int:
    if value is None or value == "":
        return lic.DEFAULT_CONCURRENCY
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("concurrency must be a whole number from 1 to 64")
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError("concurrency must be a whole number from 1 to 64")
    number = int(text)
    if number < 1 or number > MAX_CONCURRENCY:
        raise ValueError("concurrency must be a whole number from 1 to 64")
    return number


def _redact(text: str, passwords: list[str]) -> str:
    for password in passwords:
        if password:
            text = text.replace(password, "***")
    return text


def _scrub(outcome: Outcome, passwords: list[str]) -> Outcome:
    if not any(passwords):
        return outcome
    return Outcome(
        outcome.exit_code,
        _redact(outcome.report, passwords),
        [
            {key: _redact(str(value), passwords) if isinstance(value, str) else value for key, value in row.items()}
            for row in outcome.rows
        ],
        [_redact(item, passwords) for item in outcome.problems],
        outcome.archive_name,
    )


def _has_license_jobs(path: Path) -> bool:
    found = lic.licenses_in_ip_folders(path)
    return found is not None and bool(found[0])


def find_license_root(saved: Path) -> Path:
    """The directory whose children are chassis address folders.

    A browser folder upload includes the chosen folder's own name as the first
    path segment. That name can look like a hostname, so a wrapper counts only
    when a directory actually holds received license files.
    """
    if _has_license_jobs(saved):
        return saved
    children = [item for item in saved.iterdir() if item.is_dir() and not item.name.startswith(".")]
    holders = [child for child in children if _has_license_jobs(child)]
    if len(holders) == 1:
        return holders[0]
    if len(children) == 1:
        return children[0]
    return saved


def download_requests(
    crews: str,
    concurrency: int,
    dest: Path,
    *,
    transport: object | None = None,
    retry_delay: float = 0.5,
    now: datetime | None = None,
) -> Outcome:
    chassis, problems = lic.parse_crews(crews)
    passwords = [item.password for item in chassis]
    outcome = asyncio.run(
        lic.run(
            chassis,
            dest,
            parse_errors=problems,
            concurrency=concurrency,
            transport=transport,
            now=now,
            retry_delay=retry_delay,
        )
    )
    archive = outcome.zip_path.name if outcome.zip_path is not None else None
    rows = [
        {
            "address": item.address,
            "file": item.filename if item.ok and item.filename else item.detail,
            "ok": item.ok,
        }
        for item in outcome.results
    ]
    return _scrub(Outcome(outcome.exit_code, outcome.report, rows, problems, archive), passwords)


def license_path(value: object) -> Path:
    """The folder or license file on this machine, the same path the command line takes."""
    if not isinstance(value, str):
        raise ValueError("Give the absolute path of the license folder.")
    text = value.strip()
    if not text:
        raise ValueError("Give the absolute path of the license folder.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError("The license folder must be an absolute path.")
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ValueError("That path is not on this machine.")
    if not resolved.is_dir() and not resolved.is_file():
        raise ValueError("That path is not a folder or a license file.")
    return resolved


def install_folder(
    crews: str,
    concurrency: int,
    folder: Path,
    *,
    transport: object | None = None,
    retry_delay: float = 0.5,
) -> Outcome:
    root = folder if folder.is_file() else find_license_root(folder)
    expanded = lic._license_paths([root])
    if isinstance(expanded, str):
        return Outcome(2, expanded, [], [])
    selected, problems = expanded
    problems = list(problems)
    licenses: list[lic.LicenseFile] = []
    for address, path in selected:
        try:
            licenses.append(lic.load_license(path, address))
        except (OSError, ValueError) as exc:
            label = f"{address}/{path.name}" if address else path.name
            problems.append(f"{label}: {exc}")
    chassis, errors = lic.parse_crews(crews)
    passwords = [item.password for item in chassis]
    problems.extend(errors)
    bound = {item.address for item in licenses if item.address}
    if bound:
        known = {item.address for item in chassis}
        missing = sorted(address for address in bound if address not in known)
        for address in missing:
            problems.append(f"{address}: no credentials in crews.txt")
        missing_set = set(missing)
        chassis = [item for item in chassis if item.address in bound]
        licenses = [item for item in licenses if item.address not in missing_set]
    outcome = asyncio.run(
        lic.run_install(
            chassis,
            licenses,
            problems=problems,
            concurrency=concurrency,
            transport=transport,
            retry_delay=retry_delay,
        )
    )
    rows = _install_rows(licenses, outcome.results)
    return _scrub(Outcome(outcome.exit_code, outcome.report, rows, problems), passwords)


def _installed_address(detail: str) -> str:
    prefix = "installed on "
    if detail.startswith(prefix):
        return detail[len(prefix):].split(" ", 1)[0].rstrip(".")
    return ""


def _install_rows(licenses: list[lic.LicenseFile], results: list[lic.InstallResult]) -> list[dict]:
    pending: dict[str, list[str | None]] = {}
    for item in licenses:
        pending.setdefault(item.name, []).append(item.address)
    rows: list[dict] = []
    for item in results:
        choices = pending.get(item.name, [])
        address = choices.pop(0) if choices else None
        if not address:
            address = _installed_address(item.detail)
        rows.append(
            {
                "address": address or "",
                "file": item.name if item.ok else item.detail,
                "ok": item.ok,
            }
        )
    return rows


def final_status(rows: list[dict], problems: list[str]) -> str:
    """Success when every row worked, Failed when none did, Partial Success otherwise."""
    succeeded = any(row.get("ok") for row in rows)
    trouble = any(not row.get("ok") for row in rows) or bool(problems)
    if succeeded and not trouble:
        return "Success"
    if succeeded:
        return "Partial Success"
    return "Failed"


def payload_of(outcome: Outcome, job_id: str | None = None) -> dict:
    archive = None
    if job_id and outcome.archive_name:
        archive = f"/api/jobs/{job_id}/{quote(outcome.archive_name)}"
    return {
        "exitCode": outcome.exit_code,
        "status": final_status(outcome.rows, outcome.problems),
        "report": outcome.report,
        "results": outcome.rows,
        "problems": outcome.problems,
        "archive": archive,
        "archiveName": outcome.archive_name,
    }


def _rows_html(outcome: Outcome, columns: list[tuple[str, str]]) -> str:
    if not outcome.rows:
        return ""
    heads = "".join(f"<th scope=\"col\">{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in outcome.rows:
        kind = "" if row.get("ok") else " class=\"row--minus\""
        cells = []
        for key, label in columns:
            value = escape(str(row.get(key, "")))
            measured = key == "address" or (key == "file" and row.get("ok"))
            klass = " class=\"measured\"" if measured else ""
            cells.append(f"<td data-label=\"{escape(label)}\"{klass}>{value}</td>")
        body.append(f"<tr{kind}>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{heads}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def result_page(heading: str, back: str, outcome: Outcome, columns: list[tuple[str, str]], archive: str | None) -> bytes:
    summary = escape(final_status(outcome.rows, outcome.problems))
    problems = ""
    if outcome.problems:
        items = "".join(f"<li>{escape(item)}</li>" for item in outcome.problems)
        problems = f"<ul class=\"problems\">{items}</ul>"
    save = ""
    if archive:
        save = f"<a class=\"btn btn--primary\" href=\"{escape(archive)}\">Save zip</a>"
    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(heading)}</title>
  <meta name="description" content="The chassis run finished on this machine.">
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <header class="bar">
    <div class="container bar__inner">
      <a class="wordmark" href="{escape(back)}">Bulk Offline License Manager</a>
      <a class="btn btn--quiet" href="{escape(back)}">Back to the form</a>
    </div>
  </header>
  <main class="container page-result">
    <h1>{escape(heading)}</h1>
    <div class="ledger" data-state="full">
      {problems}
      {_rows_html(outcome, columns)}
      <div class="ledger__head">
        <p class="ledger__status" role="status">{summary}</p>
        {save}
      </div>
    </div>
  </main>
</body>
</html>
"""
    return document.encode("utf-8")


class Service:
    def __init__(self, work: Path, *, transport: object | None = None, retry_delay: float = 0.5, now: datetime | None = None) -> None:
        self.work = work
        self.transport = transport
        self.retry_delay = retry_delay
        self.now = now
        self.work.mkdir(parents=True, exist_ok=True)
        self._sweep()

    def _sweep(self) -> None:
        cutoff = time.time() - 24 * 60 * 60
        for child in self.work.iterdir():
            if child.is_dir() and child.stat().st_mtime < cutoff:
                for item in sorted(child.rglob("*"), reverse=True):
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        item.rmdir()
                child.rmdir()

    def download(self, crews: str, concurrency: int) -> tuple[str, Outcome]:
        job_id = uuid.uuid4().hex
        dest = self.work / job_id
        dest.mkdir()
        outcome = download_requests(
            crews,
            concurrency,
            dest,
            transport=self.transport,
            retry_delay=self.retry_delay,
            now=self.now,
        )
        return job_id, outcome

    def install(self, crews: str, concurrency: int, folder: Path) -> Outcome:
        return install_folder(
            crews,
            concurrency,
            folder,
            transport=self.transport,
            retry_delay=self.retry_delay,
        )

    def archive(self, job_id: str, name: str) -> Path | None:
        if JOB_ID.fullmatch(job_id) is None or ZIP_NAME.fullmatch(name) is None:
            return None
        path = (self.work / job_id / name).resolve()
        root = (self.work / job_id).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path


def wants_json(headers) -> bool:
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        return True
    accept = headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def static_file(url_path: str) -> Path | None:
    if url_path in ("", "/"):
        url_path = "/index.html"
    relative = safe_relative(unquote(url_path).lstrip("/"))
    if relative is None:
        return None
    if relative.parts and relative.parts[0] in {"work", "fonts"} and relative.suffix == ".py":
        return None
    path = (STATIC / relative).resolve()
    if not path.is_relative_to(STATIC) or not path.is_file():
        return None
    if "work" in path.parts or path.suffix.lower() not in STATIC_SUFFIXES:
        return None
    return path


def handler_for(service: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _security(self) -> None:
            self.send_header("Content-Security-Policy", "default-src 'self'; form-action 'self'; base-uri 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > MAX_BODY:
                self._json(413, {"detail": "That upload is larger than 32 MB."})
                return None
            return self.rfile.read(length)

        def _respond(self, outcome: Outcome, *, job_id: str | None, heading: str, back: str, columns: list[tuple[str, str]]) -> None:
            payload = payload_of(outcome, job_id)
            if wants_json(self.headers):
                self._json(200, payload)
                return
            self._send(200, result_page(heading, back, outcome, columns, payload["archive"]), "text/html; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs":
                archive = service.archive(parts[2], parts[3])
                if archive is None:
                    self._json(404, {"detail": "That zip is no longer on this machine."})
                    return
                self._send(200, archive.read_bytes(), "application/zip")
                return
            path = static_file(parsed.path)
            if path is None:
                self._json(404, {"detail": "That page is not on this server."})
                return
            self._send(200, path.read_bytes(), CONTENT_TYPES[path.suffix.lower()])

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            body = self._read_body()
            if body is None:
                return
            try:
                if parsed.path == "/api/download":
                    crews, concurrency = self._download_fields(body)
                    job_id, outcome = service.download(crews, concurrency)
                    self._respond(
                        outcome,
                        job_id=job_id,
                        heading="Download Offline License Request",
                        back="/#download",
                        columns=[("address", "IP"), ("file", "offline req file name")],
                    )
                    return
                if parsed.path == "/api/install":
                    crews, concurrency, folder = self._install_fields(body)
                    outcome = service.install(crews, concurrency, folder)
                    self._respond(
                        outcome,
                        job_id=None,
                        heading="Install offline licenses",
                        back="/#install",
                        columns=[("address", "IP"), ("file", "license filename")],
                    )
                    return
            except ValueError as exc:
                message = str(exc)
                if wants_json(self.headers):
                    self._json(400, {"detail": message})
                else:
                    self._send(400, result_page(message, "/", Outcome(2, message, [], [message]), [], None), "text/html; charset=utf-8")
                return
            except Exception:
                sys.stderr.write("job failed: unexpected error\n")
                self._json(500, {"detail": "The run failed before the report was ready."})
                return
            self._json(404, {"detail": "That page is not on this server."})

        def _download_fields(self, body: bytes) -> tuple[str, int]:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() == "application/json":
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("The request was not readable JSON.") from exc
                if not isinstance(payload, dict):
                    raise ValueError("The request was not readable JSON.")
                crews = payload.get("crews", "")
                if not isinstance(crews, str):
                    raise ValueError("The chassis list was not text.")
                return crews, parse_concurrency(payload.get("concurrency"))
            fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            crews = fields.get("crews", [""])[0]
            return crews, parse_concurrency(fields.get("concurrency", [""])[0])

        def _install_fields(self, body: bytes) -> tuple[str, int, Path]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/json":
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("The request was not readable JSON.") from exc
                if not isinstance(payload, dict):
                    raise ValueError("The request was not readable JSON.")
                crews = payload.get("crews", "")
                if not isinstance(crews, str):
                    raise ValueError("The chassis list was not text.")
                return crews, parse_concurrency(payload.get("concurrency")), license_path(payload.get("license"))
            fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            crews = fields.get("crews", [""])[0]
            return crews, parse_concurrency(fields.get("concurrency", [""])[0]), license_path(fields.get("license", [""])[0])

    return Handler


def serve(service: Service, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler_for(service))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the local offline license page.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. Defaults to this machine only.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = serve(Service(WORK), args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Bulk Offline License Manager at http://{host}:{port}/", flush=True)
    print("Styles, fonts, and scripts are files in webapp/. Chassis calls do not use a proxy.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
