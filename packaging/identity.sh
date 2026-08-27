#!/usr/bin/env bash
# Print the code-signing identity the bundle should be signed with.
#
# Ad-hoc signing -- "-", the fallback here -- pins the designated requirement to
# a hash of the code itself, so every rebuild is a different application as far
# as TCC is concerned: the Camera, Accessibility and Menu Bar grants are all
# given again from scratch. Until they are, the app runs, tracks your hands
# perfectly, and moves nothing, while System Settings shows the switch already
# on -- for the copy you built last time. Signing with a certificate pins the
# requirement to the certificate instead, and the grants survive every build.
#
# Any code-signing certificate does that, so one on the keychain is preferred to
# ad-hoc. SIGN_IDENTITY overrides, including with "-" to force ad-hoc.
#
#   packaging/identity.sh
set -euo pipefail

if [ -n "${SIGN_IDENTITY:-}" ]; then
    printf '%s\n' "$SIGN_IDENTITY"
    exit 0
fi

# Developer ID first: it keeps the grants like any other certificate, and is the
# only one that also means something on a machine that is not this one.
pick() {
    security find-identity -v -p codesigning 2>/dev/null \
        | sed -n "s/.*\"\($1:.*\)\"\$/\1/p" | head -1
}

identity="$(pick 'Developer ID Application')"
[ -n "$identity" ] || identity="$(pick 'Apple Development')"
printf '%s\n' "${identity:--}"
