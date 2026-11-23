"""Download Ixia offline license requests, and install the licenses that come back.

Download: each chassis is authenticated on its own. The response is a gzip
archive named by the chassis. The archive is kept as
offlineReq-<host>_<date>_<time>.bin.gz, and the extracted request is saved
next to it as offlineReq-<host>_<date>_<time>.bin. After every chassis has
finished, the successful folders are packed into one zip. Failed chassis keep
a failure.txt on disk and stay out of the archive.

Install: a received license file is base64 JSON locked to one host ID. The
script asks each chassis for its host ID and installs the file only on the
chassis that owns that host. Install is two calls: upload the file, then
start installofflinelicense with the licenseUuid from the upload.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote

import httpx

AUTH_PATH = "/platform/api/v1/auth/session"
SERVERS_PATH = "/platform/api/v2/licensing/servers"
UPLOAD_OPERATION = "uploadofflinecelicense"
INSTALL_OPERATION = "installofflinelicense"
HOSTID_OPERATION = "retrievehostid"
DEFAULT_FILENAME = "offlinelicenserequest.bin"
DEFAULT_INPUT = Path("crews.txt")
DEFAULT_OUTPUT = Path("out")
DEFAULT_CONCURRENCY = 8
INSTALL_TIMEOUT = 60.0
_ADDRESS = re.compile(r"^[A-Za-z0-9._:-]+$")
_HEADER_NAMES = {"ip", "<ip>"}
_RETRY_STATUSES = {502, 503, 504}
# Portal license names look like hostid-token-YYYY-MM-DD-HH-MM.bin.
# offline request downloads use a different prefix and must not be installed.
_RECEIVED_LICENSE = re.compile(
    r"^[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{8}"
    r"-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}\.bin$",
    re.IGNORECASE,
)
# Chassis: offlinelicenserequest-MM-DD-YYYY_HH-MM-SS-<host>.bin
_CHASSIS_REQUEST_NAME = re.compile(
    r"^offlinelicenserequest-"
    r"(?P<month>\d{2})-(?P<day>\d{2})-(?P<year>\d{4})_"
    r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})-"
    r"(?P<host>[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{4})"
    r"\.bin$",
    re.IGNORECASE,
)
_OK_STATES = {"SUCCESS"}
_FAIL_STATES = {"ERROR", "EXCEPTION", "TIMED_OUT", "CANCELLED", "NOT_FOUND"}
_TEXTAREA = re.compile(r"<textarea[^>]*>(.*)</textarea>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Chassis:
    address: str
    username: str
    password: str


@dataclass(frozen=True)
class ChassisResult:
    address: str
    ok: bool
    detail: str
    filename: str | None
    archive_name: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    report: str
    zip_path: Path | None
    results: list[ChassisResult]


@dataclass(frozen=True)
class LicenseFile:
    name: str
    host_id: str
    data: bytes
    address: str | None = None


@dataclass(frozen=True)
class InstallResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class InstallOutcome:
    exit_code: int
    report: str
    results: list[InstallResult]


class ChassisError(Exception):
    """A chassis failed at auth, license-server lookup, or download."""


class _CallableTransport(httpx.AsyncBaseTransport):
    """Adapt a test double that implements handle_async_request."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        handle = self._inner.handle_async_request
        response = handle(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response


def parse_crews(text: str) -> tuple[list[Chassis], list[str]]:
    """Return chassis rows and human-readable problems, in file order."""
    chassis: list[Chassis] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) < 3:
            errors.append(f"Bad row {lineno}: expected ip,username,password")
            continue
        address, username, password = parts
        if address.casefold() in _HEADER_NAMES:
            continue
        if not address or not username:
            errors.append(f"Bad row {lineno}: expected ip,username,password")
            continue
        if _ADDRESS.fullmatch(address) is None:
            errors.append(f"Bad row {lineno}: invalid address {address}")
            continue
        if address in seen:
            errors.append(f"Bad row {lineno}: duplicate chassis {address}")
            continue
        seen.add(address)
        chassis.append(Chassis(address, username, password))
    return chassis, errors


def content_disposition_filename(header: str | None) -> str:
    """Use the filename parameter, never a directory prefix from that header."""
    if not header:
        return DEFAULT_FILENAME
    raw_name: str | None = None
    try:
        message = EmailMessage()
        message["Content-Disposition"] = header
        raw_name = message.get_filename()
    except (ValueError, IndexError, KeyError):
        raw_name = None
    if not raw_name:
        return DEFAULT_FILENAME
    name = Path(unquote(raw_name).replace("\\", "/")).name
    if not name or name in {".", ".."} or "\x00" in name:
        return DEFAULT_FILENAME
    return name


def offline_request_name(chassis_filename: str) -> str:
    """Rename a chassis download to the name the licensing tool imports.

    offlinelicenserequest-11-19-2026_23-31-09-0488b0-f4c940-6afe7e-e4d7.bin
    becomes offlineReq-0488b0-f4c940-6afe7e-e4d7_2026-11-19_23-31-09.bin.
    A name that is not in the chassis pattern is returned unchanged.
    """
    match = _CHASSIS_REQUEST_NAME.fullmatch(chassis_filename)
    if match is None:
        if chassis_filename.lower().endswith(".bin"):
            return chassis_filename
        return f"{chassis_filename}.bin"
    host = match.group("host")
    date = f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
    clock = f"{match.group('hour')}-{match.group('minute')}-{match.group('second')}"
    return f"offlineReq-{host}_{date}_{clock}.bin"


def unpack_offline_request(raw: bytes) -> tuple[bytes, bytes | None]:
    """Return the request text and, when the body is gzip, the original archive."""
    if raw.startswith(b"\x1f\x8b"):
        try:
            extracted = gzip.decompress(raw)
        except OSError as exc:
            raise ValueError("response is not a valid gzip archive") from exc
        if not extracted:
            raise ValueError("gzip archive is empty")
        return extracted, raw
    return raw, None


def license_server_id(servers: object) -> int:
    """Use the first server id. An empty or unusable list means id 1."""
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and first.get("id") is not None:
            try:
                return int(first["id"])
            except (TypeError, ValueError):
                return 1
    return 1


def license_host_id(data: bytes) -> str:
    """Read hostInfo.hostID from a received offline license file.

    The chassis writes the file as one base64 line. A decoded JSON body is
    accepted too, so a file that was unwrapped by hand still works.
    """
    text = data.strip()
    if not text:
        raise ValueError("license file is empty")
    payload = _license_payload(text)
    info = payload.get("hostInfo") if isinstance(payload, dict) else None
    host = None
    if isinstance(info, dict):
        host = info.get("hostID") or info.get("hostId")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("license file missing hostID")
    return host.strip()


def _license_payload(text: bytes) -> object:
    decoded = text.decode("utf-8", "replace").strip()
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        pass
    compact = "".join(decoded.split())
    pad = "=" * ((4 - len(compact) % 4) % 4)
    try:
        raw = base64.b64decode(compact + pad, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("license file is not base64 JSON") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("license file is not base64 JSON") from exc


def load_license(path: Path, address: str | None = None) -> LicenseFile:
    data = path.read_bytes()
    return LicenseFile(path.name, license_host_id(data), data, address)


def received_license_name(name: str) -> bool:
    """A portal license file, not an offline-request download sitting beside it."""
    return _RECEIVED_LICENSE.fullmatch(name) is not None


def licenses_in_ip_folders(root: Path) -> tuple[list[tuple[str, Path]], list[str]] | None:
    """Pair each IP subfolder with every received license file, in filename order.

    Two portal responses in one folder are both kept. Filename order installs
    the lower token first, then the later one. Returns None when root is not
    a folder of chassis addresses, so a flat directory of .bin files keeps the
    older selection rules.
    """
    if not root.is_dir():
        return None
    folders = [item for item in root.iterdir() if item.is_dir() and _ADDRESS.fullmatch(item.name)]
    if not folders:
        return None
    jobs: list[tuple[str, Path]] = []
    problems: list[str] = []
    for folder in sorted(folders, key=lambda item: item.name):
        matches = sorted(
            item for item in folder.iterdir() if item.is_file() and received_license_name(item.name)
        )
        if not matches:
            problems.append(f"{folder.name}: no received license .bin")
            continue
        jobs.extend((folder.name, path) for path in matches)
    return jobs, problems


def render_report(results: list[ChassisResult], parse_errors: list[str]) -> str:
    failed = [result.address for result in results if not result.ok]
    if not failed and not parse_errors:
        return "All chassis success"
    lines = ["Partial Success"]
    if failed:
        lines.append("Failed chassis:")
        lines.extend(f"  {address}" for address in failed)
    lines.extend(parse_errors)
    return "\n".join(lines)


def render_install_report(results: list[InstallResult], problems: list[str]) -> str:
    failed = [result for result in results if not result.ok]
    if not failed and not problems:
        return "All licenses installed"
    lines = ["Partial Success"]
    if failed:
        lines.append("Failed licenses:")
        lines.extend(f"  {result.name}: {result.detail}" for result in failed)
    lines.extend(problems)
    return "\n".join(lines)


def _redact(text: str, password: str, api_key: str | None) -> str:
    if password:
        text = text.replace(password, "***")
    if api_key:
        text = text.replace(api_key, "***")
    return text


def _error_from_response(step: str, response: httpx.Response, raw: bytes, password: str) -> str:
    detail = ""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("message", "error", "statusMessage"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    detail = value
                    break
    message = f"{step}: HTTP {response.status_code} {response.reason_phrase}".rstrip()
    if detail:
        message = f"{message}: {detail}"
    return _redact(message, password, None)


def _reset_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for child in folder.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()


def _write_bytes(folder: Path, name: str, data: bytes) -> None:
    partial = folder / f".{name}.partial"
    partial.write_bytes(data)
    partial.replace(folder / name)


def _write_failure(folder: Path, message: str) -> None:
    _reset_folder(folder)
    (folder / "failure.txt").write_text(message.rstrip() + "\n", encoding="utf-8")


def _contained_file(out_dir: Path, address: str, path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    folder = (out_dir / address).resolve()
    return path.resolve().parent == folder


def _write_zip(out_dir: Path, members: list[tuple[Path, str]], now: datetime) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = f"offline-requests-{stamp}.zip"
    partial = out_dir / f".{name}.partial"
    final = out_dir / name
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in members:
            archive.write(path, arcname=arcname)
    partial.replace(final)
    return final


def finalize(
    out_dir: Path,
    results: list[ChassisResult],
    parse_errors: list[str],
    now: datetime,
) -> RunOutcome:
    """Drop any success whose file is gone, then zip only what is still on disk."""
    checked: list[ChassisResult] = []
    for result in results:
        if not result.ok or not result.filename:
            checked.append(result)
            continue
        names = [result.filename]
        if result.archive_name:
            names.append(result.archive_name)
        paths = [out_dir / result.address / name for name in names]
        if all(_contained_file(out_dir, result.address, path) for path in paths):
            checked.append(result)
            continue
        message = "download: file missing before archive"
        _write_failure(out_dir / result.address, message)
        checked.append(ChassisResult(result.address, False, message, None))

    members: list[tuple[Path, str]] = []
    for result in checked:
        if not result.ok or not result.filename:
            continue
        names = [result.filename]
        if result.archive_name:
            names.append(result.archive_name)
        for name in names:
            members.append((out_dir / result.address / name, f"{result.address}/{name}"))
    zip_path = _write_zip(out_dir, members, now) if members else None
    report = render_report(checked, parse_errors)
    exit_code = 0 if report == "All chassis success" else 1
    return RunOutcome(exit_code, report, zip_path, checked)


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    files: dict | None = None,
    attempts: int = 2,
    delay: float = 0,
) -> httpx.Response:
    last_error: Exception | None = None
    kwargs: dict = {"headers": headers}
    if files is not None:
        kwargs["files"] = files
    elif json_body is not None:
        kwargs["json"] = json_body
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(delay)
            continue
        if response.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
            await response.aclose()
            await asyncio.sleep(delay)
            continue
        return response
    raise ChassisError(f"connection failed: {last_error}")


async def _login(
    client: httpx.AsyncClient,
    base: str,
    chassis: Chassis,
    delay: float,
) -> str:
    try:
        response = await _send(
            client,
            "POST",
            f"{base}{AUTH_PATH}",
            headers={"Accept": "application/json"},
            json_body={
                "username": chassis.username,
                "password": chassis.password,
                "rememberMe": True,
                "ignorePolicy": True,
            },
            delay=delay,
        )
    except httpx.TransportError as exc:
        raise ChassisError(f"auth: connection failed: {exc}") from exc
    if response.status_code != 200:
        raise ChassisError(_error_from_response("auth", response, response.content, chassis.password))
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ChassisError("auth: response was not JSON") from exc
    api_key = payload.get("apiKey") if isinstance(payload, dict) else None
    if not isinstance(api_key, str) or not api_key:
        raise ChassisError("auth: response missing apiKey")
    return api_key


async def _server_id(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    password: str,
    delay: float,
) -> int:
    try:
        response = await _send(
            client,
            "GET",
            f"{base}{SERVERS_PATH}",
            headers={"Accept": "application/json", "x-api-key": api_key},
            delay=delay,
        )
    except httpx.TransportError as exc:
        raise ChassisError(f"license servers: connection failed: {exc}") from exc
    if response.status_code != 200:
        raise ChassisError(_error_from_response("license servers", response, response.content, password))
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return 1
    return license_server_id(payload)


async def _download(
    client: httpx.AsyncClient,
    base: str,
    server_id: int,
    api_key: str,
    password: str,
    folder: Path,
    delay: float,
) -> tuple[str, str | None]:
    url = f"{base}{SERVERS_PATH}/{server_id}/offlinelicenserequest"
    headers = {"Accept": "*/*", "Accept-Encoding": "gzip", "x-api-key": api_key}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in _RETRY_STATUSES and attempt == 0:
                    async for _chunk in response.aiter_raw():
                        pass
                    await asyncio.sleep(delay)
                    continue
                if response.status_code != 200:
                    raw = b"".join([chunk async for chunk in response.aiter_raw()])
                    raise ChassisError(_error_from_response("download", response, raw, password))
                filename = content_disposition_filename(response.headers.get("content-disposition"))
                partial = folder / f".{filename}.partial"
                size = 0
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_raw():
                        handle.write(chunk)
                        size += len(chunk)
        except ChassisError:
            raise
        except httpx.TransportError as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(delay)
                continue
            raise ChassisError(f"download: connection failed: {exc}") from exc
        else:
            if size == 0:
                partial.unlink(missing_ok=True)
                raise ChassisError("download: empty body")
            raw = partial.read_bytes()
            partial.unlink()
            try:
                extracted, archive = unpack_offline_request(raw)
            except ValueError as exc:
                raise ChassisError(f"download: {exc}") from exc
            bin_name = offline_request_name(filename)
            _write_bytes(folder, bin_name, extracted)
            archive_name = None
            if archive is not None:
                archive_name = f"{bin_name}.gz"
                _write_bytes(folder, archive_name, archive)
            return bin_name, archive_name
    raise ChassisError(f"download: connection failed: {last_error}")


async def _logout(client: httpx.AsyncClient, base: str, api_key: str) -> None:
    try:
        response = await client.request(
            "DELETE",
            f"{base}{AUTH_PATH}",
            headers={"x-api-key": api_key},
        )
        await response.aclose()
    except Exception:
        return


def _json_object(response: httpx.Response, step: str) -> dict:
    raw = response.content
    text = raw.decode("utf-8", "replace").strip()
    wrapped = _TEXTAREA.search(text)
    if wrapped:
        text = wrapped.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChassisError(f"{step}: response was not JSON") from exc
    if not isinstance(payload, dict):
        raise ChassisError(f"{step}: response was not JSON")
    return payload


def _operation_failure(step: str, payload: dict) -> str:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        state = payload.get("state")
        message = state if isinstance(state, str) and state else "operation failed"
    text = message.strip()
    if len(text) > 300:
        text = text[:300] + "..."
    return f"{step}: {text}"


def _host_from_payload(payload: dict) -> str | None:
    for key in ("hostId", "hostID"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def license_uuid_from_upload(raw: bytes) -> str:
    """The chassis UI reads details.licenseUuid from the upload JSON."""
    text = raw.decode("utf-8", "replace").strip()
    wrapped = _TEXTAREA.search(text)
    if wrapped:
        text = wrapped.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChassisError("upload: response was not JSON") from exc
    if not isinstance(payload, dict):
        raise ChassisError("upload: response was not JSON")
    if payload.get("success") is False:
        details = payload.get("details")
        detail = details if isinstance(details, str) and details else "Could not upload license file"
        raise ChassisError(f"upload: {detail}")
    details = payload.get("details")
    uuid = details.get("licenseUuid") if isinstance(details, dict) else None
    if not isinstance(uuid, str) or not uuid:
        uuid = payload.get("licenseUuid")
    if not isinstance(uuid, str) or not uuid:
        raise ChassisError("upload: response missing licenseUuid")
    return uuid


def _summarize_install_result(payload: dict) -> str:
    if payload.get("success") is False:
        details = payload.get("details") or payload.get("message") or payload.get("error")
        text = details if isinstance(details, str) and details else "install failed"
        raise ChassisError(f"install: {text}")
    if payload.get("isDeactivation") is True:
        return "installed (deactivation receipt)"
    message = payload.get("message")
    if isinstance(message, str) and message.strip() and message.strip() != "Operation successfully completed":
        return message.strip()
    return "installed"


async def _await_operation(
    client: httpx.AsyncClient,
    payload: dict,
    headers: dict[str, str],
    step: str,
    password: str,
    delay: float,
    timeout: float,
) -> dict:
    """Follow the status url until the chassis reports SUCCESS or a failure."""
    deadline = time.monotonic() + timeout
    current = payload
    while True:
        state = current.get("state")
        if isinstance(state, str) and state in _OK_STATES:
            return current
        if isinstance(state, str) and state in _FAIL_STATES:
            raise ChassisError(_operation_failure(step, current))
        url = current.get("url")
        if not isinstance(url, str) or not url:
            return current
        if time.monotonic() >= deadline:
            raise ChassisError(f"{step}: timed out")
        await asyncio.sleep(delay)
        try:
            response = await _send(client, "GET", url, headers=headers, delay=delay)
        except httpx.TransportError as exc:
            raise ChassisError(f"{step}: connection failed: {exc}") from exc
        if response.status_code != 200:
            raise ChassisError(_error_from_response(step, response, response.content, password))
        current = _json_object(response, step)


async def _operation_result(
    client: httpx.AsyncClient,
    final: dict,
    headers: dict[str, str],
    step: str,
    password: str,
    delay: float,
) -> dict:
    result_url = final.get("resultUrl")
    if not isinstance(result_url, str) or not result_url:
        return final
    try:
        response = await _send(client, "GET", result_url, headers=headers, delay=delay)
    except httpx.TransportError as exc:
        raise ChassisError(f"{step}: connection failed: {exc}") from exc
    if response.status_code != 200:
        raise ChassisError(_error_from_response(step, response, response.content, password))
    return _json_object(response, step)


async def _post_operation(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict,
    step: str,
    password: str,
    delay: float,
) -> dict:
    try:
        response = await _send(client, "POST", url, headers=headers, json_body=body, delay=delay)
    except httpx.TransportError as exc:
        raise ChassisError(f"{step}: connection failed: {exc}") from exc
    if response.status_code not in (200, 202):
        raise ChassisError(_error_from_response(step, response, response.content, password))
    return _json_object(response, step)


async def _retrieve_host_id(
    client: httpx.AsyncClient,
    base: str,
    server_id: int,
    api_key: str,
    password: str,
    delay: float,
) -> str:
    headers = {"Accept": "application/json", "x-api-key": api_key}
    url = f"{base}{SERVERS_PATH}/{server_id}/operations/{HOSTID_OPERATION}"
    started = await _post_operation(client, url, headers, {}, "host id", password, delay)
    final = await _await_operation(client, started, headers, "host id", password, delay, INSTALL_TIMEOUT)
    payload = await _operation_result(client, final, headers, "host id", password, delay)
    host = _host_from_payload(payload)
    if host is None:
        raise ChassisError("host id: response missing hostId")
    return host


async def _upload_license(
    client: httpx.AsyncClient,
    base: str,
    server_id: int,
    api_key: str,
    password: str,
    license_file: LicenseFile,
    delay: float,
) -> str:
    # This endpoint answers with text/html. Accept: application/json is a 406.
    headers = {"Accept": "*/*", "x-api-key": api_key}
    url = f"{base}{SERVERS_PATH}/{server_id}/operations/{UPLOAD_OPERATION}"
    files = {"fileId": (license_file.name, license_file.data, "application/octet-stream")}
    try:
        response = await _send(client, "POST", url, headers=headers, files=files, delay=delay)
    except httpx.TransportError as exc:
        raise ChassisError(f"upload: connection failed: {exc}") from exc
    if response.status_code != 200:
        raise ChassisError(_error_from_response("upload", response, response.content, password))
    return license_uuid_from_upload(response.content)


async def _install_uploaded(
    client: httpx.AsyncClient,
    base: str,
    server_id: int,
    api_key: str,
    password: str,
    license_uuid: str,
    delay: float,
) -> str:
    headers = {"Accept": "application/json", "x-api-key": api_key}
    url = f"{base}{SERVERS_PATH}/{server_id}/operations/{INSTALL_OPERATION}"
    started = await _post_operation(
        client,
        url,
        headers,
        {"licenseUuid": license_uuid},
        "install",
        password,
        delay,
    )
    final = await _await_operation(client, started, headers, "install", password, delay, INSTALL_TIMEOUT)
    payload = await _operation_result(client, final, headers, "install", password, delay)
    return _summarize_install_result(payload)


def _same_host(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


async def _install_on_chassis(
    chassis: Chassis,
    licenses: list[LicenseFile],
    client: httpx.AsyncClient,
    delay: float,
    claimed: set[int],
    claim_lock: asyncio.Lock,
) -> tuple[str, dict[int, InstallResult]]:
    """Return this chassis host id (or an error note) and results for licenses it owned."""
    api_key: str | None = None
    base = f"https://{chassis.address}"
    results: dict[int, InstallResult] = {}
    try:
        api_key = await _login(client, base, chassis, delay)
        server_id = await _server_id(client, base, api_key, chassis.password, delay)
        host_id = await _retrieve_host_id(client, base, server_id, api_key, chassis.password, delay)
        print(f"{chassis.address} host {host_id}", flush=True)
        for license_file in licenses:
            if license_file.address is not None and license_file.address != chassis.address:
                continue
            if not _same_host(license_file.host_id, host_id):
                print(
                    f"{chassis.address} host {host_id} does not match {license_file.name} host {license_file.host_id}",
                    flush=True,
                )
                continue
            async with claim_lock:
                if id(license_file) in claimed:
                    continue
                claimed.add(id(license_file))
            try:
                print(f"{chassis.address} uploading {license_file.name}", flush=True)
                license_uuid = await _upload_license(
                    client,
                    base,
                    server_id,
                    api_key,
                    chassis.password,
                    license_file,
                    delay,
                )
                print(f"{chassis.address} installing {license_file.name}", flush=True)
                detail = await _install_uploaded(
                    client,
                    base,
                    server_id,
                    api_key,
                    chassis.password,
                    license_uuid,
                    delay,
                )
                message = f"{detail} on {chassis.address}"
                print(f"{chassis.address} {message}", flush=True)
                results[id(license_file)] = InstallResult(license_file.name, True, message)
            except ChassisError as exc:
                message = _redact(str(exc), chassis.password, api_key)
                print(f"{license_file.name} failed: {message}", flush=True)
                results[id(license_file)] = InstallResult(license_file.name, False, message)
            except Exception as exc:
                message = _redact(f"install: {exc}", chassis.password, api_key)
                print(f"{license_file.name} failed: {message}", flush=True)
                results[id(license_file)] = InstallResult(license_file.name, False, message)
        return host_id, results
    except ChassisError as exc:
        message = _redact(str(exc), chassis.password, api_key)
        print(f"{chassis.address} failed: {message}", flush=True)
        return f"error: {message}", results
    except Exception as exc:
        message = _redact(f"install: {exc}", chassis.password, api_key)
        print(f"{chassis.address} failed: {message}", flush=True)
        return f"error: {message}", results
    finally:
        if api_key:
            await _logout(client, base, api_key)


def _unmatched_detail(license_file: LicenseFile, discovered: list[tuple[str, str]]) -> str:
    if not discovered:
        return f"host {license_file.host_id} did not match any chassis"
    parts = []
    for address, note in discovered:
        if note.startswith("error: "):
            parts.append(f"{address} {note[len('error: '):]}")
        else:
            parts.append(f"{address} is {note}")
    joined = "; ".join(parts)
    return f"host {license_file.host_id} did not match any chassis ({joined})"


async def run_install(
    chassis: list[Chassis],
    licenses: list[LicenseFile],
    *,
    problems: list[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    transport: object | None = None,
    retry_delay: float = 0.5,
) -> InstallOutcome:
    notes = list(problems or [])
    if not licenses and not chassis:
        if notes:
            return InstallOutcome(1, render_install_report([], notes), [])
        return InstallOutcome(2, "No chassis in crews.txt", [])
    if not chassis:
        if notes:
            return InstallOutcome(1, render_install_report([], notes), [])
        return InstallOutcome(2, "No chassis in crews.txt", [])
    if not licenses:
        report = render_install_report([], notes) if notes else "No license files"
        code = 1 if notes else 2
        return InstallOutcome(code, report, [])

    semaphore = asyncio.Semaphore(concurrency)
    claimed: set[int] = set()
    claim_lock = asyncio.Lock()
    timeout = httpx.Timeout(10.0, read=120.0)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async def one(item: Chassis) -> tuple[str, str, dict[int, InstallResult]]:
        async with semaphore:
            host_note, results = await _install_on_chassis(
                item, licenses, client, retry_delay, claimed, claim_lock
            )
            return item.address, host_note, results

    async with httpx.AsyncClient(
        verify=False,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        limits=limits,
        transport=_as_transport(transport),
    ) as client:
        probed = list(await asyncio.gather(*(one(item) for item in chassis)))

    by_id: dict[int, InstallResult] = {}
    for _address, _note, results in probed:
        by_id.update(results)
    discovered = [(address, note) for address, note, _results in probed]
    ordered: list[InstallResult] = []
    for license_file in licenses:
        found = by_id.get(id(license_file))
        if found is not None:
            ordered.append(found)
            continue
        ordered.append(
            InstallResult(license_file.name, False, _unmatched_detail(license_file, discovered))
        )
    report = render_install_report(ordered, notes)
    exit_code = 0 if report == "All licenses installed" else 1
    return InstallOutcome(exit_code, report, ordered)


async def process_chassis(
    chassis: Chassis,
    out_dir: Path,
    client: httpx.AsyncClient,
    delay: float,
) -> ChassisResult:
    folder = out_dir / chassis.address
    _reset_folder(folder)
    api_key: str | None = None
    base = f"https://{chassis.address}"
    try:
        api_key = await _login(client, base, chassis, delay)
        server_id = await _server_id(client, base, api_key, chassis.password, delay)
        filename, archive_name = await _download(
            client,
            base,
            server_id,
            api_key,
            chassis.password,
            folder,
            delay,
        )
        print(f"{chassis.address} saved {filename}", flush=True)
        return ChassisResult(chassis.address, True, filename, filename, archive_name)
    except ChassisError as exc:
        message = _redact(str(exc), chassis.password, api_key)
        _write_failure(folder, message)
        print(f"{chassis.address} failed: {message}", flush=True)
        return ChassisResult(chassis.address, False, message, None)
    except Exception as exc:
        message = _redact(f"download: {exc}", chassis.password, api_key)
        _write_failure(folder, message)
        print(f"{chassis.address} failed: {message}", flush=True)
        return ChassisResult(chassis.address, False, message, None)
    finally:
        if api_key:
            await _logout(client, base, api_key)


def _as_transport(transport: object | None) -> httpx.AsyncBaseTransport | None:
    if transport is None or isinstance(transport, httpx.AsyncBaseTransport):
        return transport
    return _CallableTransport(transport)


async def run(
    chassis: list[Chassis],
    out_dir: Path,
    *,
    parse_errors: list[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    transport: object | None = None,
    now: datetime | None = None,
    retry_delay: float = 0.5,
) -> RunOutcome:
    errors = list(parse_errors or [])
    out_dir.mkdir(parents=True, exist_ok=True)
    if not chassis:
        if errors:
            report = render_report([], errors)
            return RunOutcome(1, report, None, [])
        return RunOutcome(2, "No chassis in crews.txt", None, [])

    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(10.0, read=120.0)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async def one(item: Chassis) -> ChassisResult:
        async with semaphore:
            return await process_chassis(item, out_dir, client, retry_delay)

    async with httpx.AsyncClient(
        verify=False,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        limits=limits,
        headers={"Accept-Encoding": "gzip"},
        transport=_as_transport(transport),
    ) as client:
        results = list(await asyncio.gather(*(one(item) for item in chassis)))
    return finalize(out_dir, results, errors, now or datetime.now(timezone.utc))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download Ixia offline license requests from the chassis listed in crews.txt. "
            "Install a received license with: bulk_offline_lic.py install --license FILE"
        ),
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CSV file of ip,username,password")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Directory for per-IP folders and the zip")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="How many chassis to contact at once",
    )
    return parser


def _install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bulk_offline_lic.py install",
        description=(
            "Upload a received offline license and install it on the chassis whose host ID matches the file."
        ),
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CSV file of ip,username,password")
    parser.add_argument(
        "--license",
        type=Path,
        action="append",
        required=True,
        dest="licenses",
        help=(
            "License .bin, a flat directory of .bin files, or a directory whose subfolders are chassis IP addresses."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="How many chassis to contact at once",
    )
    return parser


def _license_paths(paths: list[Path]) -> tuple[list[tuple[str | None, Path]], list[str]] | str:
    """Return (chassis address or None, license path) pairs and folder problems.

    An address is set when the license was found in an IP-named subfolder.
    That license is installed only on that chassis, after the host ID matches.
    """
    files: list[tuple[str | None, Path]] = []
    problems: list[str] = []
    for path in paths:
        if path.is_dir():
            discovered = licenses_in_ip_folders(path)
            if discovered is not None:
                jobs, folder_problems = discovered
                problems.extend(folder_problems)
                if not jobs and not folder_problems:
                    return f"No IP folders in {path}"
                files.extend(jobs)
                continue
            found = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() == ".bin")
            if not found:
                return f"No .bin files in {path}"
            files.extend((None, item) for item in found)
            continue
        if not path.is_file():
            return f"License file not found: {path}"
        files.append((None, path))
    if not files and not problems:
        return "No license files"
    return files, problems


def _main_install(
    argv: list[str],
    *,
    transport: object | None = None,
    retry_delay: float = 0.5,
) -> int:
    args = _install_parser().parse_args(argv)
    if args.concurrency < 1:
        print("concurrency must be >= 1", file=sys.stderr)
        return 2
    if not args.input.is_file():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2
    expanded = _license_paths(args.licenses)
    if isinstance(expanded, str):
        print(expanded, file=sys.stderr)
        return 2
    selected, folder_problems = expanded
    licenses: list[LicenseFile] = []
    problems: list[str] = list(folder_problems)
    for address, path in selected:
        try:
            licenses.append(load_license(path, address))
        except (OSError, ValueError) as exc:
            label = f"{address}/{path.name}" if address else path.name
            problems.append(f"{label}: {exc}")
    chassis, errors = parse_crews(args.input.read_text(encoding="utf-8"))
    problems.extend(errors)
    bound = {license_file.address for license_file in licenses if license_file.address}
    if bound:
        known = {item.address for item in chassis}
        missing = sorted(address for address in bound if address not in known)
        for address in missing:
            problems.append(f"{address}: no credentials in crews.txt")
        chassis = [item for item in chassis if item.address in bound]
        licenses = [license_file for license_file in licenses if license_file.address not in set(missing)]
    for license_file in licenses:
        where = f"{license_file.address} " if license_file.address else ""
        print(f"{where}{license_file.name} host {license_file.host_id}", flush=True)
    if chassis:
        print("TLS certificate verification is disabled", file=sys.stderr)
    outcome = asyncio.run(
        run_install(
            chassis,
            licenses,
            problems=problems,
            concurrency=args.concurrency,
            transport=transport,
            retry_delay=retry_delay,
        )
    )
    print(outcome.report)
    return outcome.exit_code


def main(
    argv: list[str] | None = None,
    *,
    transport: object | None = None,
    now: datetime | None = None,
    retry_delay: float = 0.5,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "install":
        return _main_install(args_list[1:], transport=transport, retry_delay=retry_delay)
    args = _parser().parse_args(args_list)
    if args.concurrency < 1:
        print("concurrency must be >= 1", file=sys.stderr)
        return 2
    if not args.input.is_file():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2
    chassis, errors = parse_crews(args.input.read_text(encoding="utf-8"))
    if chassis:
        print("TLS certificate verification is disabled", file=sys.stderr)
    outcome = asyncio.run(
        run(
            chassis,
            args.output,
            parse_errors=errors,
            concurrency=args.concurrency,
            transport=transport,
            now=now,
            retry_delay=retry_delay,
        )
    )
    print(outcome.report)
    if outcome.zip_path is not None:
        print(f"Archive: {outcome.zip_path}")
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
