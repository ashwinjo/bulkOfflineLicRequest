# Bulk offline license requests

Downloads an offline license request file from each Ixia chassis listed in a text file. Chassis are contacted at the same time. When every chassis has finished, the successful requests are packed into one zip.

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
  10.0.0.1/offlinelicenserequest-11-11-2026_17-42-50-02b56e-011101-14c518-6a00.bin
  10.0.0.2/failure.txt
  offline-requests-2026-11-11T17:42:50Z.zip
```

The `.bin` is the response body as the chassis sent it, including gzip transport bytes. Its name comes from the `filename` in `Content-Disposition`.

The zip contains only the successful address folders. `failure.txt` stays on disk under the failed address and is not in the zip. The file contains the error from the step that failed (login, license-server lookup, or download). Passwords and api keys are not written to the console or to `failure.txt`.

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
3. `GET /platform/api/v2/licensing/servers/{id}/offlinelicenserequest` and save that body.
4. `DELETE /platform/api/v1/auth/session` to close the session. A failed logout does not discard a file that was already saved.

The license-server id is the id in the list. If that list is empty, the download uses id `1`.
