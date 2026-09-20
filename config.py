import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

API_KEY = os.getenv("KITE_API_KEY", "").strip()
API_SECRET = os.getenv("KITE_API_SECRET", "").strip()
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "").strip()
USER_ID = os.getenv("KITE_USER_ID", "").strip()
TOTP_KEY = os.getenv("KITE_TOTP_KEY", "").strip()

# Strategy / Risk Management parameters
MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", "10000"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "2000"))
SEMI_AUTOMATED_CONFIRMATION = os.getenv("SEMI_AUTOMATED_CONFIRMATION", "true").lower() in ("true", "1", "yes")

TOKEN_FILE = BASE_DIR / "session_token.json"


def validate_api_credentials():
    """Verify that Kite API Key is set."""
    if not API_KEY or API_KEY == "your_api_key_here":
        print("[!] ERROR: KITE_API_KEY is not set in .env file.")
        print("Please edit .env and provide your Zerodha Kite Connect API key.")
        return False
    return True
