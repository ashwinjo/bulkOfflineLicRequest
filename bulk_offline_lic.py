"""Download Ixia offline license requests for many chassis and zip the successes.

Each chassis is authenticated on its own. The offline-request body is saved
exactly as it arrives (gzip transport bytes included) under a folder named for
that chassis. After every chassis has finished, the successful folders are
packed into one zip. Failed chassis keep a failure.txt on disk and stay out
of the archive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote

import httpx

AUTH_PATH = "/platform/api/v1/auth/session"
SERVERS_PATH = "/platform/api/v2/licensing/servers"
DEFAULT_FILENAME = "offlinelicenserequest.bin"
DEFAULT_INPUT = Path("crews.txt")
DEFAULT_OUTPUT = Path("out")
DEFAULT_CONCURRENCY = 8
_ADDRESS = re.compile(r"^[A-Za-z0-9._:-]+$")
_HEADER_NAMES = {"ip", "<ip>"}
_RETRY_STATUSES = {502, 503, 504}


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


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    report: str
    zip_path: Path | None
    results: list[ChassisResult]


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
        path = out_dir / result.address / result.filename
        if _contained_file(out_dir, result.address, path):
            checked.append(result)
            continue
        message = "download: file missing before archive"
        _write_failure(out_dir / result.address, message)
        checked.append(ChassisResult(result.address, False, message, None))

    members = [
        (out_dir / result.address / result.filename, f"{result.address}/{result.filename}")
        for result in checked
        if result.ok and result.filename
    ]
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
    attempts: int = 2,
    delay: float = 0,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, headers=headers, json=json_body)
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
) -> str:
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
            partial.replace(folder / filename)
            return filename
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
        filename = await _download(
            client,
            base,
            server_id,
            api_key,
            chassis.password,
            folder,
            delay,
        )
        print(f"{chassis.address} saved {filename}", flush=True)
        return ChassisResult(chassis.address, True, filename, filename)
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
        description="Download Ixia offline license requests from the chassis listed in crews.txt.",
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


def main(
    argv: list[str] | None = None,
    *,
    transport: object | None = None,
    now: datetime | None = None,
    retry_delay: float = 0.5,
) -> int:
    args = _parser().parse_args(argv)
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
