"""
Zerodha Kite Connect Authentication Helper (CLI)
Run this script once daily before market hours to generate your active session access token.

Usage:
    python auth.py
"""

import webbrowser
from urllib.parse import parse_qs, urlparse

from broker.kite_adapter import KiteBrokerAdapter
from config.settings import settings


def extract_request_token(input_str: str) -> str:
    """Extracts request_token from either raw token or full redirected URL."""
    input_str = input_str.strip()
    if "request_token=" in input_str:
        parsed = urlparse(input_str)
        params = parse_qs(parsed.query)
        if "request_token" in params:
            return params["request_token"][0]
    return input_str


def authenticate():
    if not settings.validate_api_credentials():
        return None

    if not settings.kite_api_secret:
        print("[!] ERROR: KITE_API_SECRET is missing in .env file.")
        return None

    adapter = KiteBrokerAdapter.get_instance()
    login_url = adapter.get_login_url()

    print("=" * 60)
    print("  ZERODHA KITE CONNECT - DAILY AUTHENTICATION")
    print("=" * 60)
    print("\n1. Opening login URL in your browser...")
    print(f"\n   URL: {login_url}\n")

    try:
        webbrowser.open(login_url)
    except Exception:
        print("   (Could not open browser automatically. Please copy & paste the URL manually.)")

    print("2. Log in with your Zerodha credentials and TOTP.")
    print("3. After login, you will be redirected to your Redirect URL.")
    print("   Copy the full redirect URL or the 'request_token' parameter value from the browser address bar.\n")

    user_input = input("Paste the request token or redirect URL here: ").strip()
    request_token = extract_request_token(user_input)

    if not request_token:
        print("[!] ERROR: No request token provided.")
        return None

    print("\n[+] Exchanging request token for session access token...")
    try:
        payload = adapter.generate_session_from_request_token(request_token)
        user_name = payload.get("user_name", "Trader")
        user_id = payload.get("user_id", "")

        print(f"\n[✓] Success! Authenticated as: {user_name} ({user_id})")
        print(f"[✓] Access token saved to '{settings.token_file.name}'")
        print("[✓] Valid for today's trading session.")
        return payload.get("access_token")

    except Exception as e:
        print(f"\n[!] Authentication failed: {e}")
        return None


if __name__ == "__main__":
    authenticate()
