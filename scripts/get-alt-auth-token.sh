#!/usr/bin/env bash
#
# Fetch an ALT-AUTH-TOKEN via the full login + OTP flow. The username and
# password come from the environment (set in .envrc); only the TOTP code is
# pulled from 1Password.
#
# Usage:
#   ./scripts/get-alt-auth-token.sh                      # prints the JWT to stdout
#   eval "$(./scripts/get-alt-auth-token.sh --export)"   # exports ALT_AUTH_TOKEN
#
# Then call Altex services with:  -H "alt-auth-token: $ALT_AUTH_TOKEN"
#
# Env:
#   HOST          auth host                                       (default: altex.altono.app)
#   ALT_USERNAME  login email/user                                (required)
#   ALT_PASSWORD  login password                                  (required)
#   OP_OTP        1Password secret ref for the one-time password  (default: ALT_1PASSWORD_OTP_PATH)

set -euo pipefail

HOST="${HOST:-altex.altono.app}"
USERNAME="${ALT_USERNAME}"
PASSWORD="${ALT_PASSWORD}"
OP_OTP="${ALT_1PASSWORD_OTP_PATH}"

BASE="https://${HOST}/auth_api/auth"

for bin in curl jq op; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: '$bin' not found in PATH" >&2; exit 1; }
done

err() { echo "error: $*" >&2; exit 1; }

[ -n "$USERNAME" ] || err "ALT_USERNAME not set"
[ -n "$PASSWORD" ] || err "ALT_PASSWORD not set"

# --- Step 1: login ---------------------------------------------------------
login_resp="$(curl -fsS -X POST "${BASE}/login" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg u "$USERNAME" --arg p "$PASSWORD" '{username:$u, password:$p}')")" \
  || err "login request failed"

token="$(jq -r '.jwt_token' <<<"$login_resp")"
use_otp="$(jq -r '.use_otp'  <<<"$login_resp")"
use_kyc="$(jq -r '.use_kyc'  <<<"$login_resp")"

[ "$token" != "null" ] && [ -n "$token" ] || err "no jwt_token in login response: $login_resp"

# --- Step 2: exchange OTP-stage token for the real token -------------------
if [ "$use_otp" = "true" ]; then
  # `op read` on a TOTP field returns the otpauth:// seed URI, not the code.
  # `op item get --otp` generates the live 6-digit code. Derive vault+item
  # from the OP_OTP secret reference (op://<vault>/<item>/<field>).
  _ref="${OP_OTP#op://}"
  OP_VAULT="${_ref%%/*}"
  OP_ITEM="${_ref#*/}"; OP_ITEM="${OP_ITEM%%/*}"
  OTP="$(op item get "$OP_ITEM" --vault "$OP_VAULT" --otp)" \
    || err "could not read OTP from 1Password (vault=$OP_VAULT item=$OP_ITEM)"
  otp_resp="$(curl -fsS -X POST "${BASE}/login_otp" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg t "$token" --arg o "$OTP" '{token:$t, otp:$o}')")" \
    || err "login_otp request failed"
  token="$(jq -r '.jwt_token' <<<"$otp_resp")"
  [ "$token" != "null" ] && [ -n "$token" ] || err "no jwt_token in login_otp response: $otp_resp"
fi
# use_kyc=true needs no extra call: the token is already valid (prefix stripped server-side).
[ "$use_kyc" = "true" ] && echo "note: account flagged use_kyc" >&2

# --- Output ----------------------------------------------------------------
if [ "${1:-}" = "--export" ]; then
  echo "export ALT_AUTH_TOKEN=${token}"
else
  echo "$token"
fi
