"""
Connect Odoo to n8n (post-init hook)

Purpose:
- From inside the Odoo container context, configure the target Odoo database to
  trust and reference the n8n service running in the same stack.
- Idempotently ensure:
  - An Odoo system parameter for the n8n base URL is set
  - A technical user exists (login: 'n8n') with an API key (if Odoo supports it)
  - The API key value is printed to stdout once (re-used on subsequent runs)

Inputs (provided by orchestrator via environment):
- TARGET_DB_NAME: Odoo database name
- TARGET_ENV_NAME: environment name (for logs only)
- TARGET_TIER: tier (dev/stage/prod) for logs only
- N8N_URL: Base URL to reach n8n (default: http://n8n:5678)

Notes:
- This script runs inside the Odoo container (executed via dev-toolkit hooks).
- Uses Odoo's ORM directly; no external HTTP calls to Odoo are required.
"""

import os
import sys
from typing import Optional, Tuple


def _get_env(key: str, default: str = "") -> str:
    try:
        val = os.environ.get(key)
        return val if val is not None else default
    except Exception:
        return default


def _ensure_param(env, key: str, value: str) -> bool:
    """Ensure ir.config_parameter key is set to value. Return True if changed."""
    icp = env["ir.config_parameter"].sudo()
    current = (icp.get_param(key) or "").strip()
    if current == value:
        return False
    icp.set_param(key, value)
    return True


def _ensure_user_with_groups(env, login: str, password: Optional[str], group_xmlids: list[str]) -> int:
    Users = env["res.users"].sudo()
    user = Users.search([["login", "=", login]], limit=1)
    # Resolve group ids from xmlids; ignore missing
    group_ids: list[int] = []
    for xid in group_xmlids:
        try:
            rec = env.ref(xid)
            if rec and rec._name == "res.groups":
                group_ids.append(rec.id)
        except Exception:
            # best-effort; skip
            pass
    vals = {
        "name": login,
        "login": login,
    }
    if password:
        vals["password"] = password
    if group_ids:
        vals["group_ids"] = [(6, 0, group_ids)]
    if user:
        user.write(vals)
        return int(user.id)
    user = Users.with_context(no_reset_password=True).create(vals)
    return int(user.id)


def _ensure_api_key(env, user_id: int, purpose: str = "n8n", preferred_key: Optional[str] = None) -> Tuple[bool, str]:
    """Ensure there is an API key for the given user with a known note/purpose.

    Returns (created, key_value). For existing keys, key_value may be masked or empty
    depending on Odoo's version; in that case we do not change anything and return
    an empty string for the value (since it cannot be recovered).
    """
    # Odoo 16+ has res.users.apikey; earlier versions may not.
    try:
        ApiKey = env["res.users.apikey"].sudo()
    except Exception:
        return False, ""

    # If a preferred key is provided (from secrets), attempt to install it
    if preferred_key:
        try:
            ApiKey.create({"user_id": user_id, "key": preferred_key, "note": purpose})
            return True, str(preferred_key)
        except Exception:
            # try with an alternate note to bypass uniqueness constraints if any
            try:
                ApiKey.create({"user_id": user_id, "key": preferred_key, "note": f"{purpose}-file"})
                return True, str(preferred_key)
            except Exception:
                # fall through to normal generation path
                pass

    # Look for an existing key by note/purpose (cannot recover plaintext)
    existing = ApiKey.search([["user_id", "=", user_id], ["note", "=", purpose]], limit=1)
    if existing:
        return False, ""

    # Create a new API key and return its plaintext value
    try:
        # modern Odoo exposes a helper to create and return key in one call
        if hasattr(ApiKey, "_generate_key"):
            key_val = ApiKey._generate_key()
            ApiKey.create({"user_id": user_id, "key": key_val, "note": purpose})
            return True, str(key_val)
    except Exception:
        # fallback to model create which may auto-generate the key and return masked
        try:
            rec = ApiKey.create({"user_id": user_id, "note": purpose})
            # Some versions return the raw key in a field named 'key'
            key_val = getattr(rec, "key", None)
            return True, str(key_val or "")
        except Exception:
            return False, ""


def _resolve_db_name() -> str:
    # 1) Explicit env from orchestrator
    db = (_get_env("TARGET_DB_NAME", "") or "").strip()
    if db:
        return db
    # 2) Compose naming: <tier>_<env>_db
    env_name = (_get_env("TARGET_ENV_NAME", "") or "").strip()
    tier = (_get_env("TARGET_TIER", "") or "").strip()
    if env_name and tier:
        return f"{tier}_{env_name}_db"
    # 3) Container envs
    db2 = (_get_env("PGDATABASE", "") or _get_env("POSTGRES_DB", "") or "").strip()
    if db2:
        return db2
    return ""


def _secrets_path(env_name: str, tier: str) -> str:
    base = "/opt/app-dev/odoo/.secrets"
    env_part = (env_name or "").strip() or "default"
    tier_part = (tier or "").strip() or "dev"
    return os.path.join(base, env_part, tier_part, "api-key.txt")


def _container_secrets_path(env_name: str, tier: str) -> str:
    base = "/var/lib/odoo/.secrets"
    env_part = (env_name or "").strip() or "default"
    tier_part = (tier or "").strip() or "dev"
    return os.path.join(base, env_part, tier_part, "api-key.txt")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_text(path: str, content: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write((content or "").strip() + "\n")
        return True
    except Exception:
        return False


def main() -> int:
    db_name = _resolve_db_name()
    env_name = _get_env("TARGET_ENV_NAME", "")
    tier = _get_env("TARGET_TIER", "")
    n8n_url = _get_env("N8N_URL", "http://n8n:5678").rstrip("/")

    # Lazy import Odoo only in the container
    from odoo import api, SUPERUSER_ID, tools
    from odoo.modules.registry import Registry

    tools.config["db_name"] = db_name

    if not db_name:
        print("n8n connect: target DB name unknown; skipping.")
        return 0

    try:
        ctx_mgr = Registry(db_name).cursor()
    except Exception as e:
        msg = str(e)
        # Skip gracefully when database does not exist yet
        if "does not exist" in msg.lower():
            print(f"n8n connect: database '{db_name}' not found; skipping.")
            return 0
        print(f"n8n connect: failed to open registry for '{db_name}': {e}", file=sys.stderr)
        return 1

    with ctx_mgr as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        changed = False

        # 1) Ensure n8n base URL parameter
        # Use a namespaced key to avoid collisions. If you standardize another
        # key in your modules, adjust here accordingly.
        param_key = "integration.n8n.base_url"
        changed = _ensure_param(env, param_key, n8n_url) or changed

        # 2) Ensure a technical user for n8n and an API key when supported
        login = "n8n"
        # Default password is 'n8n' (can be overridden via env)
        password = _get_env("N8N_ODOO_PASSWORD", "n8n")
        # Grant system rights so flows can call admin-level endpoints if needed
        groups = ["base.group_system"]
        uid = _ensure_user_with_groups(env, login, password if password else None, groups)

        # Secrets file path for persisting/reusing the API key
        secrets_file = _secrets_path(env_name, tier)
        secrets_file_container = _container_secrets_path(env_name, tier)
        existing_secret = _read_text(secrets_file) if (env_name and tier) else ""
        created_key = False
        key_val = ""
        if existing_secret:
            # Install the known key from file (preferred)
            created_key, key_val = _ensure_api_key(env, uid, purpose="n8n", preferred_key=existing_secret)
            if created_key:
                print("Installed API key from secrets file for user 'n8n'.")
        else:
            created_key, key_val = _ensure_api_key(env, uid, purpose="n8n")
            if created_key and key_val:
                # Persist new key to secrets file
                if _write_text(secrets_file, key_val):
                    print(f"Wrote API key to {secrets_file}")
                else:
                    print(f"Warning: failed to write API key file: {secrets_file}", file=sys.stderr)
            elif (not created_key) and (env_name and tier):
                # If a key already exists but no file is present, create a new key with a different note
                try:
                    import time as _t
                    alt_purpose = f"n8n-{int(_t.time())}"
                    created_key, key_val = _ensure_api_key(env, uid, purpose=alt_purpose)
                    if created_key and key_val:
                        if _write_text(secrets_file, key_val):
                            print(f"Wrote API key to {secrets_file}")
                        else:
                            print(f"Warning: failed to write API key file: {secrets_file}", file=sys.stderr)
                except Exception:
                    pass
        # Always try to write the key to a container-persistent path for harvesting
        if key_val:
            _write_text(secrets_file_container, key_val)
        if (not created_key) and (not existing_secret):
            # No new key created and no file available (likely model missing or key already exists)
            print("Odoo API key already present or API key model not available.")

        # Emit a brief summary
        hint_env = f" env={env_name}" if env_name else ""
        hint_tier = f" tier={tier}" if tier else ""
        if changed:
            print(f"Configured n8n URL '{n8n_url}' in Odoo ({db_name}).{hint_env}{hint_tier}")
        else:
            print(f"n8n URL already configured as '{n8n_url}' in Odoo ({db_name}).{hint_env}{hint_tier}")

    return 0


if __name__ == "__main__":
    sys.exit(main())


