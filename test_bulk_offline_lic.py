import asyncio
import base64
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from bulk_offline_lic import (
    Chassis,
    LicenseFile,
    content_disposition_filename,
    license_host_id,
    offline_request_name,
    unpack_offline_request,
    license_server_id,
    license_uuid_from_upload,
    licenses_in_ip_folders,
    main,
    parse_crews,
    received_license_name,
    render_install_report,
    render_report,
    run,
    run_install,
)

HOST_A = "02d894-451101-14c518-6a00"
HOST_B = "02b56e-011101-14c518-6a00"
SAMPLE_LICENSE = Path(__file__).with_name(
    "02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin"
)

REQUEST_TEXT = b"offline-license-request"
GZIP_BODY = gzip.compress(REQUEST_TEXT)
FILENAME = (
    "offlinelicenserequest-11-11-2026_17-42-50-02b56e-011101-14c518-6a00.bin"
)
REQUEST_BIN = "offlineReq-02b56e-011101-14c518-6a00_2026-11-11_17-42-50.bin"
REQUEST_GZ = f"{REQUEST_BIN}.gz"
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


def test_offline_request_name_matches_the_licensing_tool():
    chassis_name = "offlinelicenserequest-11-19-2026_23-31-09-0488b0-f4c940-6afe7e-e4d7.bin"
    assert offline_request_name(chassis_name) == (
        "offlineReq-0488b0-f4c940-6afe7e-e4d7_2026-11-19_23-31-09.bin"
    )
    assert offline_request_name(FILENAME) == REQUEST_BIN
    assert offline_request_name("offlinelicenserequest.bin") == "offlinelicenserequest.bin"


def test_unpack_offline_request_keeps_the_gzip_archive():
    extracted, archive = unpack_offline_request(GZIP_BODY)
    assert extracted == REQUEST_TEXT
    assert archive == GZIP_BODY
    extracted, archive = unpack_offline_request(REQUEST_TEXT)
    assert extracted == REQUEST_TEXT
    assert archive is None


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

    saved = tmp_path / "10.36.84.12" / REQUEST_BIN
    assert saved.read_bytes() == REQUEST_TEXT
    assert (tmp_path / "10.36.84.12" / REQUEST_GZ).read_bytes() == GZIP_BODY
    assert outcome.report == "All chassis success"
    assert outcome.exit_code == 0
    assert outcome.zip_path == tmp_path / "offline-requests-2026-11-11T17:42:50Z.zip"
    with __import__("zipfile").ZipFile(outcome.zip_path) as archive:
        assert archive.namelist() == [
            f"10.36.84.12/{REQUEST_BIN}",
            f"10.36.84.12/{REQUEST_GZ}",
        ]
        assert archive.read(f"10.36.84.12/{REQUEST_BIN}") == REQUEST_TEXT
        assert archive.read(f"10.36.84.12/{REQUEST_GZ}") == GZIP_BODY
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

    assert (tmp_path / "10.1.1.1" / REQUEST_BIN).read_bytes() == REQUEST_TEXT
    assert (tmp_path / "10.1.1.1" / REQUEST_GZ).read_bytes() == GZIP_BODY
    failure = (tmp_path / "10.1.1.2" / "failure.txt").read_text(encoding="utf-8")
    assert "download: HTTP 500" in failure
    assert "secret-pass" not in failure
    assert list((tmp_path / "10.1.1.2").iterdir()) == [tmp_path / "10.1.1.2" / "failure.txt"]
    with __import__("zipfile").ZipFile(outcome.zip_path) as archive:
        assert archive.namelist() == [f"10.1.1.1/{REQUEST_BIN}", f"10.1.1.1/{REQUEST_GZ}"]
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
    assert (tmp_path / "10.2.2.2" / REQUEST_BIN).read_bytes() == REQUEST_TEXT


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
    assert (tmp_path / "10.8.8.8" / REQUEST_BIN).is_file()
    assert (tmp_path / "10.8.8.8" / REQUEST_GZ).is_file()


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


def _license_file(host: str, name: str = "license.bin", payload: dict | None = None) -> LicenseFile:
    body = {"hostInfo": {"hostID": host}, "isDeactivation": False}
    if payload:
        body.update(payload)
    data = base64.b64encode(json.dumps(body).encode())
    return LicenseFile(name, host, data)


def test_license_host_id_reads_base64_and_raw_json():
    encoded = _license_file(HOST_A).data
    assert license_host_id(encoded) == HOST_A
    raw = json.dumps({"hostInfo": {"hostId": HOST_B}}).encode()
    assert license_host_id(raw) == HOST_B
    with pytest.raises(ValueError):
        license_host_id(b"not-a-license")


def test_example_license_file_host_id():
    assert license_host_id(SAMPLE_LICENSE.read_bytes()) == HOST_A


def test_license_uuid_from_upload_accepts_textarea_wrapper():
    body = b'<textarea>{"success": true, "details": {"licenseUuid": "abc-123"}}</textarea>'
    assert license_uuid_from_upload(body) == "abc-123"
    with pytest.raises(Exception, match="Could not upload"):
        license_uuid_from_upload(b'{"success": false, "details": "Could not upload license file"}')


def test_render_install_report_names_the_file():
    from bulk_offline_lic import InstallResult

    ok = InstallResult("a.bin", True, "installed on 10.1.1.1")
    bad = InstallResult("b.bin", False, "host missing")
    assert render_install_report([ok], []) == "All licenses installed"
    assert render_install_report([ok, bad], []) == "Partial Success\nFailed licenses:\n  b.bin: host missing"


class InstallAPI:
    def __init__(self, *, host_id=HOST_A, upload_status=200, install_state="SUCCESS", upload_body=None):
        self.host_id = host_id
        self.upload_status = upload_status
        self.install_state = install_state
        self.upload_body = upload_body or {
            "success": True,
            "details": {"licenseUuid": "lic-uuid-1"},
        }
        self.calls: list[tuple[str, str]] = []
        self.hosts: list[str] = []
        self.upload_content: list[bytes] = []
        self.upload_accept: list[str] = []
        self.install_payloads: list[dict] = []

    def _op(self, host: str, name: str, op_id: int, state: str, message: str) -> dict:
        base = f"https://{host}/platform/api/v2/licensing/servers/1/operations/{name}/{op_id}"
        payload = {
            "id": op_id,
            "state": state,
            "progress": 100 if state == "SUCCESS" else 0,
            "message": message,
            "url": base,
        }
        if state == "SUCCESS":
            payload["resultUrl"] = base + "/result"
        return payload

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.hosts.append(request.url.host)
        host = request.url.host
        if path == "/platform/api/v1/auth/session" and request.method == "POST":
            return httpx.Response(200, json={"apiKey": "secret-api-key"})
        if path == "/platform/api/v1/auth/session" and request.method == "DELETE":
            return httpx.Response(204)
        if path == "/platform/api/v2/licensing/servers" and request.method == "GET":
            assert request.headers["x-api-key"] == "secret-api-key"
            return httpx.Response(200, json=[{"id": 1, "host": "localhost"}])
        if path.endswith("/operations/retrievehostid") and request.method == "POST":
            body = self._op(host, "retrievehostid", 3, "IN_PROGRESS", "Operation in progress")
            return httpx.Response(202, json=body)
        if path.endswith("/operations/retrievehostid/3") and request.method == "GET":
            body = self._op(host, "retrievehostid", 3, "SUCCESS", "Operation successfully completed")
            return httpx.Response(200, json=body)
        if path.endswith("/operations/retrievehostid/3/result"):
            return httpx.Response(200, json={"hostId": self.host_id})
        if path.endswith("/operations/uploadofflinecelicense") and request.method == "POST":
            assert request.headers["x-api-key"] == "secret-api-key"
            self.upload_accept.append(request.headers["accept"])
            self.upload_content.append(request.content)
            if self.upload_status != 200:
                return httpx.Response(self.upload_status, json={"message": "Could not upload license file"})
            return httpx.Response(200, json=self.upload_body)
        if path.endswith("/operations/installofflinelicense") and request.method == "POST":
            self.install_payloads.append(json.loads(request.content))
            state = "IN_PROGRESS" if self.install_state == "SUCCESS" else self.install_state
            message = "Operation in progress" if state == "IN_PROGRESS" else "license rejected"
            return httpx.Response(202, json=self._op(host, "installofflinelicense", 4, state, message))
        if path.endswith("/operations/installofflinelicense/4") and request.method == "GET":
            body = self._op(host, "installofflinelicense", 4, "SUCCESS", "Operation successfully completed")
            return httpx.Response(200, json=body)
        if path.endswith("/operations/installofflinelicense/4/result"):
            return httpx.Response(200, json={"isDeactivation": False})
        return httpx.Response(404, text=f"unexpected {request.method} {path}")


def test_install_uploads_file_then_installs_uuid():
    api = InstallAPI()
    license_file = _license_file(HOST_A, name="received.bin")
    outcome = asyncio.run(
        run_install(
            [Chassis("10.1.1.1", "admin", "chassis-secret")],
            [license_file],
            transport=api,
            retry_delay=0,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.report == "All licenses installed"
    assert outcome.results[0].detail == "installed on 10.1.1.1"
    assert api.upload_accept == ["*/*"]
    assert api.install_payloads == [{"licenseUuid": "lic-uuid-1"}]
    uploaded = api.upload_content[0]
    assert b'name="fileId"' in uploaded
    assert b'filename="received.bin"' in uploaded
    assert license_file.data in uploaded
    assert ("POST", "/platform/api/v2/licensing/servers/1/operations/uploadofflinecelicense") in api.calls
    assert ("POST", "/platform/api/v2/licensing/servers/1/operations/installofflinelicense") in api.calls
    assert ("DELETE", "/platform/api/v1/auth/session") in api.calls
    assert "chassis-secret" not in outcome.report


def test_install_skips_upload_when_host_id_does_not_match():
    api = InstallAPI(host_id=HOST_B)
    license_file = _license_file(HOST_A, name="other-host.bin")
    outcome = asyncio.run(
        run_install(
            [Chassis("10.36.84.12", "admin", "pw")],
            [license_file],
            transport=api,
            retry_delay=0,
        )
    )

    assert outcome.exit_code == 1
    assert "did not match" in outcome.results[0].detail
    assert HOST_A in outcome.results[0].detail
    assert HOST_B in outcome.results[0].detail
    assert "10.36.84.12" in outcome.results[0].detail
    assert not any(path.endswith("/uploadofflinecelicense") for _method, path in api.calls)
    assert not any(path.endswith("/installofflinelicense") for _method, path in api.calls)


def test_install_reports_upload_failure_without_installing():
    api = InstallAPI(upload_status=400)
    outcome = asyncio.run(
        run_install(
            [Chassis("10.1.1.1", "admin", "secret-pass")],
            [_license_file(HOST_A)],
            transport=api,
            retry_delay=0,
        )
    )
    assert outcome.exit_code == 1
    assert "upload: HTTP 400" in outcome.results[0].detail
    assert "secret-pass" not in outcome.results[0].detail
    assert not any(path.endswith("/installofflinelicense") for _method, path in api.calls)


def test_install_operation_error_fails_the_license():
    api = InstallAPI(install_state="ERROR")
    outcome = asyncio.run(
        run_install(
            [Chassis("10.1.1.1", "admin", "pw")],
            [_license_file(HOST_A, name="bad.bin")],
            transport=api,
            retry_delay=0,
        )
    )
    assert outcome.exit_code == 1
    assert outcome.results[0].detail.startswith("install: license rejected")


def test_main_install_matches_host_and_prints_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    license_path = tmp_path / "received.bin"
    license_path.write_bytes(_license_file(HOST_A).data)
    crews = tmp_path / "crews.txt"
    crews.write_text("10.9.9.9,admin,pw\n", encoding="utf-8")
    code = main(
        ["install", "--input", str(crews), "--license", str(license_path)],
        transport=InstallAPI(),
        retry_delay=0,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "TLS certificate verification is disabled" in captured.err
    assert f"{license_path.name} host {HOST_A}" in captured.out
    assert "All licenses installed" in captured.out
    assert "pw" not in captured.out


def test_ip_folder_selects_received_license_and_skips_request_bin(tmp_path: Path):
    received = "02b56e-011101-14c518-6a00-27220873-2026-11-23-20-40.bin"
    folder = tmp_path / "10.1.1.1"
    folder.mkdir()
    (folder / received).write_bytes(b"license")
    (folder / "offlinelicenserequest-11-23-2026_20-28-03-02b56e-011101-14c518-6a00.bin").write_bytes(
        b"request"
    )
    (folder / "notes.html").write_text("ignore", encoding="utf-8")
    assert received_license_name(received)
    assert not received_license_name("offlinelicenserequest-11-23-2026_20-28-03.bin")

    jobs, problems = licenses_in_ip_folders(tmp_path)
    assert problems == []
    assert jobs == [("10.1.1.1", folder / received)]


def test_two_portal_responses_are_kept_in_filename_order(tmp_path: Path):
    folder = tmp_path / "10.185.35.100"
    folder.mkdir()
    later = "02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin"
    earlier = "02d894-451101-14c518-6a00-27220627-2026-11-23-20-14.bin"
    (folder / later).write_bytes(b"later")
    (folder / earlier).write_bytes(b"earlier")
    (folder / "offlineReq-02d894-451101-14c518-6a00_2026-11-12_08-00-45.bin").write_bytes(b"request")

    jobs, problems = licenses_in_ip_folders(tmp_path)
    assert problems == []
    assert [path.name for _address, path in jobs] == [earlier, later]


def test_main_install_from_ip_folder_uploads_only_the_received_bin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    received_name = "02b56e-011101-14c518-6a00-27220873-2026-11-23-20-40.bin"
    folder = tmp_path / "licenses" / "10.1.1.1"
    folder.mkdir(parents=True)
    (folder / received_name).write_bytes(_license_file(HOST_A, name=received_name).data)
    (folder / "offlinelicenserequest-11-23-2026_20-28-03-02b56e-011101-14c518-6a00.bin").write_bytes(
        b"request-body"
    )
    crews = tmp_path / "crews.txt"
    crews.write_text("10.1.1.1,admin,pw\n10.9.9.9,admin,other\n", encoding="utf-8")
    api = InstallAPI()
    code = main(
        ["install", "--input", str(crews), "--license", str(tmp_path / "licenses")],
        transport=api,
        retry_delay=0,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "All licenses installed" in captured.out
    assert api.upload_content
    assert b"request-body" not in api.upload_content[0]
    assert received_name.encode() in api.upload_content[0]
    assert set(api.hosts) == {"10.1.1.1"}
    assert "pw" not in captured.out


def test_two_portal_responses_upload_one_after_the_other(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    earlier = "02d894-451101-14c518-6a00-27220627-2026-11-23-20-14.bin"
    later = "02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin"
    folder = tmp_path / "licenses" / "10.185.35.100"
    folder.mkdir(parents=True)
    (folder / later).write_bytes(_license_file(HOST_A, name=later).data)
    (folder / earlier).write_bytes(_license_file(HOST_A, name=earlier).data)
    crews = tmp_path / "crews.txt"
    crews.write_text("10.185.35.100,admin,pw\n", encoding="utf-8")

    class FirstUploadFails(InstallAPI):
        def __init__(self):
            super().__init__()
            self.uploads = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/operations/uploadofflinecelicense") and request.method == "POST":
                self.uploads += 1
                if self.uploads == 1:
                    self.calls.append((request.method, request.url.path))
                    self.hosts.append(request.url.host)
                    self.upload_accept.append(request.headers["accept"])
                    self.upload_content.append(request.content)
                    return httpx.Response(400, json={"message": "Could not upload license file"})
            return await super().handle_async_request(request)

    api = FirstUploadFails()
    code = main(
        ["install", "--input", str(crews), "--license", str(tmp_path / "licenses")],
        transport=api,
        retry_delay=0,
    )
    captured = capsys.readouterr()
    assert code == 1
    assert earlier.encode() in api.upload_content[0]
    assert later.encode() in api.upload_content[1]
    assert api.install_payloads == [{"licenseUuid": "lic-uuid-1"}]
    assert "Could not upload license file" in captured.out
    assert "All licenses installed" not in captured.out
    assert "pw" not in captured.out


def test_main_install_missing_license(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    crews = tmp_path / "crews.txt"
    crews.write_text("10.9.9.9,admin,pw\n", encoding="utf-8")
    code = main(["install", "--input", str(crews), "--license", str(tmp_path / "missing.bin")])
    captured = capsys.readouterr()
    assert code == 2
    assert "License file not found" in captured.err
