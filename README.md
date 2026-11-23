# Bulk offline license requests

Downloads an offline license request file from each Ixia chassis listed in a text file, and installs the license files that come back. Chassis are contacted at the same time. When every chassis has finished a download, the successful requests are packed into one zip.

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Chassis list

Copy the example and edit it:

```bash
cp crews.example.txt crews.txt
```

Each chassis is one line: `ip,username,password`.

```text
<ip>,<username>,<password>
10.0.0.1,admin,changeme
10.0.0.2,admin,hash#1,extra
```

- The first line is a header when its first field is `ip` or `<ip>`. It is skipped.
- Blank lines and lines that start with `#` are skipped.
- The password is everything after the second comma, so it may contain commas, `#`, and spaces.
- A hostname or an IPv4 address is accepted, with an optional port: `10.0.0.1:8443`.
- A duplicate address keeps the first row. The later row is reported as a failure.
- `crews.txt` holds passwords. It is listed in `.gitignore`. Do not commit it.

The machine running the script must be able to reach each chassis on HTTPS.

## Run

```bash
python bulk_offline_lic.py --input crews.txt --output out
```

`--input` defaults to `crews.txt`. `--output` defaults to `out`. `--concurrency` defaults to `8`, which is how many chassis are contacted at once.

```bash
python bulk_offline_lic.py --input crews.txt --output out --concurrency 4
```

Certificate verification is turned off, because chassis management interfaces use a certificate the normal trust store rejects. The script prints `TLS certificate verification is disabled` when it starts. It connects directly to each chassis and does not use `HTTP_PROXY` or `HTTPS_PROXY`.

## What you get

Each chassis gets a folder named after its address.

```text
out/
  10.0.0.1/offlineReq-02b56e-011101-14c518-6a00_2026-11-11_17-42-50.bin
  10.0.0.1/offlineReq-02b56e-011101-14c518-6a00_2026-11-11_17-42-50.bin.gz
  10.0.0.2/failure.txt
  offline-requests-2026-11-11T17:42:50Z.zip
```

The chassis names the download `offlinelicenserequest-11-11-2026_17-42-50-02b56e-011101-14c518-6a00.bin` and sends it as a gzip archive. The licensing tool wants a different name and the extracted request, so each success folder gets both files:

- `offlineReq-<host>_<YYYY-MM-DD>_<HH-MM-SS>.bin` is the archive contents. This is the short base64 request the licensing tool imports.
- `offlineReq-<host>_<YYYY-MM-DD>_<HH-MM-SS>.bin.gz` is the original gzip archive, kept so the download can be checked against the extracted file.

The host, date, and time come from the chassis filename. `11-11-2026_17-42-50` becomes `2026-11-11_17-42-50`, and the host ID moves in front of the date.

The zip contains both files from each successful address folder. `failure.txt` stays on disk under the failed address and is not in the zip. The file contains the error from the step that failed (login, license-server lookup, or download). Passwords and api keys are not written to the console or to `failure.txt`.

When every chassis succeeds, the script prints:

```text
All chassis success
Archive: out/offline-requests-2026-11-11T17:42:50Z.zip
```

When at least one chassis or input row fails, it prints the failed addresses and still writes a zip of the ones that succeeded:

```text
Partial Success
Failed chassis:
  10.0.0.2
Archive: out/offline-requests-2026-11-11T17:42:50Z.zip
```

If nothing succeeded, there is no zip.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Every chassis succeeded |
| 1 | Partial success, or every chassis failed |
| 2 | Input file missing, no chassis rows, or `--concurrency` less than 1 |

## What it calls on each chassis

1. `POST /platform/api/v1/auth/session` with the username and password.
2. `GET /platform/api/v2/licensing/servers` using the returned api key as `x-api-key`.
3. `GET /platform/api/v2/licensing/servers/{id}/offlinelicenserequest`. Gunzip that body, save it as `offlineReq-<host>_<date>_<time>.bin`, and keep the gzip bytes as the same name with `.gz` added.
4. `DELETE /platform/api/v1/auth/session` to close the session. A failed logout does not discard a file that was already saved.

The license-server id is the id in the list. If that list is empty, the download uses id `1`.

## Install received licenses

The install command takes one main folder. Inside it, each subfolder is named with a chassis IP. That IP must also be a row in `crews.txt`. The script uses the subfolder name to choose the chassis, and the matching `crews.txt` row for the username and password. A subfolder whose IP is missing from `crews.txt` is reported and skipped. A row in `crews.txt` with no matching subfolder is left alone.

```text
licenses/                                          main folder, passed to --license
  10.185.35.100/                                   must be a row in crews.txt
    02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin
  10.185.35.104/
    02dce8-4b7701-14c518-6a00-27220570-2026-11-23-20-06.bin
  10.36.84.12/
    02b56e-011101-14c518-6a00-27220873-2026-11-23-20-40.bin
```

`crews.txt` for that folder:

```text
<ip>,<username>,<password>
10.185.35.100,admin,secret
10.185.35.104,admin,secret
10.36.84.12,admin,secret
```

```bash
python bulk_offline_lic.py install --license /path/to/licenses --input crews.txt
```

`--input` defaults to `crews.txt`. `--concurrency` works the same way as the download command. Every IP subfolder is visited. Folders are independent: one failure does not stop the others. A chassis is contacted when its folder contains at least one received license and `crews.txt` has that same IP.

`--license` can also be one `.bin`. That file is installed on whichever chassis in `crews.txt` has the same host ID.

### Which file is the license

A received license name has this shape:

```text
02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin
|________________________| |______| |________________|
 host ID                    token    date and time
```

The host ID is the first four groups. It is also stored inside the file as `hostInfo.hostID`. The token (`27220628` above) and the date (`2026-11-23-20-14`) identify that portal response. They are not part of the host ID.

These files sit in the same folder and are not uploaded:

- `offlinelicenserequest-....bin` and `offlineReq-....bin`, the request that was sent to the portal
- `.bin.gz` copies of that request
- `*_instructions.html`

### Several subfolders

Each address folder is one chassis. For every folder the script:

1. Reads the username and password for that address from `crews.txt`. An address with no row is reported and skipped.
2. Logs in and reads the chassis host ID.
3. Compares that host ID with `hostInfo.hostID` in each `.bin`.
4. Uploads and installs each file whose host ID matches. Files in one folder go one after another, in filename order, on the same login.

A folder whose license belongs to a different chassis is left unchanged. The report names both host IDs.

### Several .bin files in one folder

Two portal responses for the same chassis are both installed, one after the other. The lower token goes first:

```text
10.185.35.100/
  02d894-451101-14c518-6a00-27220627-2026-11-23-20-14.bin
  02d894-451101-14c518-6a00-27220628-2026-11-23-20-14.bin
```

`27220627` is installed, then `27220628`. The date on those two names is the same. When the dates differ, filename order still decides, because the token is compared before the date. A failure on the first file does not skip the second. Offline-request files in that folder are still ignored.

### What it calls

After login, each matching chassis does this:

1. `POST /platform/api/v2/licensing/servers/{id}/operations/retrievehostid`, then poll the status URL until it succeeds. The result is `{"hostId": "..."}`.
2. `POST /platform/api/v2/licensing/servers/{id}/operations/uploadofflinecelicense` with the file in multipart field `fileId`. The request accepts any response type. The body looks like JSON and is served as `text/html`: `{"success": true, "details": {"licenseUuid": "..."}}`. Asking only for `application/json` gets HTTP 406.
3. `POST /platform/api/v2/licensing/servers/{id}/operations/installofflinelicense` with `{"licenseUuid": "..."}`, then poll until the operation succeeds and read its result.
4. `DELETE /platform/api/v1/auth/session`.

The license-server id is the first id from `GET /platform/api/v2/licensing/servers`. An empty list uses id `1`.

When every selected file installs, the script prints `All licenses installed`. A skipped folder, a host mismatch, or a failed upload prints `Partial Success` and the reason. Passwords and api keys are not printed.

| Code | Meaning |
| --- | --- |
| 0 | Every selected license installed |
| 1 | One or more folders or chassis failed, and the others were still attempted |
| 2 | License path missing, no license files, no chassis rows, or `--concurrency` less than 1 |
