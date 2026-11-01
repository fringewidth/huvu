#!/bin/sh
# Builds huvu's helper for the TV box into backend/src/huvu/tv-helper.jar,
# which the backend pushes to the box and runs with app_process. The jar is
# checked in, so the image needs no Android SDK; run this after changing the
# helper. It needs a JDK (Android Studio's) and the Android SDK.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
sdk=${ANDROID_HOME:-$HOME/Library/Android/sdk}
java_home=${JAVA_HOME:-"/Applications/Android Studio.app/Contents/jbr/Contents/Home"}
platform=$(ls -d "$sdk"/platforms/android-* | sort -V | tail -1)
tools=$(ls -d "$sdk"/build-tools/* | sort -V | tail -1)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# The Stick runs Android 5.1 (API 22): only APIs it has may be used, which
# javac can't check against a newer android.jar, so the code reaches newer or
# hidden ones by reflection only.
"$java_home/bin/javac" --release 8 -nowarn -cp "$platform/android.jar" -d "$work/classes" $(find "$here/src" -name '*.java')
JAVA_HOME="$java_home" PATH="$java_home/bin:$PATH" "$tools/d8" --min-api 22 --release --lib "$platform/android.jar" --output "$work" $(find "$work/classes" -name '*.class')
(cd "$work" && "$java_home/bin/jar" cf "$here/../src/huvu/tv-helper.jar" classes.dex)
echo "built $(cd "$here/../src/huvu" && pwd)/tv-helper.jar"
