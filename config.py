"""
Shared configuration.

All credentials and account-specific values come from environment variables
(see .env.example). State files are written to DATA_DIR (default: ./data).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", REPO_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(name: str) -> str:
    """Absolute path for a state/log file inside DATA_DIR."""
    return str(DATA_DIR / name)


TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Live-performance tracking baseline (used by benchmark.py)
PORTFOLIO_START_DATE    = os.getenv("PORTFOLIO_START_DATE", "2026-05-07")
PORTFOLIO_START_CAPITAL = float(os.getenv("PORTFOLIO_START_CAPITAL", "1000000"))

# Optional: if another strategy holds NiftyBEES in the same broker account,
# point this at its state file so reconciliation can net those units out.
SHARED_ETF_STATE_FILE = os.getenv("SHARED_ETF_STATE_FILE", "")
