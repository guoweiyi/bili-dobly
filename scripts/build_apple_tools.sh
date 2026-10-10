#!/bin/bash
# Build native tools locally; do not launch players or modify system settings.
# Compatible with macOS's /bin/bash 3.2. Outputs are intentionally untracked.
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
    printf '%s\n' \
        'Usage: ./scripts/build_apple_tools.sh [--help]' \
        'Requires macOS 13+, Xcode or Command Line Tools with a macOS 13+ SDK.' \
        'Builds for this Mac architecture into the repository build/ directory.' \
        'Creates and ad-hoc signs DolbyVisionCompare.app without launching it.'
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && "$1" == '--help' ]]; then usage; exit 0; fi
    usage >&2
    exit 64
fi

[[ "$(uname -s)" == 'Darwin' ]] || die 'The Apple tools require macOS.'
for dependency in xcrun plutil codesign mktemp; do
    command -v "$dependency" >/dev/null 2>&1 || die "Missing dependency: $dependency"
done
swift_compiler="$(xcrun --find swiftc)" || die 'Select Xcode or install Apple Command Line Tools first.'
mac_sdk="$(xcrun --sdk macosx --show-sdk-path)" || die 'A macOS SDK is required.'
machine_arch="$(uname -m)"
case "$machine_arch" in
    arm64|x86_64) ;;
    *) die "Unsupported Mac architecture: $machine_arch" ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_dir="$(cd -- "$script_dir/.." && pwd -P)"
build_dir="$project_dir/build"
[[ ! -L "$build_dir" ]] || die 'build/ must be a directory, not a symbolic link.'
for source_file in apple_media_inspect.swift apple_display_inspect.swift DolbyVisionCompare.swift; do
    [[ -r "$project_dir/$source_file" ]] || die "Missing source: $source_file"
done
mkdir -p -- "$build_dir"
for product_name in apple_media_inspect apple_display_inspect; do
    [[ ! -d "$build_dir/$product_name" && ! -L "$build_dir/$product_name" ]] ||
        die "build/$product_name must be a regular file or absent."
done
staging_dir="$(mktemp -d "$build_dir/.apple-tools.XXXXXX")"

cleanup() {
    if [[ -n "${staging_dir:-}" && -d "$staging_dir" ]]; then rm -rf -- "$staging_dir"; fi
}
trap cleanup EXIT
trap 'printf "Apple tool build failed at line %s (exit %s).\n" "$LINENO" "$?" >&2' ERR

# Each build gets its own cache, so parallel builds cannot corrupt one another.
compiler_flags=(-sdk "$mac_sdk" -target "$machine_arch-apple-macosx13.0"
                -module-cache-path "$staging_dir/module-cache" -O)
printf 'Compiling Apple tools for %s (minimum macOS 13)…\n' "$machine_arch"
"$swift_compiler" "${compiler_flags[@]}" -parse-as-library \
    "$project_dir/apple_media_inspect.swift" -o "$staging_dir/apple_media_inspect"
"$swift_compiler" "${compiler_flags[@]}" \
    "$project_dir/apple_display_inspect.swift" -o "$staging_dir/apple_display_inspect"

app_dir="$staging_dir/DolbyVisionCompare.app"
mkdir -p -- "$app_dir/Contents/MacOS"
"$swift_compiler" "${compiler_flags[@]}" -parse-as-library \
    "$project_dir/DolbyVisionCompare.swift" -o "$app_dir/Contents/MacOS/DolbyVisionCompare"
cat > "$app_dir/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>DolbyVisionCompare</string>
  <key>CFBundleIdentifier</key><string>org.bilidobly.DolbyVisionCompare</string>
  <key>CFBundleName</key><string>DolbyVisionCompare</string>
  <key>CFBundleDisplayName</key><string>DV 元数据对照</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>Movie</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSHandlerRank</key><string>Alternate</string>
    <key>LSItemContentTypes</key><array><string>public.movie</string></array>
  </dict></array>
</dict></plist>
PLIST
plutil -lint "$app_dir/Contents/Info.plist"
codesign --force --sign - --timestamp=none "$app_dir"
codesign --verify --strict "$app_dir"

# Replace only our three named build products after all compilation succeeds.
mv -f -- "$staging_dir/apple_media_inspect" "$build_dir/apple_media_inspect"
mv -f -- "$staging_dir/apple_display_inspect" "$build_dir/apple_display_inspect"
if [[ -e "$build_dir/DolbyVisionCompare.app" || -L "$build_dir/DolbyVisionCompare.app" ]]; then
    rm -rf -- "$build_dir/DolbyVisionCompare.app"
fi
mv -- "$app_dir" "$build_dir/DolbyVisionCompare.app"
printf 'Built (no UI launched):\n  %s\n  %s\n  %s\n' \
    "$build_dir/apple_media_inspect" "$build_dir/apple_display_inspect" \
    "$build_dir/DolbyVisionCompare.app"
