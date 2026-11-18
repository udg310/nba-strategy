# -*- coding: utf-8 -*-
"""Generate Polymarket CLOB API credentials locally.

Reads ~/.polymarket/env (mode 600) with:
    POLY_PRIVATE_KEY=0x...          # EOA private key (never leaves this machine)
    POLY_FUNDER=0x...               # account wallet address (from profile page)
    POLY_SIGNATURE_TYPE=3           # 3 = Deposit Wallet; legacy wallets may use 1 or 2

Writes ~/.polymarket/creds.json (mode 600):
    {"api_key":..., "api_secret":..., "api_passphrase":..., "funder":..., "signature_type":...}

Usage:
    python3 scripts/pm_creds.py            # derive (re-derives if already exists)
    python3 scripts/pm_creds.py --check    # only validate creds file exists
"""
import json, os, stat, sys

HOME = os.path.expanduser("~")
ENV_PATH = os.path.join(HOME, ".polymarket", "env")
CREDS_PATH = os.path.join(HOME, ".polymarket", "creds.json")

def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in ("POLY_PRIVATE_KEY", "POLY_FUNDER",
                                           "POLY_SIGNATURE_TYPE") if k in os.environ})
    for k in ("POLY_PRIVATE_KEY", "POLY_FUNDER"):
        if not env.get(k):
            sys.exit(f"set {k} in environment or {ENV_PATH}")
    env.setdefault("POLY_SIGNATURE_TYPE", "3")
    return env

def main():
    if "--check" in sys.argv:
        if os.path.exists(CREDS_PATH):
            d = json.load(open(CREDS_PATH))
            print(f"creds ok: funder={d.get('funder')} sig={d.get('signature_type')} "
                  f"key={str(d.get('api_key'))[:8]}...")
        else:
            print("creds missing")
        return

    env = load_env()
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError:
        sys.exit("install scripts/requirements-executor-live.txt first")

    funder = env["POLY_FUNDER"]
    sig = int(env["POLY_SIGNATURE_TYPE"])
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=env["POLY_PRIVATE_KEY"],
        chain_id=137,
        signature_type=sig,
        funder=funder,
    )
    creds = client.create_or_derive_api_key()
    out = {
        "api_key": creds.api_key,
        "api_secret": creds.api_secret,
        "api_passphrase": creds.api_passphrase,
        "funder": funder,
        "signature_type": sig,
    }
    os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
    with open(CREDS_PATH, "w") as f:
        json.dump(out, f, indent=1)
    os.chmod(CREDS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    print(f"written {CREDS_PATH} (funder={funder}, sig={sig})")
    print("do NOT commit or share this file.")

if __name__ == "__main__":
    main()
