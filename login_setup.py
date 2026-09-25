#!/usr/bin/env python3
"""Interactive Monarch Money login -> saves a session token to the system keyring.

Monarch moved its API to api.monarch.com and now requires a GraphQL login with TOTP
MFA. The `monarchmoney` 0.1.15 that the MCP server pins can no longer perform that
login (it targets the old domain via a REST flow that now 405s). This script therefore
uses `monarchmoney-enhanced` ONLY for login, against the new domain, and writes the
token to the same keyring entry the server reads. The server itself stays on 0.1.15,
which works fine for data calls once a valid token exists.

Run it without installing anything into the server's environment:

    uvx --with monarchmoney-enhanced --with keyring python login_setup.py

Then restart Claude Desktop / Claude Code so the MCP picks up the new token.
"""
import sys
import asyncio
import getpass

_HINT = (
    "This script requires monarchmoney-enhanced (the pinned monarchmoney 0.1.15 cannot\n"
    "log in to the current Monarch API). Run it with:\n\n"
    "    uvx --with monarchmoney-enhanced --with keyring python login_setup.py\n"
)

try:
    import aiohttp
    import keyring
    import monarchmoney.monarchmoney as _mm
    from monarchmoney import MonarchMoney, RequireMFAException
    from monarchmoney.exceptions import MFARequiredError
except Exception:  # pragma: no cover - environment guard
    sys.exit(_HINT)

# Monarch rebranded its API domain; the library still hardcodes the old (dead) one.
_mm.MonarchMoneyEndpoints.BASE_URL = "https://api.monarch.com"

# Must match monarch_mcp_server/secure_session.py
KEYRING_SERVICE = "com.mcp.monarch-mcp-server"
KEYRING_USERNAME = "monarch-token"

_MFA_EXC = (MFARequiredError, RequireMFAException)


async def _rest_totp(mm, email, password, code):
    """Submit the authenticator code in the REST `totp` field (not email_otp)."""
    data = {
        "username": email, "password": password, "trusted_device": True,
        "supports_mfa": True, "supports_email_otp": True, "supports_recaptcha": True,
        "totp": code,
    }
    headers = dict(mm._headers)
    headers["Content-Type"] = "application/json"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            _mm.MonarchMoneyEndpoints.getLoginEndpoint(), json=data, headers=headers
        ) as resp:
            body = await resp.json()
            if not resp.ok:
                raise RuntimeError(f"REST totp status {resp.status}: {body}")
            token = body.get("token")
            if not token:
                raise RuntimeError(f"REST totp: no token in response: {body}")
            mm._token = token
            mm._headers["Authorization"] = f"Token {token}"
            return token


async def _submit_mfa(mm, email, password, code):
    """enhanced's multi_factor_authenticate() mislabels a 6-digit authenticator code as
    email_otp (-> 403); submit it as a TOTP via GraphQL totpToken, then REST totp."""
    errors = []
    try:
        await mm._auth_service._mfa_graphql(email, password, code)
        if getattr(mm, "token", None):
            return "graphql"
        errors.append("graphql: no token")
    except Exception as e:
        errors.append(f"graphql: {type(e).__name__}: {e}")
    try:
        if await _rest_totp(mm, email, password, code):
            return "rest-totp"
    except Exception as e:
        errors.append(f"rest-totp: {type(e).__name__}: {e}")
    raise RuntimeError("MFA submission failed.\n  " + "\n  ".join(errors))


async def main():
    print("🏦 Monarch Money - login")
    print(f"   API: {_mm.MonarchMoneyEndpoints.getLoginEndpoint()}\n")
    email = input("Monarch email: ").strip()
    password = getpass.getpass("Password: ")

    mm = MonarchMoney()
    if not hasattr(mm, "_auth_service"):  # 0.1.15 has no service architecture
        sys.exit("\n" + _HINT)

    try:
        await mm.login(email, password, use_saved_session=False, save_session=False)
        print("Logged in (no MFA challenge).")
    except _MFA_EXC:
        code = input("Enter the CURRENT 6-digit code from your authenticator app: ").strip()
        via = await _submit_mfa(mm, email, password, code)
        print(f"MFA accepted via: {via}")
    except Exception as e:
        print(f"\n❌ Login failed: {type(e).__name__}: {e}")
        sys.exit(1)

    token = getattr(mm, "token", None)
    if not token:
        print("\n⚠️  Logged in but no token to save.")
        sys.exit(1)

    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
    print(f"\n✅ Token saved to keyring ({KEYRING_SERVICE} / {KEYRING_USERNAME}).")
    print(f"   token prefix: {token[:8]}…  — restart Claude to pick it up.")


if __name__ == "__main__":
    if "selftest" in sys.argv:
        print("import/selftest OK")
    else:
        asyncio.run(main())
