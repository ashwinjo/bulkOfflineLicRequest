"""The local page calls the same chassis code and does not keep passwords."""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_bulk_offline_lic import HOST_A, ChassisAPI, InstallAPI, REQUEST_BIN
from webapp.server import (
    Service,
    download_requests,
    license_path,
    parse_concurrency,
    safe_relative,
    serve,
)

WHEN = datetime(2026, 11, 11, 17, 42, 50, tzinfo=timezone.utc)
PASSWORD = "chassis-secret"
LICENSE_NAME = "02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin"
PAGE_FILES = ["index.html", "app.css", "app.js", "fonts.css", "tokens.css", "base.css"]


def test_safe_relative_rejects_parent_segments():
    assert safe_relative("licenses/10.1.1.1/a.bin") == Path("licenses/10.1.1.1/a.bin")
    assert safe_relative("../crews.txt") is None
    assert safe_relative("/etc/passwd") is None
    assert safe_relative("licenses/../../crews.txt") is None


def test_concurrency_matches_the_command_default_and_rejects_extremes():
    assert parse_concurrency(None) == 8
    assert parse_concurrency("4") == 4
    try:
        parse_concurrency("0")
    except ValueError as exc:
        assert "1 to 64" in str(exc)
    else:
        raise AssertionError("expected a rejection")


def test_download_returns_the_zip_and_hides_the_password(tmp_path: Path):
    outcome = download_requests(
        f"10.36.84.12,admin,{PASSWORD}",
        2,
        tmp_path,
        transport=ChassisAPI(),
        retry_delay=0,
        now=WHEN,
    )

    assert outcome.exit_code == 0
    assert outcome.report == "All chassis success"
    assert outcome.rows[0]["address"] == "10.36.84.12"
    assert outcome.rows[0]["file"] == REQUEST_BIN
    assert PASSWORD not in outcome.report
    assert PASSWORD not in json.dumps(outcome.rows)
    archive = tmp_path / outcome.archive_name
    assert archive.is_file()
    assert archive.stat().st_size > 0


def test_page_does_not_call_the_network_for_its_own_files():
    root = Path(__file__).resolve().parent
    for name in PAGE_FILES:
        text = (root / name).read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text
        assert "fonts.gstatic.com" not in text
        assert "http://" not in text
        assert "https://" not in text


def _start(tmp_path: Path, transport):
    service = Service(tmp_path, transport=transport, retry_delay=0, now=WHEN)
    server = serve(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(server, path: str, *, data: bytes | None = None, headers: dict | None = None):
    host, port = server.server_address[:2]
    request = Request(f"http://{host}:{port}{path}", data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urlopen(request) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_http_download_saves_a_zip_without_echoing_the_password(tmp_path: Path):
    server = _start(tmp_path, ChassisAPI())
    try:
        status, headers, body = _request(server, "/")
        assert status == 200
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert b"fonts.googleapis" not in body

        payload = json.dumps({"crews": f"10.36.84.12,admin,{PASSWORD}", "concurrency": 2}).encode()
        status, _headers, body = _request(
            server,
            "/api/download",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert status == 200
        assert PASSWORD.encode() not in body
        report = json.loads(body)
        assert report["status"] == "Success"
        assert report["results"][0]["address"] == "10.36.84.12"
        assert report["results"][0]["file"] == REQUEST_BIN
        assert report["archiveName"].endswith(".zip")

        status, zip_headers, zip_body = _request(server, report["archive"])
        assert status == 200
        assert zip_headers["Content-Type"] == "application/zip"
        assert zip_body[:2] == b"PK"
    finally:
        server.shutdown()


def test_http_install_reads_an_absolute_folder(tmp_path: Path):
    server = _start(tmp_path, InstallAPI())
    folder = tmp_path / "licenses" / "10.1.1.1"
    folder.mkdir(parents=True)
    license_bytes = base64.b64encode(json.dumps({"hostInfo": {"hostID": HOST_A}}).encode())
    (folder / LICENSE_NAME).write_bytes(license_bytes)
    payload = json.dumps({
        "crews": f"10.1.1.1,admin,{PASSWORD}",
        "license": str(tmp_path / "licenses"),
    }).encode()
    try:
        status, _headers, raw = _request(
            server,
            "/api/install",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert status == 200, raw
        assert PASSWORD.encode() not in raw
        report = json.loads(raw)
        assert report["status"] == "Success"
        assert report["results"][0]["address"] == "10.1.1.1"
        assert report["results"][0]["file"] == LICENSE_NAME
        assert report["results"][0]["ok"] is True
        assert (folder / LICENSE_NAME).is_file()
    finally:
        server.shutdown()


def test_license_path_rejects_a_relative_path():
    try:
        license_path("licenses")
    except ValueError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("expected a rejection")
