# Android — 0x4b6f757a7a

Native Java + platform WebView. Application ID: `dev.kouzz.console`.
Android 8.0/API 26 or later. No external Java runtime is needed on the phone.

## Build

Install JDK 17, Android SDK platform 35 and build-tools 35.0.0. Set `JAVA_HOME`
and `ANDROID_HOME` (or use `android/local.properties` with `sdk.dir`).
AGP 8.7.3 and Gradle 8.9 are pinned; the checked-in Gradle wrapper validates
the distribution SHA-256. Official compatibility reference:
https://developer.android.com/build/releases/agp-8-7-0-release-notes
Official Gradle checksums: https://gradle.org/release-checksums/

From the repository root:

```powershell
./scripts/build_android.ps1 -Configuration release
```

```sh
sh scripts/build_android.sh release
```

Or `cd android && ./gradlew assembleRelease` (`gradlew.bat` on Windows).
Gradle copies `client/` to generated APK assets automatically. The shared client
must provide `client/index.html` and its referenced static files. Missing HTML
fails the build instead of packaging an empty application. Source files in
`client/` are never rewritten by this project.

Output: `android/app/build/outputs/apk/release/app-release.apk`.
Both release and debug APKs use **Android SDK debug signing**. They are installable
directly, but this is not a production signing identity or a Play publishing setup.
Different build machines may generate different debug keys; replacing an existing
installation signed by another key requires uninstalling it (which clears local
settings). No signing secrets, GitHub tokens or SMTP credentials belong in assets.

Application packages are built locally. No workflow publishes or uploads them.
Optional personal icons are loaded from `.local/icons/android`, outside Git.

## Frontend bridge contract

The entry point is `file:///android_asset/client/index.html`.
The native object is present before page JavaScript runs:

```js
window.Native.request(method, path, JSON.stringify(body || {}), callbackId);
window.__nativeResolve = (callbackId, result) => {
  // result is an object, not a JSON string:
  // {ok: true, data: ...} or {ok: false, error: "..."}
};
```

The callback runs on the WebView UI thread via `evaluateJavascript`.
Only these exact routes/methods are accepted:

| Route | Result data |
| --- | --- |
| `GET /api/settings` | `{rooms:[{id,name}], repository, refresh_interval, workflow, recipients}` |
| `PUT /api/settings` | Merged, validated, persisted settings; the same shape |
| `GET /api/status` | `{rooms:[...], checked_at, settings, platform:'android', mail_available:false, mail_notice}` |
| `POST /api/refresh` | Queries rooms then returns the same shape as status |
| `GET /api/health` | Public heartbeat plus Actions information, described below |

Source defaults have an empty watchlist, repository `Nax1ta/0x4b6f757a7a`,
refresh interval 60 seconds and workflow
`poll-notify.yml` (its display name may be `orbit`). Room IDs are digit strings;
0–30 rooms are supported. Interval accepts 15–3600 seconds. The frontend owns
its polling timer; native code queries only when `POST /api/refresh` is requested.
Settings and observations survive process restarts through private SharedPreferences.
On first launch, optional `assets/client-defaults.json` can provide `rooms` and
`repository`. The file is generated privately by `scripts/bootstrap.py` from the
packager's environment and is ignored by Git at
`android/app/src/main/assets/client-defaults.json`. Existing saved settings take
precedence. The installed APK contains any exported watchlist but no authentication.
Public source and this documentation contain no personal room list or live titles.
Recipient addresses, if supplied, are local informational settings only: this
app does not send email or synchronize changes into GitHub's `ENV_FILE`.
Unknown setting keys are rejected, including credentials and arbitrary API URLs.

Room fields: `id`, `name`, `url`, `status` (`live|offline|unknown`), `live_status`
(`0|1|2|null`), `title`, `cover`, `live_started_at`, `last_live_at`,
`last_live_estimated`, `checked_at` (last successful room query), `attempted_at`,
`error`, `stale`, `source`, `metadata_partial` and `last_success_at` when known.
Timestamps are ISO 8601 UTC or null. Empty metadata stays empty until a genuine
API result provides it. Existing real title/cover/history survives transient errors.
Failed or invalid status responses always produce `unknown`, never `offline`.
Only numeric integer B站 `live_status` values 0, 1, 2 are accepted; 1 maps to live,
0 and 2 to offline, with raw status preserved.

Primary query: `room/v1/Room/get_info?room_id=…`. If that fails, native code calls
`room/v1/Room/room_init?id=…`, then attempts `get_info` with the canonical room ID
to supplement metadata. B站 date strings are interpreted in UTC+8. Missing live
start times remain null. A newly observed live session may use observation time
for `last_live_at`, explicitly marked `last_live_estimated:true`; continuing
observations retain that value. Room queries use four worker threads and an
overall 22-second refresh budget. Partial failures return individual unknown rooms.

Health reads `https://raw.githubusercontent.com/{repository}/orbit-state/health.json`
and `https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?per_page=15`.
It returns `status`, `stale`, `last_checked_at`, `last_mail_at`, `pending`,
`failures`, `latest_run`, `runs`, `repository_url`, `checked_at`, `error` and
`delivery_note`, and the heartbeat's `mail_status` (including `unavailable`).
A heartbeat older than 900 seconds yields `status:'stale'`. With a fresh heartbeat,
a latest Actions conclusion of `failure` or `timed_out` overrides `ok` to `degraded`.
Missing heartbeat yields `unknown`; Actions success alone never proves mail delivery.
GitHub public API rate limiting is surfaced without requesting a token.
Actions responses (including failures) are cached for 300 seconds per repository
and workflow, using a monotonic clock; raw heartbeat is read on every health
request. `actions_checked_at` exposes the actual cached query time. A frontend
poll every 60 seconds therefore normally spends 12 Actions API calls per hour.

## WebView isolation

Only `file:///android_asset/client/…` documents and scripts are admitted.
The resource interceptor rejects other local files, content URIs and remote
HTML/scripts. CSP is injected into generated HTML assets during the build (asset
URLs can bypass the resource interceptor). It prohibits frames, workers, objects, network fetch and forms;
HTTPS images are the only remote page resources allowed. Local file-origin
access is enabled for the shared client's ES modules, with every request
restricted by the interceptor. Universal file-origin access stays disabled.
The bridge accepts fixed routes; callers cannot supply arbitrary network URLs.
HTTP(S) external links with valid hosts open in the system browser; `javascript:`,
`intent:`, content and external file navigation are blocked. SSL errors use
WebView's default cancellation. Back returns through local history or exits.

No background service or email delivery is implemented. Cloud Actions remains
responsible for unattended monitoring and mail delivery.
