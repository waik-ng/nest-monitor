#!/usr/bin/env python3
"""
One-time helper: exchange a Device Access authorization code for a
long-lived refresh token for nest_monitor.py.

Generated with Muse, Meta's AI assistant.

Prerequisites (see README.md — one-time Google setup, incl. the $5
Device Access registration):
  * Google Cloud project with Smart Device Management API enabled
  * OAuth client ID / secret — type **Web application**, with
    https://www.google.com added as an Authorized redirect URI.
    (Desktop clients do NOT accept https://www.google.com, so they
    fail with Error 400: redirect_uri_mismatch.)
  * Device Access project ID (UUID, from the Device Access Console)

Run on any machine with a browser:
  python3 get_refresh_token.py --project-id <DEVICE_ACCESS_UUID> \
      --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>

It prints an authorization URL. Open it, sign in with the Google account
that owns the Nest thermostat, allow access, and you will be redirected
to https://www.google.com/?code=... — paste that code back here.
It then prints SDM_REFRESH_TOKEN. Put all four values in .env.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request

AUTH_BASE = "https://nestservices.google.com/partnerconnections"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/sdm.service"


def main() -> int:
    ap = argparse.ArgumentParser(description="Get a Nest SDM refresh token")
    ap.add_argument("--project-id", required=True,
                    help="Device Access project ID (UUID)")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--redirect-uri", default="https://www.google.com",
                    help="Must match an Authorized redirect URI on the "
                         "OAuth client (default: https://www.google.com)")
    args = ap.parse_args()

    params = {
        "redirect_uri": args.redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "client_id": args.client_id,
        "response_type": "code",
        "scope": SCOPE,
    }
    url = f"{AUTH_BASE}/{args.project_id}/auth?{urllib.parse.urlencode(params)}"
    print("1) Open this URL in your browser:\n")
    print(f"   {url}\n")
    print("2) Sign in with the Google account that owns the thermostat,")
    print("   allow access, and you will land on google.com/?code=XXXX")
    code = input("\n3) Paste the full code= value (or the whole redirect URL): ").strip()
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [code])[0]
        if "code=" in code:
            code = code.split("code=", 1)[1].split("&")[0]

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": args.redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except Exception as exc:
        print(f"Token exchange failed: {exc}")
        return 1

    refresh = payload.get("refresh_token")
    if not refresh:
        print(f"No refresh_token returned: {payload}")
        print("Hint: re-run with prompt=consent (already set) and make sure you")
        print("used a fresh code — codes expire in minutes and work only once.")
        return 1

    print("\nSuccess. Add these to .env:\n")
    print(f"SDM_PROJECT_ID={args.project_id}")
    print(f"SDM_CLIENT_ID={args.client_id}")
    print(f"SDM_CLIENT_SECRET={args.client_secret}")
    print(f"SDM_REFRESH_TOKEN={refresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
