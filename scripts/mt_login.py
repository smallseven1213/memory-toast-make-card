#!/usr/bin/env python3
"""Memory Toast login helper for the make-card skill.

Commands:
  login    Prompt for email + password, exchange them for a refresh token, store it.
  token    Log in by pasting a token copied from the app — for Google/Facebook users
           who have no password (app → Settings → Copy upload token).
  whoami   Show the logged-in account (prints no secrets).
  logout   Delete the stored credentials.

Secrets are read interactively (never echoed) and never written to disk; only the
rotating 7-day refresh token is stored, in ~/.memory-toast/credentials.json
(chmod 600). Run `login` (or `token`) once, then `upload_pack.py` refreshes automatically.

Examples:
  python3 scripts/mt_login.py                 # same as `login`
  python3 scripts/mt_login.py token           # paste a token (social-login users)
  python3 scripts/mt_login.py whoami
  python3 scripts/mt_login.py logout
  python3 scripts/mt_login.py --api https://my-server.example.com login
"""

import argparse
import getpass

import _mt_auth as auth


def cmd_login(args) -> None:
    api = auth.resolve_api_url(args.api)
    print(f"Memory Toast — {api}")
    email = input("Email: ").strip()
    if not email:
        auth.fail("email is required")
    password = getpass.getpass("Password: ")
    if not password:
        auth.fail("password is required")
    res = auth.login(api, email, password)
    stored_email = res.get("user", {}).get("email", email)
    auth.save_credentials({
        "apiUrl": api,
        "email": stored_email,
        "refreshToken": res["refreshToken"],
    })
    print(f"Logged in as {stored_email}.")
    print(f"Credentials saved to {auth.CRED_PATH} (chmod 600). "
          "Token stays valid as long as you upload at least once every 7 days.")


def cmd_token(args) -> None:
    api = auth.resolve_api_url(args.api)
    print(f"Memory Toast — {api}")
    token = args.token or getpass.getpass(
        "Paste your upload token (app → Settings → Copy upload token): ")
    token = token.strip()
    if not token:
        auth.fail("no token provided")
    email = auth.store_refresh_token(api, token)
    print(f"Logged in as {email} (via pasted token).")
    print(f"Credentials saved to {auth.CRED_PATH} (chmod 600). "
          "Token stays valid as long as you upload at least once every 7 days.")


def cmd_whoami(args) -> None:
    creds = auth.load_credentials()
    if not creds.get("refreshToken"):
        print("Not logged in. Run: python3 scripts/mt_login.py login")
        return
    print(f"Logged in as {creds.get('email', '(unknown)')}")
    print(f"Server: {creds.get('apiUrl', auth.DEFAULT_API_URL)}")


def cmd_logout(args) -> None:
    if auth.clear_credentials():
        print(f"Logged out — removed {auth.CRED_PATH}")
    else:
        print("Already logged out (no stored credentials).")


def main() -> None:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--api", help="override API base URL")
    parser = argparse.ArgumentParser(
        description=__doc__, parents=[base],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("login", parents=[base], help="log in and store a refresh token")
    p_token = sub.add_parser("token", parents=[base],
                             help="log in by pasting a token from the app (social-login users)")
    p_token.add_argument("--token", help="the token value (omit to be prompted, hidden)")
    sub.add_parser("whoami", parents=[base], help="show the logged-in account")
    sub.add_parser("logout", parents=[base], help="delete stored credentials")
    args = parser.parse_args()
    handler = {"login": cmd_login, "token": cmd_token,
               "whoami": cmd_whoami, "logout": cmd_logout}
    handler[args.command or "login"](args)


if __name__ == "__main__":
    main()
