import os
import sys
import re
import subprocess
from typing import List


def read_modules_list(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    raw = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            # Strip comments
            line = line.split("#", 1)[0]
            raw.append(line)
    # Normalize: split on comma/semicolon/space/newline, trim, validate
    tokens: List[str] = []
    for chunk in "\n".join(raw).replace(";", ",").replace(",", " ").split():
        c = chunk.strip()
        if not c:
            continue
        # Allow module names like web_enterprise, helpdesk-custom (dash allowed by script too)
        if not all(ch.isalnum() or ch in ("_", "-") for ch in c):
            continue
        tokens.append(c)
    # Dedupe preserving order
    seen = set()
    result: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_BOLD = "\033[1m"


def _colorize_token(token: str, level: str) -> str:
    lvl = (level or "").upper()
    if lvl == "ERROR":
        color = ANSI_RED
    elif lvl in ("WARNING", "WARN"):
        color = ANSI_YELLOW
    else:
        color = ANSI_GREEN
    return f"{color}{ANSI_BOLD}{token}{ANSI_RESET}"


def log_info(message: str) -> None:
    print(f"{_colorize_token('INFO', 'INFO')} {message}", flush=True)


def log_warning(message: str) -> None:
    print(f"{_colorize_token('WARNING', 'WARNING')} {message}", flush=True)


def log_error(message: str) -> None:
    print(f"{_colorize_token('ERROR', 'ERROR')} {message}", file=sys.stderr, flush=True)


def colorize_severity_token_in_line(line: str) -> str:
    pattern = re.compile(r"\b(INFO|WARNING|WARN|ERROR|CRITICAL)\b")

    def repl(m: re.Match) -> str:
        token = m.group(1)
        level = "WARNING" if token == "WARN" else token
        return _colorize_token(token, level)

    return pattern.sub(repl, line, count=1)


def stream_odoo_cli(cmd: str) -> int:
    try:
        with subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        ) as proc:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                print(colorize_severity_token_in_line(line))
            proc.wait()
            return int(proc.returncode or 0)
    except Exception as e:
        log_error(f"Failed to run command: {e}")
        return 1


def main() -> int:
    # Input file lives alongside this script (mounted read-only)
    base_dir = os.path.dirname(__file__)
    txt_path = os.path.join(base_dir, "install-modules.txt")

    modules = read_modules_list(txt_path)
    if not modules:
        log_info("No modules to install (install-modules.txt empty or missing).")
        return 0

    db_name = os.environ.get("TARGET_DB_NAME") or os.environ.get("PGDATABASE")
    if not db_name:
        log_error("TARGET_DB_NAME not set and PGDATABASE missing")
        return 2

    # Inspect DB to avoid re-installing already present modules
    from odoo import api, SUPERUSER_ID, tools
    from odoo.modules.registry import Registry

    tools.config["db_name"] = db_name

    # Show requested modules upfront
    log_info("Requested modules: " + ",".join(modules))

    to_install: List[str] = []
    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        Mod = env["ir.module.module"].sudo()
        for name in modules:
            rec = Mod.search([["name", "=", name]], limit=1)
            if not rec:
                log_error(f"Module not found: {name}")
                continue
            if rec.state in ("installed", "to install", "to upgrade"):
                continue
            to_install.append(name)

    if not to_install:
        log_info("All requested modules already installed or queued; nothing to do.")
        return 0

    # Use CLI to perform installation of the minimal set
    mods = ",".join(sorted(to_install))
    # Prefer standard container path; host config is copied there by tooling
    odoo_rc = "/etc/odoo/odoo.conf"
    # Use a non-default HTTP port and localhost interface to avoid conflicts with running Odoo
    safe_iface = "127.0.0.1"
    safe_port = "8071"
    log_info(f"Installing via CLI: -c {odoo_rc} -d {db_name} -i {mods} --http-interface {safe_iface} --http-port {safe_port} --stop-after-init")
    cmd = f"odoo -c {odoo_rc} -d {db_name} -i {mods} --http-interface {safe_iface} --http-port {safe_port} --stop-after-init"
    code = stream_odoo_cli(cmd)
    if code != 0:
        log_error("Failed to install modules via CLI; check module names and config.")
        return 4
    log_info("Installed modules: " + ",".join(sorted(to_install)))
    return 0


if __name__ == "__main__":
    sys.exit(main())



