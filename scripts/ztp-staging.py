#!/usr/bin/env python3
"""
ZTP staging for brownfield BCM/Cumulus environments.

Stages switch configuration from the running switch into BCM htdocs so that a
future ZTP run (DR/RMA/maintenance window) can restore config and optionally
upgrade images — without enabling ZTP automatically.

Config source of truth (critical): /etc/nvue.d/startup.yaml
"""

import argparse
import csv
import datetime as _dt
import getpass
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# BCM version compatibility
from bcm_compat import BCMProps, get_bcm_version, get_cmsh_cmd


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / ".configs"
CONFIG_FILE = CONFIG_DIR / "config.json"
FILES_DIR = REPO_ROOT / ".files"

PASSWORD_ENV_VAR = "BCM_SWITCH_SSH_PASSWORD"

BCM_SWITCH_HTDOCS_DIR = Path("/cm/local/apps/cmd/etc/htdocs/switch")
BCM_SWITCH_IMAGE_DIR_CANDIDATES = [
    Path("/cm/local/apps/cmd/etc/htdocs/switch/image"),   # observed on BCM 10.x/11.x
    Path("/cm/local/apps/cmd/etc/htdocs/switch/images"),  # documented (some docs say plural)
]

SWITCH_STARTUP_YAML = Path("/etc/nvue.d/startup.yaml")


class IPAddressParser:
    """Parse IP addresses from a string supporting common range formats."""

    @staticmethod
    def parse(ip_string: str) -> List[str]:
        ips: List[str] = []
        parts = [p.strip() for p in ip_string.replace(" ", "").split(",")]
        for part in parts:
            if not part:
                continue
            if "-" in part:
                ips.extend(IPAddressParser._parse_range(part))
            else:
                try:
                    ipaddress.ip_address(part)
                    ips.append(part)
                except ValueError:
                    print(f"Warning: Invalid IP '{part}', skipping")
        return ips

    @staticmethod
    def _parse_range(range_str: str) -> List[str]:
        # Supports:
        # - 192.168.0.1-10
        # - 192.168.0.1-192.168.0.10
        start, end = range_str.split("-", 1)
        start = start.strip()
        end = end.strip()
        try:
            start_ip = ipaddress.ip_address(start)
        except ValueError:
            return []

        if "." in end:
            try:
                end_ip = ipaddress.ip_address(end)
            except ValueError:
                return []
        else:
            try:
                end_octet = int(end)
                if end_octet < 0 or end_octet > 255:
                    return []
            except ValueError:
                return []
            start_parts = start.split(".")
            if len(start_parts) != 4:
                return []
            end_ip = ipaddress.ip_address(".".join(start_parts[:3] + [str(end_octet)]))

        if int(end_ip) < int(start_ip):
            start_ip, end_ip = end_ip, start_ip

        out: List[str] = []
        cur = int(start_ip)
        end_int = int(end_ip)
        while cur <= end_int:
            out.append(str(ipaddress.ip_address(cur)))
            cur += 1
        return out


def _env_password() -> str:
    return os.environ.get(PASSWORD_ENV_VAR, "")


def _set_env_password(pw: str) -> None:
    os.environ[PASSWORD_ENV_VAR] = pw


def _run_cmsh(cmsh_cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [get_cmsh_cmd(), "-c", cmsh_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_ssh_command(
    host: str,
    *,
    username: str,
    password: str,
    command: str,
    stdin: Optional[str] = None,
    timeout: int = 60,
) -> Tuple[int, str, str]:
    """
    Run an SSH command.

    Tries SSH key auth first (BatchMode=yes). If that fails and a password is
    available, falls back to sshpass password auth.
    """
    base_opts_key = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
    ]
    cmd_key = ["ssh"] + base_opts_key + [f"{username}@{host}", command]
    try:
        r = subprocess.run(cmd_key, input=stdin, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return (r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        pass

    if not password:
        return (1, "", "SSH key auth failed and no password provided")

    if not shutil.which("sshpass"):
        return (1, "", "sshpass not found (required for password auth)")

    base_opts_pwd = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "PubkeyAuthentication=no",
    ]
    cmd_pwd = ["sshpass", "-p", password, "ssh"] + base_opts_pwd + [f"{username}@{host}", command]
    try:
        r = subprocess.run(cmd_pwd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return (r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")


def _read_devices_from_csv(csv_path: Path) -> List[Dict]:
    if not csv_path.exists():
        raise FileNotFoundError(str(csv_path))
    devices: List[Dict] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Accept either proper-case or lowercase keys
            norm = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            ip = norm.get("ip") or ""
            hostname = norm.get("hostname") or ""
            if not ip:
                continue
            devices.append({"hostname": hostname, "ip": ip})
    return devices


def _get_bcm_switches() -> List[Dict]:
    r = subprocess.run([get_cmsh_cmd(), "-c", "device;list -t switch"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "cmsh device list failed")
    return _parse_bcm_switch_list(r.stdout)


def _parse_bcm_switch_list(output: str) -> List[Dict]:
    switches: List[Dict] = []
    lines = output.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("Type") or ("--" in line and line.count("-") > 10):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0] != "Switch":
            continue
        hostname = parts[1]
        ip = ""
        for part in parts[3:]:
            if part.count(".") == 3:
                try:
                    ipaddress.ip_address(part)
                    ip = part
                    break
                except ValueError:
                    continue
        if ip:
            switches.append({"hostname": hostname, "ip": ip})
    return switches


def _detect_image_dir() -> Path:
    for p in BCM_SWITCH_IMAGE_DIR_CANDIDATES:
        if p.exists() and p.is_dir():
            return p
    raise RuntimeError(
        "Could not find BCM switch image directory. Checked: "
        + ", ".join(str(p) for p in BCM_SWITCH_IMAGE_DIR_CANDIDATES)
    )


def _pick_image_interactive(candidates: List[Path]) -> Path:
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        while True:
            raw = input("Enter path to a Cumulus image (.bin): ").strip()
            if not raw:
                print("Image path is required.")
                continue
            p = Path(raw).expanduser()
            if p.exists():
                return p
            print(f"Not found: {p}")

    if len(candidates) == 1:
        default = candidates[0]
        raw = input(f"Image path [{default}]: ").strip()
        return Path(raw).expanduser() if raw else default

    print("\nAvailable images:")
    for i, p in enumerate(candidates, 1):
        print(f"  {i}) {p}")
    while True:
        raw = input("Pick by number (or enter a different path): ").strip()
        if not raw:
            print("Please enter a number or a path.")
            continue
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
            print("Invalid selection.")
            continue
        p = Path(raw).expanduser()
        if p.exists():
            return p
        print(f"Not found: {p}")


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_existing(target: Path) -> Optional[Path]:
    if not target.exists():
        return None
    backup_dir = target.parent / "config-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"startup.{_timestamp()}.yaml"
    target.replace(backup_path)
    return backup_path


def _stage_startup_yaml(
    *,
    device: Dict,
    username: str,
    password: str,
    update_policy: str,  # "changed" | "all"
) -> Tuple[bool, str]:
    """
    Returns (changed_or_written, message)
    """
    hostname = device["hostname"]
    ip = device["ip"]

    if not hostname:
        return (False, f"{ip}: missing hostname (cannot stage under BCM htdocs)")

    # Fetch from switch (sudo required to be safe)
    cmd = f"sudo -S cat {SWITCH_STARTUP_YAML}"
    rc, out, err = _run_ssh_command(
        ip,
        username=username,
        password=password,
        command=cmd,
        stdin=(password + "\n") if password else "\n",
        timeout=60,
    )
    if rc != 0:
        tail = (err or out or "").strip()[-800:]
        return (False, f"{hostname}: failed to fetch {SWITCH_STARTUP_YAML} via SSH (rc={rc}): {tail}")

    remote_bytes = out.encode("utf-8", errors="replace")
    dest_dir = BCM_SWITCH_HTDOCS_DIR / hostname
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "startup.yaml"

    if dest.exists():
        local_bytes = dest.read_bytes()
        if local_bytes == remote_bytes and update_policy == "changed":
            return (False, f"{hostname}: startup.yaml unchanged (no update)")
        # backup before overwrite
        b = _backup_existing(dest)
        if b:
            print(f"  - {hostname}: backed up previous staged config to {b}")

    dest.write_bytes(remote_bytes)
    return (True, f"{hostname}: staged startup.yaml -> {dest}")


def _cmsh_set_file_mode(hostname: str) -> None:
    # BCM 10 uses cumulusmode/cumulusfile, BCM 11 uses nvconfigurationmode/nvconfigurationfile
    props = BCMProps()
    r = _run_cmsh(f"device; use {hostname}; set {props.config_mode} file; set {props.config_file} startup.yaml; commit")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"cmsh set {props.config_mode}/{props.config_file} failed")


def _cmsh_initialize(hostname: str) -> None:
    r = _run_cmsh(f"device; use {hostname}; initialize")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "cmsh initialize failed")


def _cmsh_set_image(hostname: str, image_filename: str) -> None:
    r = _run_cmsh(
        f"device; use {hostname}; ztpsettings; set image {image_filename}; set checkimageonboot no; commit"
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "cmsh ztpsettings image failed")


def _parse_vars_from_ztp_script(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    txt = path.read_text(errors="replace")
    for m in re.finditer(r"^([A-Z0-9_]+)='([^']*)'\s*$", txt, flags=re.MULTILINE):
        out[m.group(1)] = m.group(2)
    return out


def _maybe_delete_config_file(*, non_interactive: bool) -> None:
    if not CONFIG_FILE.exists():
        return
    if non_interactive:
        try:
            CONFIG_FILE.unlink()
        except Exception as e:
            print(f"Warning: failed to delete {CONFIG_FILE}: {e}")
        return
    resp = input(f"\nStaging completed. Delete {CONFIG_FILE} (progress-tracking config)? (y/n) [y]: ").strip().lower()
    if resp in ["", "y", "yes"]:
        try:
            CONFIG_FILE.unlink()
            print(f"Deleted {CONFIG_FILE}")
        except Exception as e:
            print(f"Warning: failed to delete {CONFIG_FILE}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage BCM ZTP artifacts from running switch configuration")
    parser.add_argument("--csv", type=Path, metavar="FILE", help="CSV with Hostname,IP (and optional columns)")
    parser.add_argument("--from-bcm", action="store_true", help="Select switches from BCM (device;list -t switch)")
    parser.add_argument("--username", type=str, default=None, help="SSH username (default: cumulus)")
    parser.add_argument("--password", type=str, default=None, help="SSH password (uses env var if not provided)")
    parser.add_argument("--non-interactive", action="store_true", help="Run without prompts (safe defaults)")
    parser.add_argument("--update-config", action="store_true", help="Compare and optionally update staged configs")
    parser.add_argument("--stage-image", action="store_true", help="Stage image + set ztpsettings image (still leaves checkimageonboot=no)")
    parser.add_argument("--image", type=Path, default=None, help="Image path to stage (used with --stage-image)")
    args = parser.parse_args()

    if not shutil.which("cmsh"):
        print("Error: cmsh not found. Run this on a BCM head node.")
        return 2

    username = args.username or "cumulus"
    password = args.password or _env_password()
    if not password and not args.non_interactive:
        password = getpass.getpass(f"SSH password for {username} (also used for sudo): ")
    if password:
        _set_env_password(password)

    # Select devices
    devices: List[Dict] = []
    if args.csv:
        devices = _read_devices_from_csv(args.csv)
    elif args.from_bcm:
        devices = _get_bcm_switches()
    else:
        if args.non_interactive:
            print("Error: non-interactive mode requires --csv or --from-bcm")
            return 2
        ip_input = input("Enter switch IPs (single/range/comma-separated): ").strip()
        ips = IPAddressParser.parse(ip_input)
        if not ips:
            print("No valid IPs provided.")
            return 2
        # Resolve hostname from BCM if present; else from SSH 'hostname'
        bcm_by_ip = {d["ip"]: d for d in _get_bcm_switches()}
        for ip in ips:
            hn = bcm_by_ip.get(ip, {}).get("hostname", "")
            if not hn:
                rc, out, err = _run_ssh_command(ip, username=username, password=password, command="hostname", timeout=30)
                if rc == 0:
                    hn = out.strip()
                else:
                    print(f"Warning: could not determine hostname for {ip}: {(err or out).strip()}")
            devices.append({"hostname": hn, "ip": ip})

    # Filter invalid entries
    devices = [d for d in devices if d.get("ip")]
    if not devices:
        print("No devices selected.")
        return 2

    print("\n" + "=" * 70)
    print("ZTP STAGING")
    print("=" * 70)
    print(f"Devices: {len(devices)}")

    # Update policy
    update_policy = "changed"
    if args.update_config and not args.non_interactive:
        print("\nUpdate mode enabled. Choose update behavior:")
        print("  1) Update configurations that have changed (recommended)")
        print("  2) Update all configurations (even if unchanged)")
        choice = input("Select (1/2) [1]: ").strip()
        if choice == "2":
            update_policy = "all"
    elif args.update_config and args.non_interactive:
        update_policy = "changed"

    # Image staging: only when explicitly requested via --stage-image.
    do_stage_image = args.stage_image
    image_path: Optional[Path] = args.image

    image_dir: Optional[Path] = None
    staged_image_filename: Optional[str] = None
    if do_stage_image:
        image_dir = _detect_image_dir()
        candidates: List[Path] = []
        candidates += sorted(FILES_DIR.glob("cumulus-*.bin"))
        candidates += sorted(image_dir.glob("cumulus-*.bin"))
        if image_path is None:
            if args.non_interactive:
                print("Error: --stage-image requires --image in non-interactive mode")
                return 2
            image_path = _pick_image_interactive(candidates)
        image_path = image_path.expanduser().resolve()
        if not image_path.exists():
            print(f"Error: image not found: {image_path}")
            return 2
        staged_image_filename = image_path.name
        dest = image_dir / staged_image_filename
        if dest.exists():
            print(f"\nImage already present in BCM image dir: {dest}")
        else:
            print(f"\nStaging image into BCM image dir: {dest}")
            shutil.copy2(image_path, dest)

    # Per-device staging
    failures: List[str] = []
    for i, dev in enumerate(devices, 1):
        hostname = dev.get("hostname") or "(unknown)"
        ip = dev["ip"]
        print(f"\n[{i}/{len(devices)}] {hostname} ({ip})")

        changed, msg = _stage_startup_yaml(
            device=dev,
            username=username,
            password=password,
            update_policy=("all" if not args.update_config else update_policy),
        )
        print("  - " + msg)

        if not dev.get("hostname"):
            failures.append(ip)
            continue

        try:
            _cmsh_set_file_mode(dev["hostname"])
            props = BCMProps()
            print(f"  - BCM: set {props.config_mode}=file, {props.config_file}=startup.yaml")
            _cmsh_initialize(dev["hostname"])
            print("  - BCM: initialize complete")
        except Exception as e:
            print(f"  - ERROR: BCM config/initialize failed: {e}")
            failures.append(dev["hostname"])
            continue

        if do_stage_image and staged_image_filename:
            try:
                _cmsh_set_image(dev["hostname"], staged_image_filename)
                print(f"  - BCM: ztpsettings image={staged_image_filename} (checkimageonboot=no)")
                _cmsh_initialize(dev["hostname"])
                print("  - BCM: initialize complete (post-image)")
            except Exception as e:
                print(f"  - ERROR: BCM image staging failed: {e}")
                failures.append(dev["hostname"])
                continue

        # Summarize URLs from generated script
        ztp_script = BCM_SWITCH_HTDOCS_DIR / dev["hostname"] / "cumulus-ztp.sh"
        vars_ = _parse_vars_from_ztp_script(ztp_script)
        if vars_.get("CMD_ZTP_URL"):
            print(f"  - ZTP URL: {vars_['CMD_ZTP_URL']}")
        if vars_.get("CMD_NV_CONFIG"):
            print(f"  - Config URL: {vars_['CMD_NV_CONFIG']}")
        else:
            # Even if CMD_NV_CONFIG isn't present for some reason, the file is staged in htdocs.
            print(f"  - Config staged: {BCM_SWITCH_HTDOCS_DIR/dev['hostname']/ 'startup.yaml'}")
        if vars_.get("CMD_IMAGE_URL"):
            print(f"  - Image URL: {vars_['CMD_IMAGE_URL']}")
        elif do_stage_image:
            print("  - Note: CMD_IMAGE_URL not present in cumulus-ztp.sh (typically appears when checkimageonboot=YES)")

    print("\n" + "=" * 70)
    print("STAGING SUMMARY")
    print("=" * 70)
    if failures:
        print(f"Failed: {len(failures)} ({', '.join(failures)})")
    else:
        print("All devices staged successfully.")

    print("\nManual enablement guidance (customer action when ready):")
    print("- Configure DHCP ZTP URL (e.g., option 239) to point to each switch's cumulus-ztp.sh")
    print("- Ensure the switch will run ZTP (factory reset / ZTP enabled / boot-time ZTP as appropriate)")
    if do_stage_image:
        print("- Optional: when ready to enforce image upgrades, set checkimageonboot=yes in BCM (manual step)")

    if not failures:
        _maybe_delete_config_file(non_interactive=args.non_interactive)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())


