"""Set or verify a Home Assistant-only PIN through an interactive terminal."""

# This executable CLI intentionally writes status text directly to its terminal.
# ruff: noqa: T201, TRY300

from __future__ import annotations

import getpass
import importlib.util
import json
from pathlib import Path
import sys
import warnings

# Load only the helper, without importing or starting the HA integration.
spec = importlib.util.spec_from_file_location(
    "qolsys_private_pin", Path(__file__).with_name("pin_auth.py")
)
assert spec is not None and spec.loader is not None
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
spec.loader.exec_module(auth)


def main() -> int:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Use an interactive SSH terminal; PIN input must not be echoed.")
        return 1
    # Abort if getpass cannot disable terminal echo; never fall back to input().
    warnings.filterwarnings("error", category=getpass.GetPassWarning)
    if sys.argv[1:] not in ([], ["--check"]):
        print("Usage: set_pin.py [--check]. Never supply a PIN on the command line.")
        return 1
    root = Path("/config/qolsys_panel")
    path = root / "users.conf"
    try:
        users = json.loads((root / "pin-users.json").read_text())
        allowed = frozenset(user["id"] for user in users)
        if not allowed:
            print("No panel users available. Check that the integration is connected.")
            return 1
        if sys.argv[1:] == ["--check"]:
            pin = getpass.getpass("Home Assistant PIN (hidden): ")
            matched = auth.verify_pin(path, pin, allowed)
            del pin
            print(
                "PIN verified locally; no command sent to the alarm panel."
                if matched != -1
                else "PIN did not match; no command sent to the alarm panel."
            )
            return 0 if matched != -1 else 1
        print("Panel user IDs available for attribution:")
        for user in users:
            print(f"  {user['id']}: {user['name']}")
        selected = input("Panel user ID to map your Home Assistant PIN to: ").strip()
        if (
            not selected.isascii()
            or not selected.isdigit()
            or int(selected) not in allowed
        ):
            print("Choose an ID from the list above.")
            return 1
        user_id = int(selected)
        print("Choose a separate Home Assistant PIN of 4 to 12 digits.")
        print("It will not change your physical panel code.")
        pin = getpass.getpass("New PIN (hidden): ")
        repeat = getpass.getpass("Repeat PIN (hidden): ")
        if pin != repeat:
            print("PINs did not match. Nothing saved.")
            return 1
        auth.set_user_pin(path, user_id, pin, allowed)
        del pin, repeat
        print("Saved an Argon2id hash with a random salt and 0600 file permissions.")
        print("Ready immediately; Home Assistant does not need a restart.")
        return 0
    except (auth.PinStoreError, OSError, ValueError, KeyError, getpass.GetPassWarning):
        print("Setup failed safely. Check the private PIN store and panel connection.")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        raise SystemExit(1) from None
