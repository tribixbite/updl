# updl — working context

Resumable, verifiable archiver for UniFi Protect footage. A fork of
`danielfernau/unifi-protect-video-downloader` with upstream history preserved.

Deployment-specific details — console address, account, camera inventory, archive path —
are deliberately **not** recorded here, because this repository is public. Keep them in a
local, untracked note or in the environment.

## Design in one paragraph

The unit of work is one hour of one camera's recording. Every run sweeps the whole window
the NVR still holds and asks a SQLite manifest about each hour in it, so a run started
weeks later downloads exactly the hours that are missing or damaged. Sweeping rather than
resuming from a cursor is also what keeps the archive correct as footage ages out:
`recording_start` moves forward with the NVR's own retention, so the window shrinks to
what is still fetchable while everything already archived stays put.

## The manifest

`DEST/.protect-archive/manifest.db`, one row per `(camera_id, start_ms)`, holding path,
size, SHA-256 and a status of `ok` / `empty` / `failed`.

- `empty` records an hour the NVR had no footage for, so it is never re-requested and real
  gaps stay auditable.
- `failed` is what makes a later run retry precisely the hours that broke — the upstream
  statefile cursor advanced straight past them, making a gap permanent.
- `sync.state` is still written for upstream compatibility but no longer gates downloads.
- `--ignore-state` keeps its documented meaning of "re-download everything" by ignoring
  the manifest too.

The end of a segment is stored but deliberately **not** part of its identity: a sync always
requests hour-aligned ranges, and a two-field key is what lets an archive whose manifest was
lost be rebuilt from the filenames on disk (`sync --reconcile`).

SQLite rather than JSON because a four-camera year is ~35k rows per camera and rewriting a
JSON document once per segment is quadratic.

## Protect API behaviour that is easy to get wrong

**"No footage in this range" is an HTTP 404 carrying `{"error": 502}`.** Neither number
means what it looks like. Established by probing a range far in the future and one long
before retention began — both return exactly that, while the hours either side of a real
gap return 200 and video, reproducibly. Classified as a failure, every hour a camera was
offline would be re-requested forever and every run would end reporting failures nothing
can fix. `_reports_no_footage` matches that exact pairing and nothing looser: a bare 404, or
a 404 with any other error code, is still a real failure, because a malformed request
produces one too.

**Audio is omitted silently for an under-permissioned account.** If the Protect account
lacks `readmedia` on a camera, the export still returns 200 with intact video and no audio
track. Nothing fails; size and hash are self-consistent; the file decodes. The only symptom
is silence on playback, so an archive can accumulate for weeks before anyone notices. This
was observed for real: an entire multi-gigabyte archive was silent because the account's
only camera grant pointed at a camera id that no longer existed on the NVR. Granting camera
media permission fixed it with no code change. `verify --require-audio` exists to catch it.

**The export endpoint dates each MP4 at the moment of export**, not when the footage was
recorded, so files must be re-stamped or the archive sorts by collection date.

**Cameras in `detections` recording mode leave genuinely empty hours**, and an hour of such
a camera can decode to a few seconds of video. `--verify=deep` therefore only asserts a
positive duration; asserting anything near 3600 s would fail every such segment.

**Segment size varies by orders of magnitude** between continuous and detection-only
cameras, so never estimate an archive's size from a segment count.

**The console stores more than the stream this tool fetches.** `nvr.storageStats.recording
Distribution` splits into `hq`, `lq` and `timelapse`; `/video/export` serves `hq`. Comparing
the archive against the console's total disk usage will look like a large shortfall when
nothing is missing. The other streams *are* exportable (`&channel=2`, `&type=timelapse`) —
they are simply not requested. An unrecognised value such as `&channel=timelapse` makes the
export hang rather than error, so validate before passing anything through.

## Authentication

Consoles backed by Ubiquiti SSO answer a credentials-only login with HTTP **499** and
`MFA_AUTH_REQUIRED`. The `UBIC_2FA` cookie returned with it is a short-lived challenge
(~10 minutes), not a remembered-device cookie, and `/api/auth/login` is rate limited — so
the session token is cached per user in `%LOCALAPPDATA%\protect-archiver\sessions.json`
(override with `PROTECT_SESSION_STORE`, written `0o600`). `rememberMe: true` yields a
long-lived token, which is what makes repeat runs practical.

Where the second factor is delivered by email there is no shared secret, so no run can
obtain a *new* token unattended; an existing one lasts long enough that a scheduled run
works until it expires. With no terminal attached the code prompt fails with an explanation
rather than hanging.

The **official** Protect integration API (`developer.ui.com/protect/<version>/openapi.json`)
has no video-export endpoint at all — only live `rtsps-stream` and snapshots. `/video/export`
is the private API, which is why behaviour has to be established by probing.

## Defects fixed in this fork — do not "simplify" these back out

1. `download_file` never retried HTTP errors: a non-200 fell into `try/else: return`, so
   only `RequestException` reached the retry loop and a transient 500 became a permanent
   gap. Now backs off on 5xx/429/408, does not retry other 4xx, re-auths once on 401.
2. Downloads were not atomic, so an interrupted run left a truncated MP4 indistinguishable
   from a complete one. Now `.part` + `os.replace`, with stale partials swept at startup.
3. `sync` never passed `skip_existing_files`.
4. `get_camera_list` read `recording_start` as naive UTC while the rest of the code uses
   naive local, shifting every camera's first sync by the UTC offset.
5. A camera reporting `datetime.min` would have swept from year 1.
6. `format_bytes` used integer division, printing 1536 bytes as "1.0 kb".

## Packaging

Published as **`updl`**; CLI command `updl`, with `protect-archiver` kept as an alias. **The
import package stays `protect_archiver`** (the `pillow`/`PIL` pattern) so fixes can still be
merged from upstream rather than hand-ported — do not rename it without accepting that cost.

Release: bump `version` in `pyproject.toml`, tag `vX.Y.Z`, publish a GitHub Release.
`.github/workflows/publish.yml` runs tests/mypy/flake8, refuses a tag that disagrees with
the project version, and uploads via PyPI Trusted Publishing (OIDC) — no API token in the
repo. `workflow_dispatch` targets TestPyPI for a dry run.

`dockerbuild.yml` was deleted because it published to the upstream project's Docker Hub
namespace on every `v*` tag; the `Makefile` targets this fork's namespace and does not push
as part of `all`.

## Gotchas for anyone working here

- **Windows writes CRLF by default; this repo is LF.** A `pathlib.write_text()` in a helper
  script silently rewrites a whole file's line endings and produces a diff of hundreds of
  lines. Check `git diff --stat` for implausible counts and normalise with a bytes-level
  `b"\r\n" -> b"\n"` pass before committing.
- `mypy.ini` overrides `[tool.mypy]` in `pyproject.toml` and sets `disallow_untyped_defs`,
  so every function needs annotations.
- `VERIFY_SSL` defaults to false and the CLI silences urllib3's `InsecureRequestWarning` —
  expected for a self-signed NVR certificate on a LAN, not a bug.
- Tests must never touch the real session store; `conftest.py` has an autouse fixture
  pointing `PROTECT_SESSION_STORE` at a tmp path. Keep it.
- Test fixtures must not carry real account UUIDs, hostnames or camera ids. Use
  `*.invalid` hostnames and obviously-synthetic identifiers.
- `.gitignore` excludes `*.png` because investigating the Protect UI leaves screenshots of
  live camera footage in the working tree.

New logic goes in **new modules** and edits to upstream files stay small and obvious, so
future merges from upstream remain possible.

Checks before committing:

```powershell
python -m pytest -q; python -m mypy .; python -m flake8 protect_archiver conftest.py
```
