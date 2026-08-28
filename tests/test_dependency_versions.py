"""Dependency version floor tests (Priority 7 of the hardening plan).

A pip-audit pass (2026-08-28) found 20 known CVEs across aiohttp, starlette,
python-dotenv, and python-multipart at the versions previously pinned in
requirements.txt. All four were bumped to versions with the relevant fixes
available. fastapi was bumped alongside starlette since starlette's version
is constrained by whichever fastapi version is installed
(fastapi==0.115.0 requires starlette<0.39.0, which has no patched release in
range -- the CVEs are only reachable by moving both together).

These tests don't re-verify the CVEs themselves (that's pip-audit's job, and
it should be re-run periodically, not just once). What they guard against is
a *regression*: someone bumping a requirement back down without noticing --
e.g. resolving a dependency conflict by pinning an older version, or a merge
that reintroduces a stale requirements.txt. A version floor test fails loudly
in CI if that happens, rather than silently reintroducing a fixed CVE.

Starlette's remaining unpatched findings at the time of this pass (Host
header URL-reconstruction issues, a Windows-only StaticFiles SSRF, an
HTTPEndpoint dispatch quirk, a FileResponse Range-header DoS) all require
starlette>=1.0.1, which needs a much newer fastapi than this app currently
runs -- and this app doesn't use StaticFiles, FileResponse, or Starlette's
class-based HTTPEndpoint routing, and doesn't make security decisions based
on request.url (Twilio signature verification explicitly uses the trusted
configured BASE_URL, not request.url -- see app/security.py), so the
practical exposure from staying below 1.0.1 was judged low relative to the
risk of a larger, less-tested version jump. That's a judgment call to
revisit later, not a settled fact -- worth another look at a natural
upgrade point.
"""

import importlib.metadata

import pytest


MINIMUM_VERSIONS = {
    "aiohttp": (3, 14, 3),
    "fastapi": (0, 118, 0),
    "python-dotenv": (1, 2, 2),
    "python-multipart": (0, 0, 20),  # anything >=0.0.20 covers the found CVEs
    "starlette": (0, 48, 0),
}


def _version_tuple(version_str: str) -> tuple:
    """Parse a dotted version string into a comparable tuple of ints,
    ignoring any pre-release/build suffix (e.g. "1.2.3rc1" -> (1, 2, 3))."""
    parts = []
    for chunk in version_str.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@pytest.mark.parametrize("package_name,minimum", sorted(MINIMUM_VERSIONS.items()))
def test_package_meets_security_floor(package_name, minimum):
    installed = importlib.metadata.version(package_name)
    assert _version_tuple(installed) >= minimum, (
        f"{package_name} {installed} is below the {'.'.join(map(str, minimum))} security "
        f"floor set after the 2026-08-28 pip-audit pass -- check whether the CVEs that "
        f"prompted the original bump apply again before downgrading."
    )
