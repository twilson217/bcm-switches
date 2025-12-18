#!/usr/bin/env python3
"""
ZTP preflight checklist for BCM/Cumulus ZTP readiness.

Purpose:
- Provide an operator-facing checklist *before* enabling ZTP for DR/RMA/cutover.
- Source-of-truth is BCM (cmsh + BCM htdocs artifacts). If devices are not in BCM,
  there is no reason to run this script.

This script prints every check it performs. It also prints explicit TODO items
for manual steps that are intentionally left undone by ztp-staging.py (for safety).
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

DHCPD_CONF_DIR = Path("/etc")


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


def _cmsh_get_network(hostname: str) -> str:
    return (_cmsh_get(hostname, "network") or "").strip().strip('"').strip("'")


def _cmsh_get_mac(hostname: str) -> str:
    return (_cmsh_get(hostname, "mac") or "").strip().strip('"').strip("'").upper()


def _cmsh_get_ip(hostname: str) -> str:
    return (_cmsh_get(hostname, "ip") or "").strip().strip('"').strip("'")


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


def _check(label: str, ok: bool, details: str = "") -> Tuple[bool, str]:
    prefix = "✅" if ok else "⚠️"
    if details:
        return ok, f"{prefix} {label}: {details}"
    return ok, f"{prefix} {label}"


def _missing(label: str, details: str = "") -> Tuple[bool, str]:
    prefix = "❌"
    if details:
        return False, f"{prefix} {label}: {details}"
    return False, f"{prefix} {label}"


def _detect_dhcpd_conf_for_network(network: str) -> Optional[Path]:
    """
    BCM commonly generates per-network ISC dhcpd config files like:
      /etc/dhcpd.<network>.conf
    """
    if not network:
        return None
    candidates = [
        DHCPD_CONF_DIR / f"dhcpd.{network}.conf",
        DHCPD_CONF_DIR / f"dhcpd.{network}.include.conf",
        DHCPD_CONF_DIR / f"dhcpd.{network}.include",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _dhcpd_service_active() -> Tuple[bool, str]:
    """
    Best-effort check for a BCM-managed ISC DHCP service.
    Names vary across distros; treat any active candidate as OK.
    """
    candidates = ["dhcpd", "isc-dhcp-server"]
    for svc in candidates:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if (r.stdout or "").strip() == "active":
                return True, svc
        except Exception:
            continue
    return False, "dhcpd/isc-dhcp-server"


def _parse_dhcp_host_block(conf_text: str, hostname: str) -> Optional[str]:
    """
    Extract the host block for a hostname from ISC dhcpd config.
    Minimal parser: finds `host <hostname> {` and returns up to its matching `}`.
    """
    pat = re.compile(rf"(^\s*host\s+{re.escape(hostname)}\s*\{{)", re.MULTILINE)
    m = pat.search(conf_text)
    if not m:
        return None
    start = m.start(1)
    depth = 0
    for i in range(start, len(conf_text)):
        ch = conf_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return conf_text[start : i + 1]
    return None


def _dhcp_option_value(block: str, option_name: str) -> Optional[str]:
    """
    Extract `option <option_name> "<value>";` from a host block.
    """
    m = re.search(rf'option\s+{re.escape(option_name)}\s+"([^"]+)";', block)
    if not m:
        return None
    return m.group(1)


def _config_checks(dev: Dict) -> Tuple[bool, List[str], List[str]]:
    hostname = dev.get("hostname") or ""
    lines: List[str] = []
    manual: List[str] = []
    if not hostname:
        lines.append(_missing("Device hostname", "missing hostname (cannot query BCM device object)")[1])
        return (False, lines, manual)

    cmode = _norm_cmsh_val(_cmsh_get(hostname, "cumulusmode"))
    cfile = _norm_cmsh_val(_cmsh_get(hostname, "cumulusfile"))
    ok, msg = _check("BCM device.cumulusmode == FILE", cmode == "file", f"current='{cmode or '(empty)'}'")
    lines.append(msg if ok else _missing("BCM device.cumulusmode", f"current='{cmode or '(empty)'}', expected 'file'")[1])
    ok, msg = _check("BCM device.cumulusfile == startup.yaml", cfile == "startup.yaml", f"current='{cfile or '(empty)'}'")
    lines.append(msg if ok else _missing("BCM device.cumulusfile", f"current='{cfile or '(empty)'}', expected 'startup.yaml'")[1])

    staged = BCM_SWITCH_HTDOCS_DIR / hostname / "startup.yaml"
    if staged.exists():
        lines.append(_check("BCM staged config file exists", True, str(staged))[1])
    else:
        lines.append(_missing("BCM staged config file exists", str(staged))[1])

    ztp_script = BCM_SWITCH_HTDOCS_DIR / hostname / "cumulus-ztp.sh"
    if not ztp_script.exists():
        lines.append(
            _missing(
                "BCM generated ZTP script exists",
                f"{ztp_script} (run: cmsh -c \"device; use {hostname}; initialize\")",
            )[1]
        )
    else:
        lines.append(_check("BCM generated ZTP script exists", True, str(ztp_script))[1])
        vars_ = _parse_vars_from_ztp_script(ztp_script)

        ztp_url = vars_.get("CMD_ZTP_URL", "")
        if ztp_url:
            lines.append(_check("ZTP script contains CMD_ZTP_URL", True, ztp_url)[1])
        else:
            lines.append(_missing("ZTP script contains CMD_ZTP_URL", "CMD_ZTP_URL not found in script")[1])

        nv_cfg = vars_.get("CMD_NV_CONFIG", "")
        if nv_cfg:
            lines.append(_check("ZTP script contains CMD_NV_CONFIG", True, nv_cfg)[1])
        else:
            lines.append(_missing("ZTP script contains CMD_NV_CONFIG", "CMD_NV_CONFIG not found in script")[1])

    # BCM-managed DHCP checks (assume BCM is the DHCP server for these devices).
    net = _cmsh_get_network(hostname)
    mac = _cmsh_get_mac(hostname)
    ip = _cmsh_get_ip(hostname)

    if net:
        lines.append(_check("BCM device.network is set", True, net)[1])
    else:
        lines.append(_missing("BCM device.network is set", "network is empty/unset")[1])

    svc_ok, svc_name = _dhcpd_service_active()
    if svc_ok:
        lines.append(_check("BCM DHCP service is active", True, svc_name)[1])
    else:
        lines.append(_missing("BCM DHCP service is active", svc_name)[1])

    conf_path = _detect_dhcpd_conf_for_network(net)
    if conf_path and conf_path.exists():
        lines.append(_check("BCM DHCP config file exists for network", True, str(conf_path))[1])
        try:
            conf_txt = conf_path.read_text(errors="replace")
            block = _parse_dhcp_host_block(conf_txt, hostname)
            if not block:
                lines.append(_missing("BCM DHCP has host block for switch", f"host {hostname} not found in {conf_path}")[1])
            else:
                lines.append(_check("BCM DHCP has host block for switch", True, f"host {hostname}")[1])
                if mac:
                    if mac.lower() in block.lower():
                        lines.append(_check("BCM DHCP host block contains switch MAC", True, mac)[1])
                    else:
                        lines.append(_missing("BCM DHCP host block contains switch MAC", mac)[1])
                if ip:
                    if ip in block:
                        lines.append(_check("BCM DHCP host block contains fixed-address", True, ip)[1])
                    else:
                        lines.append(_missing("BCM DHCP host block contains fixed-address", ip)[1])

                # Validate cumulus-provision-url points to the ZTP script URL (if available)
                expected_ztp_url = ""
                if "vars_" in locals():
                    expected_ztp_url = vars_.get("CMD_ZTP_URL", "")
                prov = _dhcp_option_value(block, "cumulus-provision-url")
                if prov:
                    if expected_ztp_url and prov != expected_ztp_url:
                        lines.append(
                            _missing(
                                "BCM DHCP option cumulus-provision-url matches ZTP URL",
                                f"dhcp='{prov}', ztp-script='{expected_ztp_url}'",
                            )[1]
                        )
                    else:
                        lines.append(_check("BCM DHCP option cumulus-provision-url is set", True, prov)[1])
                else:
                    lines.append(_missing("BCM DHCP option cumulus-provision-url is set", "option not found in host block")[1])
        except Exception as e:
            lines.append(_missing("Read/parse BCM DHCP config", str(e))[1])
    else:
        lines.append(_missing("BCM DHCP config file exists for network", f"no dhcpd.<network>.conf found for network '{net}'")[1])

    # Operator decision, but BCM-sourced: runztponeachboot.
    run_each_boot = _cmsh_ztp_get(hostname, "runztponeachboot")
    run_norm = _norm_cmsh_val(run_each_boot) or "(empty)"
    lines.append(_check("BCM ztpsettings.runztponeachboot is set (operator decision)", run_norm in {"yes", "no"}, f"current='{run_norm}'")[1])

    # Intentionally not listing switch-side/cutover items here (per request: BCM-only checks).

    ok_all = all(not line.startswith("[MISSING]") for line in lines)
    return (ok_all, lines, manual)


def _image_checks(dev: Dict, image_dir: Optional[Path]) -> Tuple[bool, List[str], List[str]]:
    hostname = dev.get("hostname") or ""
    lines: List[str] = []
    manual: List[str] = []
    if not hostname:
        lines.append(_missing("Device hostname", "missing hostname (cannot query BCM device object)")[1])
        return (False, lines, manual)

    img = (_cmsh_ztp_get(hostname, "image") or "").strip().strip('"').strip("'")
    check_on_boot = _cmsh_ztp_get(hostname, "checkimageonboot")

    if img:
        lines.append(_check("BCM ztpsettings.image is set", True, img)[1])
    else:
        lines.append(_missing("BCM ztpsettings.image is set", "image is empty/unset")[1])

    if image_dir is None:
        lines.append(
            _missing(
                "BCM image directory exists",
                "expected /cm/local/apps/cmd/etc/htdocs/switch/image/ (or .../images/)",
            )[1]
        )
    else:
        lines.append(_check("BCM image directory exists", True, str(image_dir))[1])
        if img:
            img_path = image_dir / Path(img).name
            if img_path.exists():
                lines.append(_check("BCM image file exists in image directory", True, str(img_path))[1])
            else:
                lines.append(_missing("BCM image file exists in image directory", str(img_path))[1])

    # Image enforcement steps (BCM-verifiable):
    # - checkimageonboot should be explicitly enabled when ready
    # - cumulus-ztp.sh should contain CMD_IMAGE_URL after enable + initialize
    check_norm = _norm_cmsh_val(check_on_boot) or "(empty)"
    lines.append(
        _check(
            "BCM ztpsettings.checkimageonboot == yes (enable image enforcement)",
            check_norm == "yes",
            f"current='{check_norm}'",
        )[1]
    )

    ztp_script = BCM_SWITCH_HTDOCS_DIR / hostname / "cumulus-ztp.sh"
    vars_ = _parse_vars_from_ztp_script(ztp_script)
    img_url = vars_.get("CMD_IMAGE_URL", "")
    lines.append(
        _check(
            "ZTP script contains CMD_IMAGE_URL (run initialize after enabling checkimageonboot)",
            bool(img_url),
            img_url or "CMD_IMAGE_URL not present",
        )[1]
    )

    # If BCM is the DHCP server, it can also provide an image URL via DHCP (default-url).
    net = _cmsh_get_network(hostname)
    conf_path = _detect_dhcpd_conf_for_network(net)
    if img and conf_path and conf_path.exists():
        try:
            conf_txt = conf_path.read_text(errors="replace")
            block = _parse_dhcp_host_block(conf_txt, hostname)
            if block:
                default_url = _dhcp_option_value(block, "default-url")
                if default_url:
                    lines.append(_check("BCM DHCP option default-url is set (image URL)", True, default_url)[1])
                else:
                    lines.append(_missing("BCM DHCP option default-url is set (image URL)", "option not found in host block")[1])
        except Exception as e:
            lines.append(_missing("Read/parse BCM DHCP config for default-url", str(e))[1])

    # Intentionally not listing non-verifiable items here (per request: BCM-only checklist output).

    ok_all = all(not line.startswith("[MISSING]") for line in lines)
    return (ok_all, lines, manual)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight BCM/Cumulus ZTP readiness checklist (BCM is source of truth)")
    parser.add_argument("--csv", type=Path, metavar="FILE", help="Optional: limit to devices listed in CSV (must exist in BCM)")
    parser.add_argument("--switch", type=str, metavar="HOSTNAME", help="Optional: limit to a single BCM switch hostname")
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

    # Always source from BCM; optionally filter by --switch or --csv.
    bcm_devices = _get_bcm_switches()
    bcm_by_hostname = {d["hostname"]: d for d in bcm_devices if d.get("hostname")}

    devices: List[Dict] = []
    if args.switch:
        if args.switch not in bcm_by_hostname:
            print(f"Error: switch '{args.switch}' not found in BCM.")
            return 2
        devices = [bcm_by_hostname[args.switch]]
    elif args.csv:
        csv_devices = _read_devices_from_csv(args.csv)
        hostnames = [d.get("hostname") for d in csv_devices if d.get("hostname")]
        missing = [h for h in hostnames if h not in bcm_by_hostname]
        if missing:
            print("Error: the following switches from CSV were not found in BCM:")
            for h in missing:
                print(f"  - {h}")
            return 2
        devices = [bcm_by_hostname[h] for h in hostnames if h in bcm_by_hostname]
    else:
        devices = [d for d in bcm_devices if d.get("hostname")]

    if not devices:
        print("No devices selected.")
        return 2

    image_dir = _detect_image_dir()

    print("\n" + "=" * 70)
    print("ZTP PREFLIGHT (CHECKLIST)")
    print("=" * 70)
    print(f"Devices: {len(devices)}")
    if image_dir:
        print(f"BCM image dir: {image_dir}")
    else:
        print("BCM image dir: (not found)")

    overall_ok = True  # True only if all *BCM-sourced* required items are present

    if run_config:
        _report_section("Config-management preflight")
        ok_count = 0
        for dev in devices:
            hostname = dev["hostname"]
            ok, lines, manual = _config_checks(dev)
            print(f"\n{hostname}")
            ok_lines = [l for l in lines if l.startswith("✅")]
            missing_lines = [l for l in lines if l.startswith("❌")]
            todo_lines = [l for l in lines if l.startswith("⚠️")]
            other_lines = [l for l in lines if l not in ok_lines + missing_lines + todo_lines]

            # Print in a stable order: OK first, then MISSING, then TODO.
            for line in ok_lines + missing_lines + todo_lines + other_lines:
                print(f"  {line}")
            if missing_lines:
                overall_ok = False
            for line in manual:
                print(f"  {line}")
                # Manual items should keep the checklist in a TODO state by design.
            if ok:
                ok_count += 1
        print(f"\nConfig staging (BCM prerequisites): {ok_count}/{len(devices)} OK")

    if run_image:
        _report_section("Image-management preflight")
        ok_count = 0
        for dev in devices:
            hostname = dev["hostname"]
            ok, lines, manual = _image_checks(dev, image_dir)
            print(f"\n{hostname}")
            ok_lines = [l for l in lines if l.startswith("✅")]
            missing_lines = [l for l in lines if l.startswith("❌")]
            todo_lines = [l for l in lines if l.startswith("⚠️")]
            other_lines = [l for l in lines if l not in ok_lines + missing_lines + todo_lines]

            for line in ok_lines + missing_lines + todo_lines + other_lines:
                print(f"  {line}")
            if missing_lines:
                overall_ok = False
            for line in manual:
                print(f"  {line}")
            if ok:
                ok_count += 1
        print(f"\nImage staging (BCM prerequisites): {ok_count}/{len(devices)} OK")

    # Exit code:
    # - 0 only if BCM-side prerequisites are all present
    # - 1 if any required BCM artifact/setting is missing
    # Manual TODO items do not affect exit code (they are reminders, not verifiable from BCM).
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


