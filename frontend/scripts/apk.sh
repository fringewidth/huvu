#!/usr/bin/env bash
# Builds a debug APK for one native target: phone or firetv.
#
# phone is the Capacitor app. Capacitor copies the web build into the APK,
# so the web build, the copy and the APK always go together.
#
# firetv is the TV app (android/tv), a WebView over the TV's page on the
# host, so its APK carries no web build.
#
#   scripts/apk.sh phone
#   scripts/apk.sh firetv
set -euo pipefail

cd "$(dirname "$0")/.."
export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"

case "${1:-}" in
  phone)
    npm run build:phone
    npx cap sync android
    (cd android && ./gradlew :app:assembleDebug)
    echo "android/app/build/outputs/apk/debug/app-debug.apk"
    ;;
  firetv)
    (cd android && ./gradlew :tv:assembleDebug)
    echo "android/tv/build/outputs/apk/debug/tv-debug.apk"
    ;;
  *)
    echo "usage: $0 phone|firetv" >&2
    exit 2
    ;;
esac
