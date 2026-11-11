import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from bulk_offline_lic import (
    Chassis,
    content_disposition_filename,
    license_server_id,
    main,
    parse_crews,
    render_report,
    run,
)

GZIP_BODY = gzip.compress(b"offline-license-request")
FILENAME = (
    "offlinelicenserequest-11-11-2026_17-42-50-02b56e-011101-14c518-6a00.bin"
)
WHEN = datetime(2026, 11, 11, 17, 42, 50, tzinfo=timezone.utc)


def test_parse_crews_skips_header_and_keeps_password_tail():
    text = """
<ip>,<username>,<password>
# comment

10.36.236.121, admin, hash#1,extra
10.36.84.12,admin,
not-a-row
10.36.236.121,admin,second
"""
    chassis, errors = parse_crews(text)

    assert chassis == [
        Chassis("10.36.236.121", "admin", "hash#1,extra"),
        Chassis("10.36.84.12", "admin", ""),
    ]
    assert errors == [
        "Bad row 7: expected ip,username,password",
        "Bad row 8: duplicate chassis 10.36.236.121",
    ]


def test_content_disposition_filename_uses_header_and_strips_directories():
    header = f"attachment; filename={FILENAME}"
    assert content_disposition_filename(header) == FILENAME
    assert content_disposition_filename('attachment; filename="../../evil.bin"') == "evil.bin"
    assert content_disposition_filename(None) == "offlinelicenserequest.bin"
    assert content_disposition_filename("attachment") == "offlinelicenserequest.bin"


def test_license_server_id_uses_first_entry_or_defaults_to_one():
    assert license_server_id([{"id": 7, "host": "localhost"}]) == 7
    assert license_server_id([{"id": "4", "host": "localhost"}]) == 4
    assert license_server_id([]) == 1
    assert license_server_id([{"host": "localhost"}]) == 1
    assert license_server_id(None) == 1


def test_render_report_messages():
    from bulk_offline_lic import ChassisResult

    ok = ChassisResult("10.1.1.1", True, FILENAME, FILENAME)
    bad = ChassisResult("10.1.1.2", False, "download: HTTP 500", None)
    assert render_report([ok], []) == "All chassis success"
    assert render_report([ok, bad], []) == "Partial Success\nFailed chassis:\n  10.1.1.2"
    assert render_report([ok], ["Bad row 3: expected ip,username,password"]) == (
        "Partial Success\nBad row 3: expected ip,username,password"
    )


class ChassisAPI:
    def __init__(
        self,
        *,
        servers=None,
        auth_status=200,
        download_status=200,
        body=GZIP_BODY,
        filename=FILENAME,
        delete_status=204,
        auth_message="nope",
    ):
        self.servers = [{"id": 1, "host": "localhost"}] if servers is None else servers
        self.auth_status = auth_status
        self.download_status = download_status
        self.body = body
        self.filename = filename
        self.delete_status = delete_status
        self.auth_message = auth_message
        self.calls: list[tuple[str, str, str]] = []
        self.auth_payloads: list[dict] = []
        self.download_accept_encoding: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.host, request.url.path))
        if request.url.path == "/platform/api/v1/auth/session" and request.method == "POST":
            self.auth_payloads.append(json.loads(request.content.decode()))
            if self.auth_status != 200:
                return httpx.Response(self.auth_status, json={"message": self.auth_message})
            return httpx.Response(200, json={"apiKey": "secret-api-key", "sessionId": "s"})
        if request.url.path == "/platform/api/v2/licensing/servers" and request.method == "GET":
            assert request.headers["x-api-key"] == "secret-api-key"
            return httpx.Response(200, json=self.servers)
        if request.url.path.startswith("/platform/api/v2/licensing/servers/") and request.url.path.endswith(
            "/offlinelicenserequest"
        ):
            assert request.headers["x-api-key"] == "secret-api-key"
            self.download_accept_encoding.append(request.headers["accept-encoding"])
            headers = {
                "content-type": "application/octet-stream",
                "content-encoding": "gzip",
            }
            if self.filename is not None:
                headers["content-disposition"] = f"attachment; filename={self.filename}"
            # stream= keeps the body raw. content= would gunzip because of Content-Encoding.
            return httpx.Response(
                self.download_status,
                headers=headers,
                stream=httpx.ByteStream(self.body),
            )
        if request.url.path == "/platform/api/v1/auth/session" and request.method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(404, text=f"unexpected {request.method} {request.url.path}")


class FailFirstAuth:
    def __init__(self, inner: ChassisAPI):
        self.inner = inner
        self.failures = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/auth/session") and self.failures == 0:
            self.failures += 1
            raise httpx.ConnectError("connection reset")
        return await self.inner.handle_async_request(request)


def test_successful_download_saves_raw_gzip_and_zips_folder(tmp_path: Path):
    api = ChassisAPI(servers=[{"id": 7, "host": "localhost"}])
    chassis = [Chassis("10.36.84.12", "admin", "chassis-secret")]
    outcome = asyncio.run(
        run(chassis, tmp_path, transport=api, concurrency=2, now=WHEN, retry_delay=0)
    )

    saved = tmp_path / "10.36.84.12" / FILENAME
    assert saved.read_bytes() == GZIP_BODY
    assert outcome.report == "All chassis success"
    assert outcome.exit_code == 0
    assert outcome.zip_path == tmp_path / "offline-requests-2026-11-11T17:42:50Z.zip"
    with __import__("zipfile").ZipFile(outcome.zip_path) as archive:
        assert archive.namelist() == [f"10.36.84.12/{FILENAME}"]
        assert archive.read(f"10.36.84.12/{FILENAME}") == GZIP_BODY
    assert api.auth_payloads == [
        {
            "username": "admin",
            "password": "chassis-secret",
            "rememberMe": True,
            "ignorePolicy": True,
        }
    ]
    assert api.download_accept_encoding == ["gzip"]
    assert ("GET", "10.36.84.12", "/platform/api/v2/licensing/servers/7/offlinelicenserequest") in api.calls
    assert ("DELETE", "10.36.84.12", "/platform/api/v1/auth/session") in api.calls
    assert "chassis-secret" not in outcome.report


def test_empty_server_list_defaults_to_id_one(tmp_path: Path):
    api = ChassisAPI(servers=[])
    outcome = asyncio.run(
        run([Chassis("10.0.0.5", "admin", "pw")], tmp_path, transport=api, now=WHEN, retry_delay=0)
    )
    assert outcome.exit_code == 0
    assert ("GET", "10.0.0.5", "/platform/api/v2/licensing/servers/1/offlinelicenserequest") in api.calls


def test_partial_success_zips_only_bins_and_writes_failure_txt(tmp_path: Path):
    class SplitAPI:
        def __init__(self):
            self.good = ChassisAPI()
            self.bad = ChassisAPI(download_status=500, auth_message="nope")

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            api = self.good if request.url.host == "10.1.1.1" else self.bad
            return await api.handle_async_request(request)

    outcome = asyncio.run(
        run(
            [Chassis("10.1.1.1", "admin", "secret-pass"), Chassis("10.1.1.2", "admin", "secret-pass")],
            tmp_path,
            parse_errors=["Bad row 4: expected ip,username,password"],
            transport=SplitAPI(),
            now=WHEN,
            retry_delay=0,
        )
    )

    assert (tmp_path / "10.1.1.1" / FILENAME).read_bytes() == GZIP_BODY
    failure = (tmp_path / "10.1.1.2" / "failure.txt").read_text(encoding="utf-8")
    assert "download: HTTP 500" in failure
    assert "secret-pass" not in failure
    assert list((tmp_path / "10.1.1.2").iterdir()) == [tmp_path / "10.1.1.2" / "failure.txt"]
    with __import__("zipfile").ZipFile(outcome.zip_path) as archive:
        assert archive.namelist() == [f"10.1.1.1/{FILENAME}"]
    assert outcome.exit_code == 1
    assert outcome.report == (
        "Partial Success\n"
        "Failed chassis:\n"
        "  10.1.1.2\n"
        "Bad row 4: expected ip,username,password"
    )


def test_auth_failure_redacts_password_and_does_not_zip(tmp_path: Path):
    api = ChassisAPI(auth_status=401, auth_message="rejected secret-pass")
    outcome = asyncio.run(
        run([Chassis("10.9.9.9", "admin", "secret-pass")], tmp_path, transport=api, now=WHEN, retry_delay=0)
    )
    text = (tmp_path / "10.9.9.9" / "failure.txt").read_text(encoding="utf-8")
    assert text.startswith("auth: HTTP 401")
    assert "secret-pass" not in text
    assert outcome.zip_path is None
    assert outcome.report == "Partial Success\nFailed chassis:\n  10.9.9.9"
    posts = [call for call in api.calls if call[0] == "POST"]
    assert posts == [("POST", "10.9.9.9", "/platform/api/v1/auth/session")]


def test_auth_retries_once_on_connection_error(tmp_path: Path):
    api = FailFirstAuth(ChassisAPI())
    outcome = asyncio.run(
        run([Chassis("10.2.2.2", "admin", "pw")], tmp_path, transport=api, now=WHEN, retry_delay=0)
    )
    assert outcome.exit_code == 0
    assert api.failures == 1
    assert (tmp_path / "10.2.2.2" / FILENAME).read_bytes() == GZIP_BODY


def test_missing_file_before_archive_is_not_zipped(tmp_path: Path):
    from bulk_offline_lic import ChassisResult, finalize

    folder = tmp_path / "10.4.4.4"
    folder.mkdir()
    result = ChassisResult("10.4.4.4", True, FILENAME, FILENAME)
    outcome = finalize(tmp_path, [result], [], WHEN)
    assert outcome.zip_path is None
    assert "file missing before archive" in (folder / "failure.txt").read_text(encoding="utf-8")
    assert outcome.report == "Partial Success\nFailed chassis:\n  10.4.4.4"


def test_logout_failure_does_not_fail_saved_request(tmp_path: Path):
    api = ChassisAPI(delete_status=500)
    outcome = asyncio.run(
        run([Chassis("10.8.8.8", "admin", "pw")], tmp_path, transport=api, now=WHEN, retry_delay=0)
    )
    assert outcome.exit_code == 0
    assert (tmp_path / "10.8.8.8" / FILENAME).is_file()


def test_main_prints_partial_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    crews = tmp_path / "crews.txt"
    crews.write_text("<ip>,<username>,<password>\n10.5.5.5,admin,pw\n", encoding="utf-8")
    out = tmp_path / "out"
    api = ChassisAPI(download_status=404)
    code = main(["--input", str(crews), "--output", str(out)], transport=api, now=WHEN, retry_delay=0)
    captured = capsys.readouterr()
    assert code == 1
    assert "TLS certificate verification is disabled" in captured.err
    assert "Partial Success" in captured.out
    assert "Failed chassis:" in captured.out
    assert "10.5.5.5" in captured.out
    assert "pw" not in captured.out
    assert "secret-api-key" not in captured.out
    assert not list(out.glob("*.zip"))


def test_main_missing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code = main(["--input", str(tmp_path / "missing.txt"), "--output", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 2
    assert "Input file not found" in captured.err
