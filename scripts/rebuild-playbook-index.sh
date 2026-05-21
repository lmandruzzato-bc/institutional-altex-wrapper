#!/usr/bin/env bash
# Rebuild playbook/index.toon from all playbook/*.md frontmatter.
#
# Exit codes:
#   0 - index rebuilt (or unchanged)
#   1 - environment error (missing tool, no playbook dir, no write permission)
#   2 - one or more entries have malformed frontmatter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLAYBOOK_DIR="${REPO_ROOT}/playbook"
INDEX_FILE="${PLAYBOOK_DIR}/index.toon"
HEADER='[playbook_entries] -> id, title, phase, transfer_method, exchange, fix_category, error_patterns_joined, last_seen'

if [[ ! -d "${PLAYBOOK_DIR}" ]]; then
  echo "rebuild-playbook-index: ${PLAYBOOK_DIR} not found" >&2
  exit 1
fi

ROWS="$(mktemp -t playbook-rows.XXXXXX)" || { echo "rebuild-playbook-index: mktemp failed" >&2; exit 1; }
ERRS="$(mktemp -t playbook-errs.XXXXXX)" || { echo "rebuild-playbook-index: mktemp failed" >&2; exit 1; }
trap 'rm -f "${ROWS}" "${ERRS}"' EXIT

# Awk program: parse one entry's frontmatter and emit a single TOON row to stdout.
# Required fields: id, title, signature.{phase, transfer_method, exchange, fix_category}, last_seen.
# Emits "MALFORMED: <file> — <reason>" to stderr and exits 2 on any problem.
read -r -d '' PARSER <<'AWK' || true
function strip(s) {
  sub(/^[ \t]+/, "", s)
  sub(/[ \t]+$/, "", s)
  return s
}
function unquote(s,   n, f, l) {
  s = strip(s)
  n = length(s)
  if (n >= 2) {
    f = substr(s, 1, 1)
    l = substr(s, n, 1)
    if ((f == "\"" && l == "\"") || (f == "\047" && l == "\047")) {
      return substr(s, 2, n - 2)
    }
  }
  return s
}
function leading_spaces(s,   i) {
  for (i = 1; i <= length(s); i++) {
    if (substr(s, i, 1) != " ") return i - 1
  }
  return length(s)
}
function parse_kv(s,   p) {
  p = index(s, ":")
  if (p == 0) { g_key = ""; g_val = ""; return 0 }
  g_key = substr(s, 1, p - 1)
  g_val = strip(substr(s, p + 1))
  return 1
}
function qcsv(s) {
  if (s ~ /[,"]/) {
    gsub(/"/, "\"\"", s)
    return "\"" s "\""
  }
  return s
}

BEGIN {
  state = 0   # 0=pre-frontmatter, 1=inside frontmatter, 2=post
  in_signature = 0
  in_patterns = 0
  id = ""; title = ""; last_seen = ""
  phase = ""; tmethod = ""; exchange = ""; fixcat = ""
  patterns = ""
}

{
  rstrip = $0
  sub(/[ \t]+$/, "", rstrip)

  if (rstrip == "---") {
    if (state == 0) { state = 1; next }
    if (state == 1) { state = 2; exit }
  }

  if (state != 1) next

  ind = leading_spaces($0)
  trimmed = strip($0)
  if (trimmed == "" || substr(trimmed, 1, 1) == "#") next

  if (ind == 0) {
    in_signature = 0
    in_patterns = 0
    if (parse_kv(trimmed)) {
      if (g_key == "id") id = unquote(g_val)
      else if (g_key == "title") title = unquote(g_val)
      else if (g_key == "last_seen") last_seen = unquote(g_val)
      else if (g_key == "signature" && g_val == "") in_signature = 1
    }
  } else if (in_signature && ind == 2) {
    in_patterns = 0
    if (parse_kv(trimmed)) {
      if (g_key == "phase") phase = unquote(g_val)
      else if (g_key == "transfer_method") tmethod = unquote(g_val)
      else if (g_key == "exchange") exchange = unquote(g_val)
      else if (g_key == "fix_category") fixcat = unquote(g_val)
      else if (g_key == "error_patterns" && g_val == "") in_patterns = 1
    }
  } else if (in_patterns && ind == 4 && substr(trimmed, 1, 1) == "-") {
    pat = strip(substr(trimmed, 2))
    pat = unquote(pat)
    if (patterns == "") patterns = pat
    else patterns = patterns "|" pat
  }
}

END {
  if (state != 2) {
    print "MALFORMED: " FILENAME " — frontmatter missing or unterminated" > "/dev/stderr"
    exit 2
  }
  miss = ""
  if (id == "")       miss = miss " id"
  if (title == "")    miss = miss " title"
  if (phase == "")    miss = miss " signature.phase"
  if (tmethod == "")  miss = miss " signature.transfer_method"
  if (exchange == "") miss = miss " signature.exchange"
  if (fixcat == "")   miss = miss " signature.fix_category"
  if (last_seen == "") miss = miss " last_seen"
  if (miss != "") {
    print "MALFORMED: " FILENAME " — missing fields:" miss > "/dev/stderr"
    exit 2
  }

  # error_patterns_joined is always quoted (patterns commonly contain `|`, commas, or `-`).
  qp = patterns
  gsub(/"/, "\"\"", qp)
  qp = "\"" qp "\""

  printf "%s, %s, %s, %s, %s, %s, %s, %s\n", \
    qcsv(id), qcsv(title), qcsv(phase), qcsv(tmethod), \
    qcsv(exchange), qcsv(fixcat), qp, qcsv(last_seen)
}
AWK

shopt -s nullglob
malformed=0
entries_found=0
for f in "${PLAYBOOK_DIR}"/*.md; do
  base="$(basename "${f}")"
  [[ "${base}" == "README.md" ]] && continue
  entries_found=$((entries_found + 1))
  if ! awk "${PARSER}" "${f}" >>"${ROWS}" 2>>"${ERRS}"; then
    malformed=1
  fi
done

if [[ ${malformed} -ne 0 ]]; then
  cat "${ERRS}" >&2
  exit 2
fi

# Build the new index in a temp file, then atomically rename.
NEW_INDEX="$(mktemp -t playbook-index.XXXXXX)" || { echo "rebuild-playbook-index: mktemp failed" >&2; exit 1; }
{
  echo "${HEADER}"
  LC_ALL=C sort "${ROWS}"
} > "${NEW_INDEX}"

chmod 644 "${NEW_INDEX}"
mv "${NEW_INDEX}" "${INDEX_FILE}"
