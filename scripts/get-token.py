#!/usr/bin/env python3
"""
Get a Cognito ID token for manual/scripted API testing in cloud mode.

The normal way to authenticate is the browser BFF flow (/auth/login →
/auth/callback), which sets the ``session`` httpOnly cookie automatically — the
React UI needs nothing from this script. Use this script only when you want to
call the API **by hand** (curl/httpie) without a browser.

The server authenticates cloud requests from the ``session`` cookie only; it does
not read an ``Authorization: Bearer`` header. So pass the ID token as a cookie:

    TOKEN=$(python scripts/get-token.py user@example.com)
    curl -b "session=$TOKEN" http://localhost:8000/projects

Usage:
    # Print the ID token (default — this is what goes in the session cookie)
    python scripts/get-token.py user@example.com

    # Print the access token (not consumed by the API; kept for ad-hoc use)
    python scripts/get-token.py user@example.com --access-token

    # Print both as shell variable assignments (eval to set both at once)
    eval $(python scripts/get-token.py user@example.com --env)

Requires ALLOW_USER_PASSWORD_AUTH on the Cognito app client. Tokens expire after
1 hour — re-run to refresh.
"""

import getpass
import json
import sys
import urllib.error
import urllib.request

USER_POOL_ID = "us-east-1_BSBhcKA66"
CLIENT_ID    = "1ugglpalgp9r2gvb24s2v7dunq"
REGION       = "us-east-1"
ENDPOINT     = f"https://cognito-idp.{REGION}.amazonaws.com/"


def get_tokens(email: str, password: str) -> dict:
    body = json.dumps({
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": CLIENT_ID,
        "AuthParameters": {"USERNAME": email, "PASSWORD": password},
    }).encode()

    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            "Content-Type": "application/x-amz-json-1.1",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)["AuthenticationResult"]
    except urllib.error.HTTPError as e:
        error = json.load(e)
        print(f"Auth failed: {error.get('message', e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "id"
    email_arg = None
    for arg in args:
        if arg == "--access-token":
            mode = "access"
        elif arg == "--env":
            mode = "env"
        else:
            email_arg = arg

    email    = email_arg or input("Email: ")
    password = getpass.getpass("Password: ")
    result   = get_tokens(email, password)

    if mode == "access":
        print(result["AccessToken"])
    elif mode == "env":
        print(f"TOKEN={result['IdToken']}")
        print(f"ACCESS_TOKEN={result['AccessToken']}")
    else:
        print(result["IdToken"])
