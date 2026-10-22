#!/bin/bash
# Install Pebble on a Mac with Apple silicon.
#
#   curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash
#
# Options (after "bash -s --" when piped, for example "| bash -s -- --dry-run"):
#   --version X.Y.Z   install that release instead of the latest one
#   --dry-run         check this Mac and print what would happen; nothing is downloaded or changed
#   --uninstall       move Pebble.app to ~/PebbleBackups and list what Pebble left on this Mac
#   --help            show this help
#
# What it does, and what it never does:
#   - It runs only on macOS on Apple silicon (not under Rosetta), at the macOS version the app needs.
#   - It downloads Pebble-macos-arm64.zip and SHA256SUMS from the GitHub release over HTTPS only, into a new private
#     temporary folder, and checks the zip's SHA-256 against its line in SHA256SUMS. That shows the file arrived
#     intact; because both files come from the same release, it does not prove who made them.
#   - It unpacks the zip there and checks the app's identifier, version and code signature before anything else.
#   - It refuses while Pebble is running, and never stops a process.
#   - An existing Pebble.app is moved to ~/PebbleBackups/<time>-pre-install.noindex/Pebble.app.backup, never deleted.
#   - It installs into /Applications when your account can write there, otherwise into ~/Applications. It never
#     uses sudo, never changes Gatekeeper and never removes quarantine flags: curl does not quarantine what it
#     downloads.
#   - Pebble is signed with the project's own stable signing identity, not an Apple Developer ID, and it is not
#     notarized by Apple.
#   - It deletes nothing but its own temporary folder, and never touches your data (~/.pebble, ~/Documents/Pebble).
#
# Exit codes: 0 done, 1 refused or failed (the message says what changed, if anything), 2 usage.

set -euo pipefail

REPO="tohin003/Pebble"
ASSET="Pebble-macos-arm64.zip"
SUMS="SHA256SUMS"
SUMS_LINE='^[0-9a-f]{64}  Pebble-macos-arm64\.zip$'
APP="Pebble.app"
IDENTIFIER="com.pebble.local"
# The release bundle's LSMinimumSystemVersion (src-tauri/tauri.conf.json). The downloaded bundle's own value is
# checked again after unpacking.
MIN_MACOS="12.0"

mode="install"
dry_run=0
version=""
macos=""
bundle_version=""
work=""
tmp_root=""

say() {
  printf '%s\n' "$*"
}

fail() {
  printf 'Pebble installer: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Install Pebble on a Mac with Apple silicon.

  curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/tohin003/Pebble/main/install.sh | bash -s -- [option]

  --version X.Y.Z   install that release instead of the latest one
  --dry-run         check this Mac and print what would happen; nothing is downloaded or changed
  --uninstall       move Pebble.app to ~/PebbleBackups and list what Pebble left on this Mac
  --help            show this help
EOF
}

cleanup() {
  # Only the folder mktemp made for this run.
  if [ -n "$work" ] && [ -n "$tmp_root" ] && [ -d "$work" ]; then
    case "$work" in
      "$tmp_root"/pebble-install.*) rm -rf -- "$work" ;;
    esac
  fi
}

# version_at_least HAVE WANT: true when the dotted version HAVE is WANT or later.
version_at_least() {
  local IFS=.
  local -a have want
  read -r -a have <<< "$1"
  read -r -a want <<< "$2"
  local i h w
  for i in 0 1 2; do
    h="${have[$i]:-0}"
    w="${want[$i]:-0}"
    [[ "$h" =~ ^[0-9]+$ ]] || h=0
    [[ "$w" =~ ^[0-9]+$ ]] || w=0
    if [ "$h" -gt "$w" ]; then
      return 0
    fi
    if [ "$h" -lt "$w" ]; then
      return 1
    fi
  done
  return 0
}

check_platform() {
  local system machine translated
  system="$(uname -s)"
  [ "$system" = "Darwin" ] || fail "Pebble runs on macOS only (this system is $system)."
  translated="$(sysctl -n sysctl.proc_translated 2>/dev/null || true)"
  if [ "$translated" = "1" ]; then
    fail "This Terminal runs under Rosetta. Open Terminal without Rosetta (in Finder, Get Info on Terminal and untick \"Open using Rosetta\") and run the installer again."
  fi
  machine="$(uname -m)"
  [ "$machine" = "arm64" ] || fail "Pebble needs a Mac with Apple silicon (this Mac reports $machine)."
  macos="$(sw_vers -productVersion)"
  version_at_least "$macos" "$MIN_MACOS" || fail "Pebble needs macOS $MIN_MACOS or later (this Mac has $macos)."
}

# Every running process whose executable lies inside a Pebble.app, by executable path (never by command-line text).
running_pebble() {
  local processes line
  processes="$(ps -axo comm=)" || fail "Could not list the running apps; nothing was changed."
  while IFS= read -r line; do
    case "$line" in
      */"$APP"/Contents/*) printf '%s\n' "$line" ;;
    esac
  done <<< "$processes"
}

refuse_if_running() {
  local running
  running="$(running_pebble)"
  if [ -n "$running" ]; then
    say "Pebble is running:"
    printf '  %s\n' "$running"
    fail "Quit Pebble first (click the pebble, then Quit Pebble, or use its menu-bar icon), then run the installer again. Nothing was changed."
  fi
}

install_folder() {
  if [ -w /Applications ]; then
    printf '%s\n' /Applications
  else
    printf '%s\n' "$HOME/Applications"
  fi
}

new_backup_root() {
  local kind="$1" stamp root
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  root="$HOME/PebbleBackups/${stamp}-${kind}.noindex"
  mkdir -p "$HOME/PebbleBackups"
  mkdir -m 700 "$root" || fail "Could not create $root; nothing was changed."
  printf '%s\n' "$root"
}

fetch() {
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error --location \
    --retry 2 --connect-timeout 20 --output "$2" "$1"
}

plist_value() {
  plutil -extract "$2" raw -o - "$1" 2>/dev/null || true
}

# check_bundle APP: the identifier, the version (the requested one, when given), the minimum macOS and the signature.
check_bundle() {
  local app="$1" plist found minimum
  plist="$app/Contents/Info.plist"
  [ -f "$plist" ] || fail "The download holds no app information (Info.plist); nothing was installed."
  found="$(plist_value "$plist" CFBundleIdentifier)"
  [ "$found" = "$IDENTIFIER" ] || fail "The downloaded app is not Pebble (identifier \"$found\"); nothing was installed."
  bundle_version="$(plist_value "$plist" CFBundleShortVersionString)"
  [[ "$bundle_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "The downloaded app has no readable version; nothing was installed."
  if [ -n "$version" ] && [ "$bundle_version" != "$version" ]; then
    fail "The release v$version holds Pebble $bundle_version; nothing was installed."
  fi
  minimum="$(plist_value "$plist" LSMinimumSystemVersion)"
  if [ -n "$minimum" ] && ! version_at_least "$macos" "$minimum"; then
    fail "Pebble $bundle_version needs macOS $minimum or later (this Mac has $macos); nothing was installed."
  fi
  codesign --verify --deep --strict "$app" || fail "The app's code signature does not verify; nothing was installed."
}

download_and_verify() {
  local base="$1" expected actual count entry
  local -a top
  tmp_root="${TMPDIR:-/tmp}"
  tmp_root="${tmp_root%/}"
  work="$(mktemp -d "$tmp_root/pebble-install.XXXXXXXX")" || fail "Could not create a temporary folder; nothing was changed."
  chmod 700 "$work"
  say "Downloading Pebble from $base/ ..."
  fetch "$base/$ASSET" "$work/$ASSET" || fail "The download failed; nothing was changed."
  fetch "$base/$SUMS" "$work/$SUMS" || fail "SHA256SUMS could not be downloaded; nothing was changed."
  count="$(grep -cE "$SUMS_LINE" "$work/$SUMS" || true)"
  [ "$count" = "1" ] || fail "SHA256SUMS has no single line for $ASSET; nothing was installed."
  expected="$(grep -E "$SUMS_LINE" "$work/$SUMS" | cut -c1-64)"
  actual="$(shasum -a 256 "$work/$ASSET" | cut -c1-64)"
  if [ "$actual" != "$expected" ]; then
    fail "The download does not match SHA256SUMS (expected $expected, got $actual); nothing was installed."
  fi
  say "SHA-256 matches SHA256SUMS: $actual"
  mkdir "$work/unpacked"
  ditto -x -k "$work/$ASSET" "$work/unpacked" || fail "The download could not be unpacked; nothing was installed."
  [ -d "$work/unpacked/$APP" ] || fail "The download holds no Pebble.app; nothing was installed."
  shopt -s nullglob dotglob
  top=("$work/unpacked"/*)
  shopt -u nullglob dotglob
  for entry in "${top[@]}"; do
    case "${entry##*/}" in
      "$APP" | __MACOSX) ;;
      *) fail "The download holds something besides Pebble.app (${entry##*/}); nothing was installed." ;;
    esac
  done
  check_bundle "$work/unpacked/$APP"
}

print_first_run() {
  local target="$1"
  say ""
  say "First run:"
  say "  1. Open Pebble from $target (or with Spotlight)."
  say "  2. Connect a model: choose Continue with ChatGPT and finish the sign-in in your browser, or add another"
  say "     provider or a local model under Settings, Models."
  say "  3. If macOS asks whether Pebble may use its item in your Keychain, enter your login password and choose"
  say "     Always Allow. Your sign-ins and keys are kept there."
  say ""
  say "About this download:"
  say "  - Pebble is signed with the project's own stable signing identity, not an Apple Developer ID, and it is"
  say "    not notarized by Apple."
  say "  - The SHA-256 check shows the download arrived intact. SHA256SUMS comes from the same release, so it"
  say "    does not prove who made the file."
  say "  - curl does not quarantine what it downloads, so macOS opens Pebble directly. If you download the zip in"
  say "    a browser instead, macOS may refuse the first launch: Control-click Pebble.app, choose Open, then Open"
  say "    again (or System Settings, Privacy & Security, Open Anyway). That allows Pebble only; never turn"
  say "    Gatekeeper off."
}

do_install() {
  local base target folder existing="" backup_root="" other=""
  if [ -n "$version" ]; then
    base="https://github.com/$REPO/releases/download/v$version"
  else
    base="https://github.com/$REPO/releases/latest/download"
  fi
  target="$(install_folder)"
  if [ -e "$target/$APP" ] || [ -L "$target/$APP" ]; then
    existing="$target/$APP"
  fi
  for folder in /Applications "$HOME/Applications"; do
    if [ "$folder" != "$target" ] && [ -d "$folder/$APP" ]; then
      other="$folder/$APP"
    fi
  done

  if [ "$dry_run" = "1" ]; then
    say "Dry run: nothing is downloaded or changed."
    say "  This Mac: macOS $macos on Apple silicon."
    say "  Would download: $base/$ASSET"
    say "  and check it against: $base/$SUMS"
    say "  Would install: $target/$APP"
    if [ -n "$existing" ]; then
      say "  Would move the existing $existing to ~/PebbleBackups/<time>-pre-install.noindex/Pebble.app.backup"
    fi
    if [ -n "$other" ]; then
      say "  Another copy stays where it is: $other"
    fi
    if [ -n "$(running_pebble)" ]; then
      say "  Pebble is running now: the installer would stop and ask you to quit it first."
    fi
    return 0
  fi

  refuse_if_running
  download_and_verify "$base"
  refuse_if_running

  if [ "$target" = "$HOME/Applications" ]; then
    mkdir -p "$target"
  fi
  if [ -n "$existing" ]; then
    backup_root="$(new_backup_root pre-install)"
    mv "$existing" "$backup_root/Pebble.app.backup" || fail "Could not move the existing $existing to $backup_root; nothing was installed."
  fi
  if ! mv "$work/unpacked/$APP" "$target/$APP"; then
    if [ -n "$backup_root" ] && [ ! -e "$target/$APP" ]; then
      mv "$backup_root/Pebble.app.backup" "$target/$APP" || true
      fail "Could not install into $target; the previous Pebble.app was put back."
    fi
    fail "Could not install into $target. The previous Pebble.app, if there was one, is in ${backup_root:-the same place}."
  fi
  if ! codesign --verify --deep --strict "$target/$APP"; then
    if [ -z "$backup_root" ]; then
      backup_root="$(new_backup_root pre-install)"
    fi
    mv "$target/$APP" "$backup_root/Pebble.app.failed" || true
    if [ -d "$backup_root/Pebble.app.backup" ] && [ ! -e "$target/$APP" ]; then
      mv "$backup_root/Pebble.app.backup" "$target/$APP" || true
    fi
    fail "The installed copy did not verify; it was moved to $backup_root/Pebble.app.failed and the previous app, if any, was put back."
  fi

  say ""
  say "Pebble $bundle_version is installed: $target/$APP"
  if [ -n "$backup_root" ]; then
    say "The previous Pebble.app is kept in $backup_root/Pebble.app.backup. Nothing was deleted; move that"
    say "folder to the Trash when you no longer need it."
  fi
  if [ -n "$other" ]; then
    say "Another copy of Pebble is still in $other. Move it to the Trash so that Spotlight opens the new one."
  fi
  print_first_run "$target"
}

list_left() {
  local path
  local -a items=(
    "$HOME/.pebble"
    "$HOME/Documents/Pebble"
    "$HOME/Library/Application Support/com.pebble.local"
    "$HOME/Library/WebKit/com.pebble.local"
    "$HOME/Library/Caches/com.pebble.local"
    "$HOME/Library/HTTPStorages/com.pebble.local"
    "$HOME/Library/HTTPStorages/com.pebble.local.binarycookies"
    "$HOME/Library/Saved Application State/com.pebble.local.savedState"
    "$HOME/Library/Preferences/com.pebble.local.plist"
    "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json"
    "$HOME/Library/Application Support/Google/Chrome Beta/NativeMessagingHosts/com.pebble.browser.json"
    "$HOME/Library/Application Support/Google/Chrome Canary/NativeMessagingHosts/com.pebble.browser.json"
    "$HOME/Library/Application Support/Chromium/NativeMessagingHosts/com.pebble.browser.json"
    "$HOME/Library/Keychains/pebble-signing.keychain-db"
    "$HOME/PebbleBackups"
  )
  say ""
  say "Pebble's data is still on this Mac; nothing below was changed. ~/.pebble holds your conversations, memories"
  say "and settings, and ~/Documents/Pebble holds readable copies that are yours. To remove Pebble completely, move"
  say "these to the Trash (each backup in ~/PebbleBackups also holds a copy of your data):"
  for path in "${items[@]}"; do
    if [ -e "$path" ]; then
      say "  $path"
    fi
  done
  say "  and any other copies folder you chose in Settings, Data & storage."
  say ""
  say "Keychain items (the installer never reads them): in Keychain Access, search for com.pebble.local and delete"
  say "  com.pebble.local.vault, com.pebble.local.providers, com.pebble.local.connectors,"
  say "  com.pebble.local.plan-credentials and com.pebble.local.browser-pairing."
  say "Also remove Pebble in System Settings (General, Login Items; Notifications; Privacy & Security:"
  say "Accessibility, Screen Recording, Files and Folders), and Pebble for Chrome at chrome://extensions."
  say "The full list, in order: docs/DATA_FOLDERS.md, \"Remove Pebble completely\"."
}

do_uninstall() {
  local folder root="" name="Pebble.app.backup" movable=0
  for folder in /Applications "$HOME/Applications"; do
    if [ -e "$folder/$APP" ] && [ -w "$folder" ]; then
      movable=$((movable + 1))
    fi
  done
  if [ "$dry_run" = "1" ]; then
    say "Dry run: nothing is moved or changed."
  else
    refuse_if_running
  fi
  if [ "$movable" -gt 0 ] && [ "$dry_run" != "1" ]; then
    root="$(new_backup_root uninstall)"
  fi
  for folder in /Applications "$HOME/Applications"; do
    if [ ! -e "$folder/$APP" ]; then
      continue
    fi
    if [ ! -w "$folder" ]; then
      say "$folder/$APP stays: this account cannot change $folder (an administrator can move it to the Trash)."
    elif [ "$dry_run" = "1" ]; then
      say "  Would move $folder/$APP to ~/PebbleBackups/<time>-uninstall.noindex/"
    else
      mv "$folder/$APP" "$root/$name" || fail "Could not move $folder/$APP to $root."
      say "Moved $folder/$APP to $root/$name"
      name="Pebble.app.backup.2"
    fi
  done
  if [ "$movable" -eq 0 ]; then
    say "No Pebble.app that this account can move in /Applications or ~/Applications."
  elif [ "$dry_run" != "1" ]; then
    say "Nothing was deleted: move $root to the Trash when you no longer need it."
  fi
  list_left
}

main() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --version)
        [ "$#" -ge 2 ] || { usage >&2; exit 2; }
        version="$2"
        shift 2
        ;;
      --version=*)
        version="${1#--version=}"
        shift
        ;;
      --dry-run)
        dry_run=1
        shift
        ;;
      --uninstall)
        mode="uninstall"
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        printf 'Pebble installer: unknown option %s\n\n' "$1" >&2
        usage >&2
        exit 2
        ;;
    esac
  done
  if [ -n "$version" ] && ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'Pebble installer: --version needs a release number such as 1.0.0 (got %s).\n' "$version" >&2
    exit 2
  fi
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    fail "HOME is not set to your home folder."
  fi
  if [ "$(id -u)" = "0" ]; then
    fail "Run the installer as yourself, without sudo. Pebble installs for your account and never needs administrator rights."
  fi
  check_platform
  trap cleanup EXIT
  if [ "$mode" = "uninstall" ]; then
    do_uninstall
  else
    do_install
  fi
  cleanup
  trap - EXIT
}

# Everything above only defines; a download cut short runs nothing.
main "$@"
