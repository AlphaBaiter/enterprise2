"""
Odoo post-init bootstrap script

Purpose:
- Targets the configured database (DB_NAME) and ensures a default user
  (USER_LOGIN/USER_PASSWORD) with the requested access groups (USER_GROUPS).
- Activates specified interface languages (LANG_CODES), e.g., Hebrew (he_IL).
- Designed to be idempotent: safe to re-run without creating duplicates.

Notes:
- Runs inside the Odoo container after services start (invoked by dev-utils).
- All settings are configured via constants below; no external arguments.
"""

import os
import sys
from typing import List, Sequence, Tuple

# --- Configure here ---
# Database name for the target Odoo DB (set explicitly for dev)
DB_NAME: str = ""

# User to create/update
USER_LOGIN: str = "user"
USER_PASSWORD: str = "user"

# Languages to activate (language codes like 'he_IL')
LANG_CODES: Sequence[str] | str = ["en_US", "he_IL"]

# Access rights: shorthands (internal, portal, admin, public) or xmlids like base.group_system
# Can be a Python list or a single comma-separated string.
USER_GROUPS: Sequence[str] | str = ["internal"]


def normalize_tokens(val: Sequence[str] | str | None) -> List[str]:
    if not val:
        return []
    if isinstance(val, str):
        raw = val
        tokens = []
        for chunk in raw.replace(";", ",").split(","):
            c = chunk.strip()
            if c:
                tokens.append(c)
        return tokens
    return [s.strip() for s in val if s and s.strip()]


def ensure_rtlcss_installed() -> None:
    """Ensure 'rtlcss' CLI is available in PATH inside the container.

    - If rtlcss is missing, try installing via npm (preferred).
    - If npm is missing, attempt to install nodejs/npm via common package managers,
      then install rtlcss globally.
    - Best-effort and idempotent; logs warnings on failure but does not raise.
    """
    def _ok(cmd: str) -> bool:
        # os.system returns shell exit status; 0 indicates success
        return os.system(cmd) == 0

    # Fast path: present
    if _ok("rtlcss --version >/dev/null 2>&1"):
        return

    # Ensure npm exists
    if not _ok("npm -v >/dev/null 2>&1"):
        # Try common distros
        if _ok("command -v apt-get >/dev/null 2>&1"):
            os.system("apt-get update -y && apt-get install -y --no-install-recommends nodejs npm")
        elif _ok("command -v apk >/dev/null 2>&1"):
            os.system("apk add --no-cache nodejs npm")
        elif _ok("command -v dnf >/dev/null 2>&1"):
            os.system("dnf install -y nodejs npm")
        elif _ok("command -v yum >/dev/null 2>&1"):
            os.system("yum install -y nodejs npm")

    # Install rtlcss via npm when possible
    if _ok("npm -v >/dev/null 2>&1"):
        os.system("npm --silent -g install --unsafe-perm rtlcss")

    # Final verification and logging
    if _ok("rtlcss --version >/dev/null 2>&1"):
        print("rtlcss is available")
    else:
        print("Warning: rtlcss not available after attempted install", file=sys.stderr)


def validate_enterprise(db_name: str) -> Tuple[bool, str]:
    from odoo import api, SUPERUSER_ID, tools
    from odoo.modules.registry import Registry

    tools.config["db_name"] = db_name
    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        icp = env["ir.config_parameter"].sudo()
        modules = env["ir.module.module"].sudo()

        code = (icp.get_param("database.enterprise_code") or "").strip()
        web_ent_installed = bool(
            modules.search([["name", "=", "web_enterprise"], ["state", "=", "installed"]], limit=1)
        )
        ent_dir_ok = os.path.isdir("/mnt/addons/enterprise") and os.path.isdir(
            "/mnt/addons/enterprise/web_enterprise"
        )

        problems = []
        if not code:
            problems.append("missing 'database.enterprise_code' in ir.config_parameter")
        if not web_ent_installed:
            problems.append("module 'web_enterprise' is not installed")
        if not ent_dir_ok:
            problems.append("enterprise addons path or 'web_enterprise' directory not found")

        if problems:
            return False, "; ".join(problems)
        return True, "OK"


def main() -> int:
    # Prefer env vars if provided by dev-utils
    db_name = os.environ.get("TARGET_DB_NAME") or DB_NAME or "dev_db"
    env_name = os.environ.get("TARGET_ENV_NAME", "")
    tier = os.environ.get("TARGET_TIER", "")
    print("Installing rtlcss extension (rtlcss) if missing...", flush=True)
    # Ensure rtlcss for RTL asset builds (best-effort)
    try:
        ensure_rtlcss_installed()
    except Exception:
        # Non-fatal; continue bootstrap
        pass
    env_langs = os.environ.get("ODOO_LANGS", "").strip()
    if env_langs:
        lang_codes = normalize_tokens(env_langs)
    else:
        lang_codes = normalize_tokens(LANG_CODES)

    # Default user credentials and groups (allow env overrides)
    login = os.environ.get("USER_LOGIN") or USER_LOGIN
    password = os.environ.get("USER_PASSWORD") or USER_PASSWORD
    groups_tokens = normalize_tokens(os.environ.get("USER_GROUPS") or USER_GROUPS)

    # Optionally install Python requirements from hooks folder (only if non-empty)
    hooks_dir = os.environ.get("HOOKS_PATH", "/opt/hooks")
    hooks_dir = os.path.join(hooks_dir, "post-init")
    req_path = os.path.join(hooks_dir, "requirements.txt")
    # Prefer install-modules.txt, fallback to modules.txt for backward compatibility
    mods_path_install = os.path.join(hooks_dir, "install-modules.txt")
    mods_path_legacy = os.path.join(hooks_dir, "modules.txt")
    try:
        if os.path.isfile(req_path):
            # Only run pip when file contains at least one non-comment, non-empty line
            with open(req_path, "r", encoding="utf-8") as rf:
                has_reqs = any(line.strip() and not line.strip().startswith("#") for line in rf)
            if has_reqs:
                # Use python -m pip, quiet flags, and --user to avoid noisy warnings
                os.system(
                    f"python3 -m pip install --user --no-cache-dir --disable-pip-version-check -q -r {req_path}"
                )
    except Exception as e:
        print(f"Warning: failed to install requirements: {e}", file=sys.stderr)

    # Lazy import Odoo only in container
    from odoo import api, SUPERUSER_ID, tools
    from odoo.modules.registry import Registry

    tools.config["db_name"] = db_name

    def resolve_groups(env, tokens: List[str]) -> List[int]:
        group_ids: List[int] = []
        if not tokens:
            return group_ids
        by_token = {
            "internal": ["base.group_user"],
            "portal": ["base.group_portal"],
            "public": ["base.group_public"],
            "admin": ["base.group_system"],
            "settings": ["base.group_system"],
            "manager": ["base.group_system"],
        }
        for token in tokens:
            # If token looks like an xmlid (module.name), use directly; otherwise map shorthands
            xmlids = [token] if "." in token else by_token.get(token.lower(), [])
            for xid in xmlids:
                try:
                    rec = env.ref(xid)
                    if rec and rec._name == "res.groups":
                        group_ids.append(rec.id)
                except Exception:
                    print(f"Warning: group xmlid '{xid}' not found", file=sys.stderr)
        # Deduplicate preserving order
        seen = set()
        uniq: List[int] = []
        for gid in group_ids:
            if gid not in seen:
                seen.add(gid)
                uniq.append(gid)
        return uniq

    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        Users = env["res.users"]
        Lang = env["res.lang"]
        group_ids = resolve_groups(env, groups_tokens)

        # Activate languages
        if lang_codes:
            activated: List[str] = []
            for code in lang_codes:
                try:
                    Lang._activate_lang(code)
                    activated.append(code)
                except Exception as e:
                    print(f"Warning: failed to activate language {code}: {e}", file=sys.stderr)
            if activated:
                print("Activated languages: " + ",".join(activated))

        # Module installation is handled by orchestrator or dedicated hook script.
        # Intentionally not invoking `odoo -i` here to avoid duplicate installs and shell parsing issues.

        # For production tier, validate enterprise setup
        if tier == "prod":
            ok, detail = validate_enterprise(db_name)
            if not ok:
                print(f"Enterprise DB validation FAILED for '{db_name}'{f' (env={env_name})' if env_name else ''}: {detail}", file=sys.stderr)
            else:
                print(f"Enterprise DB validation PASSED for '{db_name}'{f' (env={env_name})' if env_name else ''}.")

        # Ensure default user (idempotent)
        user = Users.search([["login", "=", login]], limit=1)
        values = {
            "name": login.capitalize(),
            "login": login,
            "password": password,
        }
        if group_ids:
            values["group_ids"] = [(6, 0, group_ids)]
        try:
            if user:
                user.write(values)
                print(f"Updated user '{login}' with {len(group_ids)} groups")
            else:
                Users.with_context(no_reset_password=True).create(values)
                print(f"Created user '{login}' with {len(group_ids)} groups")
        except Exception as e:
            print(f"Failed to bootstrap user '{login}': {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())



