#!/usr/bin/env bash
# Lightweight hygiene check for the repository. Not a replacement for a real secret scanner.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0

echo "== tracked forbidden files =="
if git ls-files | grep -E '(^|/)(\.env$|Данные/|Основная информация\.txt$|.*\.(pem|key|sqlite|sqlite3|gguf|pdf|docx|xlsx)$)'; then
  echo "FAIL: tracked secret/internal/binary file" >&2
  fail=1
else
  echo "OK: no tracked .env, keys, databases, weights or office documents"
fi

echo "== private keys and common tokens =="
if git grep -nIE 'BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' -- . ':!docs/security.md'; then
  echo "FAIL: possible secret" >&2
  fail=1
else
  echo "OK: no private keys or common token formats"
fi

echo "== assignments that look secret =="
if git grep -nIE '(password|passwd|api[_-]?key|secret|access[_-]?token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}' -- .; then
  echo "FAIL: possible hardcoded secret" >&2
  fail=1
else
  echo "OK: no hardcoded secret assignments"
fi

echo "== internal paths =="
if git grep -nIE '/(home|Users)/[A-Za-z0-9._-]+/|C:\\\\Users\\\\[A-Za-z0-9._-]+' -- . ':!docs/security.md'; then
  echo "FAIL: absolute personal path" >&2
  fail=1
else
  echo "OK: no absolute personal paths"
fi

if [ "$fail" -ne 0 ]; then
  echo "Hygiene check failed" >&2
  exit 1
fi
echo "Hygiene check passed"
