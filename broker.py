"""
Broker session factory (Fyers API v3).

The production deployment uses a separate TOTP-based auto-login module that
refreshes the access token every morning. It is intentionally not included in
this repository because it handles account credentials. This module expects a
valid access token in the environment instead.
"""
import os

import config  # noqa: F401  (loads .env)


def get_fyers():
    """Return an authenticated FyersModel, or None if credentials are missing."""
    app_id = os.getenv("FYERS_APP_ID")          # e.g. XXXXXXXXXX-200
    token  = os.getenv("FYERS_ACCESS_TOKEN")
    if not app_id or not token:
        print("[broker] FYERS_APP_ID / FYERS_ACCESS_TOKEN not set")
        return None
    from fyers_apiv3 import fyersModel
    return fyersModel.FyersModel(client_id=app_id, token=token,
                                 is_async=False, log_path="")
