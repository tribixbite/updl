# unifi-protect-video-downloader — working context

## The task

Back up UniFi Protect footage to a local archive at `D:\Unifi` on this Windows box, in a
way that can be re-run at any future date without re-downloading anything it already
holds, and that can optionally prove what is on disk is not corrupt.

The archiving machinery for this was built on 2026-09-07 (see
`docs/superpowers/specs/2026-09-07-resumable-protect-archive-design.md`). Nothing has
been synced from the live NVR yet — the first real run is still pending.

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

Because the factor is email, **there is no unattended mode**. Scheduling is deliberately
out of scope; a run is started by hand.

`PROTECT_EMAIL` is set in the environment but **this tool does not use it** — the CLI
authenticates with `--username`, not an email address.

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
