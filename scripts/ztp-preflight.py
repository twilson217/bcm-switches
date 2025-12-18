#!/usr/bin/env python3
"""
ZTP preflight checks for BCM/Cumulus ZTP readiness.

This is intentionally non-disruptive: it checks BCM-side staging and generated
artifacts (htdocs + cmsh settings) and does not SSH into switches by default.
"""

import argparse
import csv
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
FILES_DIR = REPO_ROOT / ".files"

BCM_SWITCH_HTDOCS_DIR = Path("/cm/local/apps/cmd/etc/htdocs/switch")
BCM_SWITCH_IMAGE_DIR_CANDIDATES = [
    Path("/cm/local/apps/cmd/etc/htdocs/switch/image"),
    Path("/cm/local/apps/cmd/etc/htdocs/switch/images"),
]


def _run_cmsh(cmsh_cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cmsh", "-c", cmsh_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_devices_from_csv(csv_path: Path) -> List[Dict]:
    if not csv_path.exists():
        raise FileNotFoundError(str(csv_path))
    devices: List[Dict] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            hostname = norm.get("hostname") or ""
            ip = norm.get("ip") or ""
            if not hostname and not ip:
                continue
            devices.append({"hostname": hostname, "ip": ip})
    return devices


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
        if len(parts) < 5 or parts[0] != "Switch":
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


def _get_bcm_switches() -> List[Dict]:
    r = subprocess.run(["cmsh", "-c", "device;list -t switch"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "cmsh device list failed")
    return _parse_bcm_switch_list(r.stdout)


def _detect_image_dir() -> Optional[Path]:
    for p in BCM_SWITCH_IMAGE_DIR_CANDIDATES:
        if p.exists() and p.is_dir():
            return p
    return None


def _cmsh_get(hostname: str, field: str) -> str:
    r = _run_cmsh(f"device; use {hostname}; get {field}")
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _cmsh_ztp_get(hostname: str, field: str) -> str:
    r = _run_cmsh(f"device; use {hostname}; ztpsettings; get {field}")
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _parse_vars_from_ztp_script(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    txt = path.read_text(errors="replace")
    for m in re.finditer(r"^([A-Z0-9_]+)='([^']*)'\s*$", txt, flags=re.MULTILINE):
        out[m.group(1)] = m.group(2)
    return out


def _norm_cmsh_val(v: str) -> str:
    return (v or "").strip().strip('"').strip("'").lower()


def _is_truthy(v: str) -> bool:
    return _norm_cmsh_val(v) in {"yes", "true", "1", "enabled", "on"}


def _report_section(title: str) -> None:
    print("\n" + "-" * 70)
    print(title)
    print("-" * 70)


def _config_checks(dev: Dict) -> Tuple[bool, List[str]]:
    hostname = dev.get("hostname") or ""
    issues: List[str] = []
    if not hostname:
        issues.append("Missing hostname (required for BCM device checks).")
        return (False, issues)

    cmode = _norm_cmsh_val(_cmsh_get(hostname, "cumulusmode"))
    cfile = _norm_cmsh_val(_cmsh_get(hostname, "cumulusfile"))
    if cmode != "file":
        issues.append(f"BCM cumulusmode is '{cmode or '(empty)'}' (expected 'file').")
    if cfile != "startup.yaml":
        issues.append(f"BCM cumulusfile is '{cfile or '(empty)'}' (expected 'startup.yaml').")

    staged = BCM_SWITCH_HTDOCS_DIR / hostname / "startup.yaml"
    if not staged.exists():
        issues.append(f"Missing staged config: {staged}")

    ztp_script = BCM_SWITCH_HTDOCS_DIR / hostname / "cumulus-ztp.sh"
    if not ztp_script.exists():
        issues.append(f"Missing ZTP script: {ztp_script} (run `cmsh -c \"device; use {hostname}; initialize\"`)")
    else:
        vars_ = _parse_vars_from_ztp_script(ztp_script)
        nv_cfg = vars_.get("CMD_NV_CONFIG", "")
        if not nv_cfg:
            issues.append("ZTP script does not contain CMD_NV_CONFIG (BCM may not be set to serve config file).")

    return (len(issues) == 0, issues)


def _image_checks(dev: Dict, image_dir: Optional[Path]) -> Tuple[bool, List[str]]:
    hostname = dev.get("hostname") or ""
    issues: List[str] = []
    if not hostname:
        issues.append("Missing hostname (required for BCM device checks).")
        return (False, issues)

    img = (_cmsh_ztp_get(hostname, "image") or "").strip().strip('"').strip("'")
    check_on_boot = _cmsh_ztp_get(hostname, "checkimageonboot")

    if not img:
        issues.append("BCM ztpsettings image is not set.")
        # If image mgmt is not configured, preflight should fail only for image-only mode.
        return (False, issues)

    if image_dir is None:
        issues.append("Could not find BCM image directory (expected /cm/local/apps/cmd/etc/htdocs/switch/image/).")
    else:
        img_path = image_dir / Path(img).name
        if not img_path.exists():
            issues.append(f"Image file missing in BCM image dir: {img_path}")

    # We expect checkimageonboot to remain NO unless the user intentionally enabled it.
    if _is_truthy(check_on_boot):
        # If enabled, make sure CMD_IMAGE_URL is present and points under /switch/image/
        ztp_script = BCM_SWITCH_HTDOCS_DIR / hostname / "cumulus-ztp.sh"
        vars_ = _parse_vars_from_ztp_script(ztp_script)
        img_url = vars_.get("CMD_IMAGE_URL", "")
        if not img_url:
            issues.append("checkimageonboot is YES but CMD_IMAGE_URL is not present in cumulus-ztp.sh (run initialize).")
        elif "/switch/image/" not in img_url:
            issues.append(f"CMD_IMAGE_URL does not look like /switch/image/... : {img_url}")
    else:
        # Not an error: staging-only behavior. But helpful guidance:
        pass

    return (len(issues) == 0, issues)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight BCM/Cumulus ZTP readiness (non-disruptive)")
    parser.add_argument("--csv", type=Path, metavar="FILE", help="CSV with Hostname,IP (and optional columns)")
    parser.add_argument("--from-bcm", action="store_true", help="Select switches from BCM (device;list -t switch)")
    parser.add_argument("--config-only", action="store_true", help="Run only config-management checks")
    parser.add_argument("--image-only", action="store_true", help="Run only image-management checks")
    args = parser.parse_args()

    if not shutil.which("cmsh"):
        print("Error: cmsh not found. Run this on a BCM head node.")
        return 2

    if args.config_only and args.image_only:
        print("Error: --config-only and --image-only are mutually exclusive")
        return 2

    run_config = not args.image_only
    run_image = not args.config_only

    if args.csv:
        devices = _read_devices_from_csv(args.csv)
    elif args.from_bcm:
        devices = _get_bcm_switches()
    else:
        # Default: all BCM switches
        devices = _get_bcm_switches()

    devices = [d for d in devices if d.get("hostname")]
    if not devices:
        print("No devices selected.")
        return 2

    image_dir = _detect_image_dir()

    print("\n" + "=" * 70)
    print("ZTP PREFLIGHT")
    print("=" * 70)
    print(f"Devices: {len(devices)}")
    if image_dir:
        print(f"BCM image dir: {image_dir}")
    else:
        print("BCM image dir: (not found)")

    overall_ok = True

    if run_config:
        _report_section("Config-management preflight")
        ok_count = 0
        for dev in devices:
            hostname = dev["hostname"]
            ok, issues = _config_checks(dev)
            if ok:
                ok_count += 1
                print(f"  ✓ {hostname}")
            else:
                overall_ok = False
                print(f"  ✗ {hostname}")
                for it in issues:
                    print(f"      - {it}")
        print(f"\nConfig-management: {ok_count}/{len(devices)} PASS")

    if run_image:
        _report_section("Image-management preflight")
        ok_count = 0
        for dev in devices:
            hostname = dev["hostname"]
            ok, issues = _image_checks(dev, image_dir)
            if ok:
                ok_count += 1
                print(f"  ✓ {hostname}")
            else:
                overall_ok = False
                print(f"  ✗ {hostname}")
                for it in issues:
                    print(f"      - {it}")
                # If checkimageonboot is off, provide the intended staged-only guidance
                check_on_boot = _cmsh_ztp_get(hostname, "checkimageonboot")
                if not _is_truthy(check_on_boot):
                    print("      - Note: checkimageonboot is NO (staging-only). CMD_IMAGE_URL may not appear until enabled + initialize.")
        print(f"\nImage-management: {ok_count}/{len(devices)} PASS")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


