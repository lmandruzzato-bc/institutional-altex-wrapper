#!/usr/bin/env bash
# Overwrite `.permissions.additionalDirectories` in .claude/settings.local.json
# from CLAUDE_ADDITIONAL_DIR_N env vars (N = 1, 2, 3, ...).
# Stops at the first unset/empty var. Requires jq.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS_FILE="${SCRIPT_DIR}/../.claude/settings.local.json"

if ! command -v jq >/dev/null 2>&1; then
    echo "update-additional-dirs.sh: jq not found in PATH" >&2
    exit 1
fi

dirs=()
i=1
while true; do
    var="CLAUDE_ADDITIONAL_DIR_${i}"
    val="${!var:-}"
    [[ -z "$val" ]] && break
    dirs+=("$val")
    i=$((i + 1))
done

if [[ ${#dirs[@]} -eq 0 ]]; then
    echo "update-additional-dirs.sh: no CLAUDE_ADDITIONAL_DIR_N vars set; nothing to do" >&2
    exit 0
fi

if [[ ! -f "$SETTINGS_FILE" ]]; then
    echo '{}' > "$SETTINGS_FILE"
fi

json_arr=$(printf '%s\n' "${dirs[@]}" | jq -R . | jq -s .)

tmp=$(mktemp)
jq --argjson arr "$json_arr" '.permissions.additionalDirectories = $arr' "$SETTINGS_FILE" > "$tmp"
mv "$tmp" "$SETTINGS_FILE"
