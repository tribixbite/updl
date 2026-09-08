# unifi-protect-video-downloader — working context

## The task

Back up UniFi Protect footage to a local archive at `D:\Unifi` on this Windows box, in a
way that can be re-run at any future date without re-downloading anything it already
holds, and that can optionally prove what is on disk is not corrupt.

Built and proven against the live NVR on 2026-09-07 (design:
`docs/superpowers/specs/2026-09-07-resumable-protect-archive-design.md`).

Measured behaviour of three consecutive runs against `D:\Unifi`:

| Run | Wall clock | Downloaded | Already archived | Export requests |
| --- | --- | --- | --- | --- |
| 1 (backfill) | ~1 h | 243 segments, 58.5 GB | 0 | 364 |
| 2 | 19.6 s | 2 (newly elapsed hours) | 243 | 124 |
| 3 | **0.35 s** | 0 | 367 | **0** |

Run 3 issued no export requests and no logins at all. That is the property the whole
design exists for: re-running later costs nothing and re-downloads nothing.

Corruption handling was verified by flipping bits in a segment *without changing its
size*: `--verify=quick` correctly did not notice, `--verify=hash` reported the exact file
with both hashes, `verify --repair` marked it, and the next sync re-fetched precisely that
one segment (366 already archived). `sync --verify=hash` does the same in a single step.
`verify --level deep` ran ffprobe over all 367 segments and passed.

## Verified environment

Established by probing, not assumed. Re-check before trusting any of it.

| Fact | Value |
| --- | --- |
| NVR | UDM Pro SE, "the NVR", **protect.invalid** |
| Reachability | 2 hops from this box, routed via the UDR at gateway.invalid. **ICMP is blocked** — `ping` fails while TCP 443 is open, so test with `Test-NetConnection -Port 443` |
| Protocol | HTTPS. Port 80 301-redirects, so the default `https` protocol is right |
| Second console | UDR "a second console", gateway.invalid — *not* the one with the cameras |
| Account | `archiver`, Ubiquiti SSO |
| Archive | `D:\Unifi`, 5.2 TB free of 7.3 TB |

## Authentication — the thing that shapes everything else

`archiver` is an **SSO account with an email second factor**. A credentials-only
`POST /api/auth/login` returns HTTP **499** with `MFA_AUTH_REQUIRED`; the code is emailed
to the address on the UI account. Consequences that are easy to get wrong:

- **There is no TOTP seed**, because the enrolled authenticator is `type: "email"`. Nothing
  can generate these codes offline. Storing a shared secret is not an option here.
- **`UBIC_2FA` is a 10-minute challenge cookie, not a remembered-device cookie** (decoded
  from its own JWT `exp`). It cannot be reused to skip 2FA on a later run.
- **`/api/auth/login` is rate limited.** Do not loop on it while debugging.
- Therefore **the session token is cached between runs** in
  `%LOCALAPPDATA%\protect-archiver\sessions.json` (override with `PROTECT_SESSION_STORE`,
  written `0o600`). This is what makes "type the code every time" actually mean
  "occasionally". `--no-session-store` disables it.
- **A code can be supplied with `--mfa-code` / `PROTECT_MFA_CODE`**, otherwise it is
  prompted for. With no terminal attached it fails with an explanation rather than hanging,
  which is the only sane behaviour for a piped or scheduled run.
- **`rememberMe: true` yields a 30-day session token.** Measured against this UDM Pro SE on
  2026-09-07: the returned JWT's `exp` was 720 h out. So in practice a code is typed about
  once a month, not once a run — which is why caching the token was worth building.

Because the factor is email, **no run can obtain a *new* token unattended**. But since a
token lasts 30 days, a scheduled daily run is in fact viable: it works untouched for a
month and then fails with a clear message until someone runs it once by hand to refresh.
Nothing is scheduled at present — that was left as the operator's call.

`PROTECT_EMAIL` is set in the environment but **this tool does not use it** — the CLI
authenticates with `--username`, not an email address.

## The cameras, and what they cost to archive

Measured on the first real run, 2026-09-07. **Segment size varies by two orders of
magnitude depending on the camera's recording mode**, so do not estimate the archive from
a segment count alone.

| Camera | ID | Mode | Per hour |
| --- | --- | --- | --- |
| G4 Instant | `cam00000000000000000001` | continuous | ~424 MB |
| G4 Instant | `cam00000000000000000003` | continuous | ~424 MB |
| G4 Doorbell Pro | `cam00000000000000000002` | detection only | ~6 MB |
| G4 Instant | `cam00000000000000000004` | no recordings | — skipped |

The two "G4 Instant" cameras share a display name; the filesystem-safe name disambiguates
them with the last four characters of the camera id (`G4 Instant (e745)` vs
`G4 Instant (3fef)`), so they do not collide on disk.

NVR retention at first run was 4–6 days, ~363 hourly segments, roughly 90 GB. Throughput
over the LAN was ~18 MB/s, so a full backfill takes on the order of an hour or two.

A consequence worth remembering: **an hour of a detection-only camera decodes to ~30 s of
video**, not 3600 s. `--verify=deep` therefore only asserts that ffprobe reports a positive
duration; asserting a duration near 3600 would fail every doorbell segment.

## "No footage in that range" is an HTTP 404 carrying `{"error": 502}`

Neither number means what it looks like, and this is the single most confusing thing the
export endpoint does. Established on 2026-09-07 by probing a range 30 days in the future
and one long before retention began — **both** return exactly `404 {"error": 502,
"operationId": N}`, while the hours either side of a real gap return 200 and video. It
reproduces identically on every attempt, so it is not transient.

The first live run hit this on 11 hours where a camera had been offline. It matters which
way it is classified:

- as a **failure**, every one of those hours is re-requested on every future run forever,
  and every run ends by reporting failures no amount of retrying can fix;
- as **empty**, it settles once and is never asked for again, and the gap stays visible in
  the manifest as a deliberate record rather than as an error.

`_reports_no_footage` in `downloader/download_file.py` matches that exact pairing and
nothing looser — a bare 404, or a 404 with any other error code, is still a real failure,
because a malformed request produces one too. Rows already recorded as `failed` heal
themselves: the next run retries them, gets this response, and rewrites them as `empty`.

## Running it

```powershell
# D:\Unifi must already exist: DEST is click.Path(exists=True), checked before anything runs
protect-archiver sync D:\Unifi --address protect.invalid

# or the wrapper, which adds a single-instance lock, a log, and a volume-mounted check
.\scripts\protect-sync.ps1
```

Credentials come from `PROTECT_USERNAME` / `PROTECT_PASSWORD` in the environment. Never
pass a password as an argument — it is visible to every other process on the machine.

Useful commands:

- **`sync DEST`** — the incremental mirror. Sweeps each camera's whole retention window
  every run and downloads only what is missing or damaged.
- **`verify DEST`** — audits the archive **offline**, never contacting the NVR. `--repair`
  marks bad segments for the next sync; `--rehash` fills in hashes for adopted rows.
- **`download DEST`** — explicit `--start`/`--end` range, for a one-off. Note it does *not*
  write to the manifest; only `sync` does.
- **`events DEST`** — motion/smart-detect clips only.

## How re-running without re-downloading works

Upstream tracked progress in `sync.state`, one cursor per camera. That cannot express what
this job needs, so a **SQLite manifest** at `<DEST>\.protect-archive\manifest.db` is now
the authority. One row per hour per camera, keyed on `(camera_id, start_ms)`, recording
path, size, SHA-256 and a status of `ok` / `empty` / `failed`.

- `empty` records an hour the NVR had no footage for, so it is never re-requested and real
  gaps stay auditable.
- `failed` is what makes a *later* run retry precisely the hours that broke — the cursor
  used to advance straight past them, making a gap permanent.
- `sync.state` is still written for upstream compatibility but no longer gates downloads.
- `--ignore-state` keeps its documented meaning of "re-download everything" by ignoring the
  manifest too.

Verification levels, applied before skipping anything (`--verify`):

| level | check | cost |
| --- | --- | --- |
| `none` | trust the manifest | touches no files |
| `quick` *(default)* | exists, and size matches | one `stat` per segment |
| `hash` | SHA-256 recomputed | reads the whole archive |
| `deep` | + ffprobe decodes it | degrades to `hash` with a warning if ffprobe is absent |

If the manifest is ever lost, **`sync --reconcile`** adopts what is on disk by parsing the
filenames back into camera and start time, rather than re-downloading terabytes. Adopted
rows carry no hash until `verify --rehash` is run.

## Defects fixed in this fork (do not "simplify" these back out)

Each of these was load-bearing for a re-runnable backup:

1. **`download_file` never retried HTTP errors.** A non-200 incremented `files_failed` then
   fell into the `try/else: return`; only `RequestException` ever reached the retry loop. A
   transient 500 from the export endpoint became a permanent gap. Now: exponential backoff
   on 5xx/429/408, no retry on other 4xx, 401 re-auths once without spending a retry.
2. **Writes were not atomic.** Content streamed straight into the final `.mp4`, so an
   interrupted run left a truncated file indistinguishable from a complete one. Now written
   to `.part` and `os.replace`d on success, with stale `.part` files swept at startup.
3. **`sync` never passed `skip_existing_files`** (`cli/sync.py` simply omitted it).
4. **`get_camera_list` used `datetime.utcfromtimestamp`** for `recording_start` while the
   rest of the codebase uses naive *local* time — `interval.timestamp()` reads a naive value
   as local. Every camera's first sync therefore started a whole UTC offset away from the
   real recording start. (Also removed in Python 3.12.)
5. **A camera reporting `datetime.min`** (never recorded) would have swept from year 1,
   expanding to millions of hourly requests. Now skipped with a warning.
6. **`format_bytes` used integer division**, so 1536 bytes printed as `1.0 kb`.

## Gotchas

- **This machine writes CRLF by default; the repo is LF.** A `pathlib.write_text()` in a
  helper script will silently rewrite a whole file's line endings and produce a diff of
  hundreds of lines. Check `git diff --stat` for implausible line counts, and normalise
  with a bytes-level `b"\r\n" -> b"\n"` pass before committing.
- `pyproject.toml` sets black's `experimental_string_processing`, which modern black
  rejects as an invalid key (pre-commit pins black 22.1.0, where it was valid). Harmless,
  but it means local black and the hook can format strings differently.
- `mypy.ini` overrides `[tool.mypy]` in `pyproject.toml` and sets
  `disallow_untyped_defs = True`, so every function needs annotations.
- `VERIFY_SSL` defaults to false and the CLI silences urllib3's `InsecureRequestWarning` —
  expected for a self-signed NVR certificate on the LAN, not a bug.
- Tests must never touch the real session store; `conftest.py` has an autouse fixture
  pointing `PROTECT_SESSION_STORE` at a tmp path. Keep it.

## Repo etiquette

This is a fork of `danielfernau/unifi-protect-video-downloader` tracking an active
upstream, so new logic lives in **new modules** (`manifest.py`, `verify.py`,
`reconcile.py`, `session_store.py`, `cli/verify.py`) and edits to upstream files are kept
to small, obvious hunks that survive a merge. Prefer that shape for anything added later.

Checks before committing:

```powershell
python -m pytest -q; python -m mypy .; python -m flake8 protect_archiver conftest.py
```
