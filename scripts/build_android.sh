#!/usr/bin/env sh
set -eu
configuration="${1:-debug}"
case "$configuration" in
  debug) task=assembleDebug ;;
  release) task=assembleRelease ;;
  *) echo 'Usage: build_android.sh [debug|release]' >&2; exit 2 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -f "$root/client/index.html" ]; then
  echo 'Missing client/index.html. Add the shared frontend before building.' >&2
  exit 1
fi
cd "$root/android"
sh ./gradlew --no-daemon "$task"
apk="$root/android/app/build/outputs/apk/$configuration/app-$configuration.apk"
test -f "$apk"
printf 'APK: %s\n' "$apk"
echo 'Signing: Android SDK debug key (also for release); direct installation only, not a production signing identity.'
