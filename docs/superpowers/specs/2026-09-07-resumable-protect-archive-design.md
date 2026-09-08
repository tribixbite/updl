# Resumable UniFi Protect archive — design

Date: 2026-09-07
Status: approved

## Goal

Back up UniFi Protect footage from the UDM Pro SE to `D:\Unifi` such that the command
can be re-run at any future date, downloads nothing it already holds, fills gaps left by
earlier failures, and can optionally verify that what is on disk is not corrupt.

## Environment (verified, not assumed)

| Fact | Value | How it was established |
| --- | --- | --- |
| NVR | UDM Pro SE, "the NVR", `protect.invalid` | `GET /api/system` → `{"hardware":{"shortname":"UDMPROSE"}}` |
| Reachability | 2 hops via the UDR at `gateway.invalid` | `tracert`; TCP 443 open; ICMP blocked |
| Protocol | HTTPS (port 80 301-redirects) | `GET http://protect.invalid/api/system` → 301 |
| Second console | UDR "a second console", `gateway.invalid` | subnet scan; not in scope |
| Account | `archiver`, Ubiquiti SSO, MFA required | login → 499 `MFA_AUTH_REQUIRED` |
| Second factor | `type: "email"` — no TOTP seed exists | `authenticators[0].type` in the 499 body |
| MFA challenge cookie | `UBIC_2FA`, **10 minute** lifetime | decoded JWT `exp` claim |
| Archive volume | `D:\`, 5.2 TB free of 7.3 TB | `df -h /d` |

## Defects being fixed

These are why the tool cannot currently be re-run safely.

1. **HTTP errors are never retried.** `download_file.py` increments `files_failed` on a
   non-200 and then falls into the `try/else: return`. The retry loop only ever runs for
   a `RequestException`. A transient 500 from the export endpoint becomes a permanent gap.
2. **Writes are not atomic.** Content streams into the final `.mp4`. An interrupted run
   leaves a truncated file that `--skip-existing-files` then skips forever.
3. **`sync` never passes `skip_existing_files`**, so it defaults to `False`.
4. **The statefile advances past failures**, so a gap is never revisited.
5. **`get_camera_list` builds `recording_start` with `datetime.utcfromtimestamp`** (naive
   UTC) while the rest of the code uses naive *local* time, so the first sync starts at the
   wrong point. Also removed in Python 3.12.
6. **No integrity concept at all** — the code carries a `TODO` saying exactly this.

## Architecture

Changes land in **new modules** wherever possible; edits to upstream files are kept to
small hunks, because this fork tracks `danielfernau` and every in-tree change has to be
carried across future merges.

### `session_store.py` (new)

`/api/auth/login` is rate-limited and the second factor arrives by email, so a session
token is persisted and reused until it expires. Stored **outside** the archive (an archive
gets copied to external media; a session token is a credential) under
`%LOCALAPPDATA%\protect-archiver\sessions.json`, opened `0o600`, keyed by
`address:port/username`. Expiry is read from the token's own JWT `exp` claim.

### `client/unifi_os.py` (edit)

Login becomes: reuse a stored unexpired token → else POST credentials → on 499
`MFA_AUTH_REQUIRED`, capture `UBIC_2FA` and resubmit within its 10-minute window with a
`token` field holding the emailed code. The code comes from `--mfa-code`/`PROTECT_MFA_CODE`
if supplied, otherwise an interactive prompt, otherwise a clear error — never a silent hang.

### `manifest.py` (new) — SQLite at `<DEST>/.protect-archive/manifest.db`

```
segments(camera_id, start_ms, end_ms) PRIMARY KEY
        camera_name, path, size, sha256, status, downloaded_at, verified_at
meta(key, value)
```

`status` is `ok` | `empty` | `failed`. Recording `empty` (the sub-300-byte "no footage this
hour" case) stops it being re-requested forever and makes real gaps auditable. Recording
`failed` is what lets a later run retry precisely the holes. `meta` stores the archive
layout (`use_subfolders`, `use_utc_filenames`) and the run warns if a later invocation
disagrees, which would otherwise silently build a second parallel copy.

SQLite rather than one JSON blob: a 4-camera year is ~35k rows per camera and rewriting a
JSON document once per segment is O(n²).

### `verify.py` (new)

| level | check | cost |
| --- | --- | --- |
| `none` (default) | manifest `ok` + file exists + size matches | one `stat` |
| `hash` | + SHA-256 recomputed | reads the file |
| `deep` | + `ffprobe` decodes it, duration is sane | catches truncated MP4 |

`deep` degrades to `hash` with a warning when `ffprobe` is absent. Any mismatch re-downloads.

### `cli/verify.py` (new) — audits the archive without contacting the NVR

Reports ok / missing / size-mismatch / hash-mismatch / undecodable. `--repair` marks bad
rows for the next sync to refetch. `--reconcile` adopts files already on disk that predate
the manifest, so an existing archive is indexed rather than re-downloaded.

### `sync.py` + `cli/sync.py` (edit)

Sweep `recording_start → now` on every run with the completed set preloaded into memory, so
skips are instant and touch no network. Gap-filling becomes automatic, and footage that has
since aged off the NVR stays in the archive because `recording_start` moves forward with NVR
retention. The manifest is authoritative; `sync.state` is still written for upstream
compatibility but no longer gates downloads. `--ignore-state` keeps its documented meaning
of "re-download everything" by ignoring the manifest too.

Also: atomic `.part` + `os.replace` writes with stale-`.part` cleanup at startup, real
exponential backoff on 5xx/429 (no retry on 4xx, 401 re-auths once), and the options `sync`
was missing — `--skip-existing-files`, `--wait-between-downloads`,
`--download-request-timeout`, `--max-retries`, `--verify`.

## Consequence the user has accepted

Email-delivered MFA means there is no unattended mode beyond the session token's lifetime.
Scheduling is therefore out of scope; a run is started by hand and a code is typed when the
stored token has expired. Token persistence exists to make that rare rather than per-run.

## Out of scope

Retention/pruning of `D:\Unifi`. Deleting footage should be a deliberate, separate decision.
