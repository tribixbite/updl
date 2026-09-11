# updl

[![CI](https://github.com/tribixbite/updl/actions/workflows/pythonpackage.yml/badge.svg)](https://github.com/tribixbite/updl/actions/workflows/pythonpackage.yml)
[![PyPI](https://img.shields.io/pypi/v/updl?color=blue)](https://pypi.org/project/updl/)
[![Python](https://img.shields.io/pypi/pyversions/updl)](https://pypi.org/project/updl/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Resumable, verifiable archiver for UniFi Protect footage.**

Back up a UniFi Protect system to local storage in a way you can re-run at any time: it
downloads only what is genuinely missing, remembers the hours the NVR had no footage for,
retries the ones that failed, and can prove that what is already on disk is still intact.

```console
$ updl /srv/protect
Archive currently holds 367 segment(s) (122 empty, 245 ok)
0 files downloaded (0.0 b), 367 already archived, 0 files skipped, 0 files failed
```

That run took **0.35 seconds and made no requests to the NVR at all**, because everything
was already held. The first run of the same command downloaded 58.5 GB.

> Not affiliated with, endorsed by, or supported by Ubiquiti Inc. "UniFi" and "Protect"
> are trademarks of Ubiquiti Inc., used here only to describe what this tool talks to.

## Install

```console
pip install updl
```

Python 3.10+. `ffprobe` (from FFmpeg) is optional and only needed for `--verify=deep` and
`--require-audio`.

## Quick start

The destination must already exist. Then, on a new machine:

```console
updl -a 192.168.1.1 -u archiver -p '...' -d /srv/protect
```

If the account uses multi-factor authentication you are prompted for a code once. The
session token is cached, so you are not asked again until it expires.

**Every run after that needs no arguments at all:**

```console
updl
```

The console address, account and destination are remembered from the last successful run.
Your password is **never** written to disk — it is asked for again only when the cached
token expires.

Prefer the environment to flags for the password, since a command-line argument is visible
to every other process on the machine:

```console
export PROTECT_PASSWORD='...'
updl
```

Every option has a matching `PROTECT_*` environment variable. Precedence is: an explicit
flag, then the environment variable, then what was remembered, then the built-in default.

Where state is kept — override the first two with `UPDL_CONFIG` and `PROTECT_SESSION_STORE`:

| What | Windows | Linux / macOS |
| --- | --- | --- |
| Remembered settings | `%LOCALAPPDATA%\updl\config.json` | `~/.config/updl/config.json` |
| Session token | `%LOCALAPPDATA%\protect-archiver\sessions.json` | `~/.local/state/protect-archiver/sessions.json` |
| Archive index | `DEST\.protect-archive\manifest.db` | `DEST/.protect-archive/manifest.db` |

## Commands

Syncing is the primary command: use `updl [DEST]`, or plain `updl` after the first
successful run remembers the destination. The explicit `sync` name remains available for
existing scripts:

| Command | What it does |
| --- | --- |
| `updl [DEST]` | Incremental mirror. Sweeps each camera's retention window and fetches only what is missing or damaged. |
| `updl sync [DEST]` | Backwards-compatible explicit form of the primary command. |
| `updl verify [DEST]` | Audits an archive **offline** — never contacts the NVR, so it is safe to run against a backup copy. |
| `updl download DEST` | One-off download of an explicit `--start`/`--end` range. |
| `updl events DEST` | Motion and smart-detection event clips only. |

## How re-running avoids re-downloading

A SQLite manifest at `DEST/.protect-archive/manifest.db` holds one row per hour per
camera, keyed on `(camera_id, start_ms)`, recording the path, size, SHA-256 and a status:

- **`ok`** — downloaded and accounted for.
- **`empty`** — the NVR reported no footage for that hour, so it is never requested again
  and the gap stays visible as a deliberate record rather than as an error.
- **`failed`** — the download failed; a later run retries exactly that hour.

Because the manifest is authoritative, `sync` sweeps the whole retention window every run
and still costs nothing when there is nothing to do. Gaps left by earlier failures are
filled automatically, and footage that has since aged off the NVR stays in the archive.

## Verifying what is on disk

`--verify` controls how hard `sync` works to prove an existing file is intact before
skipping it. `updl verify` accepts the same levels for an offline audit.

| Level | Checks | Cost |
| --- | --- | --- |
| `none` | trusts the manifest | touches no files |
| `quick` *(default)* | file exists at the recorded size | one `stat` per segment |
| `hash` | SHA-256 recomputed | reads the whole archive |
| `deep` | + `ffprobe` decodes it | catches a file that is the right size and hash but unplayable |

```console
# Audit, then queue anything damaged for the next sync to re-fetch.
updl verify /srv/protect --level hash --repair
```

Other `verify` flags: `--require-audio` (see below), `--rehash` to fill in hashes for rows
adopted from disk, `--fix-timestamps`, and `--clean-partials`.

## Things this handles that are easy to get wrong

**Audio is silently dropped for under-permissioned accounts.** If the Protect account
lacks `readmedia` on a camera, the export still returns 200 with intact video — just no
audio track. Nothing fails, so an archive can accumulate for weeks before anyone plays a
clip and finds it silent. `updl verify DEST --require-audio` inspects the streams and
reports it; add `--repair` to re-fetch. If it flags everything, grant the account camera
media permission and run it again.

**"No footage" is reported as an HTTP 404 carrying `{"error": 502}`.** Neither number
means what it looks like. Treated as a failure, every hour a camera was offline would be
re-requested forever; `updl` records it as `empty` instead.

**Downloads are atomic.** Bytes land in a `.part` file and are renamed into place only on
success, so an interrupted run cannot leave a truncated MP4 that later runs mistake for a
complete one. Stale partials are swept at startup.

**Transient server errors are retried.** The export endpoint routinely returns 500 on a
busy NVR. `updl` backs off exponentially on 5xx/429/408, does not retry other 4xx, and
refreshes an expired session on 401 without spending a retry.

**Files are dated by recording time, not download time.** Protect stamps each exported MP4
at the moment of export, so an archive fetched today would otherwise show today's date for
last week's footage.

## Accounts and multi-factor authentication

The account must be able to read camera media. A Protect account lacking `readmedia` on a
camera gets video without audio rather than an error — see above.

For consoles backed by Ubiquiti SSO with a second factor, `updl` handles the two-step
login and **caches the session token** (`--no-session-store` disables this), so a code is
needed occasionally rather than on every run. Supply one non-interactively with
`--mfa-code` / `PROTECT_MFA_CODE`; otherwise it prompts. With no terminal attached it
fails with an explanation rather than hanging, which is what a scheduled run needs.

Where the second factor is delivered by email there is no shared secret, so no run can
obtain a *new* token unattended — but an existing token is long-lived, so a scheduled run
works until it expires.

If camera discovery reports a successful HTTP request but then says the response was empty,
HTML, invalid JSON, or unexpected metadata, the console did not return a usable camera list.
One possible cause of HTML is a login page returned for a cached session the console no
longer accepts; the token's displayed expiry does not prove that it remains valid server-side.
`updl` refreshes once for a 401, a same-console login redirect, or an HTML response, then
fails clearly. Other invalid responses fail directly. If the problem persists, check that
the configured address and port lead to the UniFi Protect console and that the account can
access Protect.

## Recovering an archive whose manifest was lost

`updl DEST --reconcile` reads the footage already on disk and rebuilds the manifest
rows from the filenames, rather than downloading terabytes again. Adopted rows carry no
hash until `updl verify DEST --rehash` records one.

## Credits

A fork of [`danielfernau/unifi-protect-video-downloader`](https://github.com/danielfernau/unifi-protect-video-downloader)
by Daniel Fernau, David Cramer and contributors, which does the hard work of talking to
the Protect API. See [NOTICE](NOTICE) for what this fork changed and why. MIT licensed;
the original copyright notice is retained in [LICENSE](LICENSE).

## Contributing

```console
poetry install
poetry run pytest
poetry run mypy .
poetry run flake8 protect_archiver conftest.py
```
