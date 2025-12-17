#!/usr/bin/env python3
"""
NVIDIA Air Simulation Setup Helper
---------------------------------

This script is intended for use in an NVIDIA Air lab environment to prepare
switches before running deployments and/or preparing airgapped artifacts.

It performs:
1) Preflight: if oob-mgmt-switch swp0 appears to be routed (DHCP lease taken),
   configure it to bridge swp0-50 via NVUE.
2) Generate CSV from DHCP leases (csv-from-dhcp.py)
3) Map hostnames from a topology JSON (map-csv-topology.py)
4) Change switch defaults (change-switch-defaults.py): password + hostname + disable ZTP

Notes:
- This does NOT reset the Air simulation.
- This assumes DHCP leases live at /var/lib/dhcpd/dhcpd.leases (typical on BCM/Air labs).
"""

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
DEFAULT_TOPOLOGY = REPO_DIR / "scripts" / "tests" / "sample-configs" / "test-topology.json"
DEFAULT_LEASES = Path("/var/lib/dhcpd/dhcpd.leases")
DEFAULT_CSV = REPO_DIR / ".configs" / "from-dhcp.csv"


def run(cmd: str, timeout: int = 600) -> int:
    proc = subprocess.run(cmd, shell=True, cwd=str(REPO_DIR), timeout=timeout)
    return proc.returncode


def _normalize_mac(mac: str) -> str:
    return (mac or "").strip().lower()


def parse_topology_oob_swp0_mac(topology_path: Path) -> Optional[str]:
    data = json.loads(topology_path.read_text())
    links = data.get("content", {}).get("links", [])
    for link in links:
        if not isinstance(link, list):
            continue
        for endpoint in link:
            if not isinstance(endpoint, dict):
                continue
            node = endpoint.get("node")
            iface = endpoint.get("interface")
            mac = endpoint.get("mac")
            if node == "oob-mgmt-switch" and iface == "swp0" and mac:
                return _normalize_mac(mac)
    return None


def parse_dhcpd_leases_for_mac(leases_path: Path, mac: str) -> Optional[str]:
    """
    Return the last ACTIVE lease IP for a given MAC, if present.
    """
    if not leases_path.exists():
        return None
    want = _normalize_mac(mac)
    current_ip = None
    current_mac = None
    current_state = None
    ip_for_mac: Optional[str] = None

    for raw in leases_path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("lease ") and line.endswith("{"):
            parts = line.split()
            current_ip = parts[1] if len(parts) >= 2 else None
            current_mac = None
            current_state = None
            continue
        if current_ip is None:
            continue
        if line.startswith("hardware ethernet"):
            parts = line.replace(";", "").split()
            if len(parts) >= 3:
                current_mac = _normalize_mac(parts[2])
            continue
        if line.startswith("binding state"):
            parts = line.replace(";", "").split()
            if len(parts) >= 3:
                current_state = parts[2].lower()
            continue
        if line == "}":
            if current_ip and current_mac and current_state == "active" and current_mac == want:
                ip_for_mac = current_ip
            current_ip = None
            current_mac = None
            current_state = None
            continue

    return ip_for_mac


def ping(ip: str) -> bool:
    try:
        res = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception:
        return False


def configure_oob_bridge(ip: str, password: str, username: str = "cumulus") -> bool:
    """
    Configure oob-mgmt-switch swp0-50 as bridged ports using NVUE.
    Tries direct nv commands; if permission fails, retries via sudo -S.
    """
    cmds = [
        "nv set interface swp0-50 bridge domain br_default",
        "nv config apply -y",
    ]
    for c in cmds:
        base = f"sshpass -p {shlex.quote(password)} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 {username}@{ip} {shlex.quote(c)}"
        r = subprocess.run(base, shell=True, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            continue
        sudo_cmd = f"echo {shlex.quote(password)} | sudo -S {c}"
        sudo = f"sshpass -p {shlex.quote(password)} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 {username}@{ip} {shlex.quote(sudo_cmd)}"
        r2 = subprocess.run(sudo, shell=True, capture_output=True, text=True, timeout=120)
        if r2.returncode != 0:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare NVIDIA Air lab switches for deployment")
    ap.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY, help="Topology JSON (default: test topology)")
    ap.add_argument("--leases", type=Path, default=DEFAULT_LEASES, help="dhcpd.leases path")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV path to generate/update")
    ap.add_argument("--password", type=str, default="Nvidia1234!", help="New switch password to set")
    ap.add_argument("--oob-username", type=str, default="cumulus", help="oob-mgmt-switch username")
    ap.add_argument("--oob-password", type=str, default="cumulus", help="oob-mgmt-switch current password (default: cumulus)")
    ap.add_argument("--skip-oob", action="store_true", help="Skip oob-mgmt-switch preflight")
    args = ap.parse_args()

    print("=" * 70)
    print("AIR SIM SETUP")
    print("=" * 70)

    if not args.topology.exists():
        print(f"Error: topology not found: {args.topology}")
        return 1

    # 1) Preflight oob-mgmt-switch
    if not args.skip_oob:
        print("\n[1/4] Preflight oob-mgmt-switch bridging...")
        oob_mac = parse_topology_oob_swp0_mac(args.topology)
        if not oob_mac:
            print("  - Could not find oob-mgmt-switch swp0 MAC in topology; skipping preflight.")
        else:
            oob_ip = parse_dhcpd_leases_for_mac(args.leases, oob_mac)
            if not oob_ip:
                print("  - No DHCP lease for oob-mgmt-switch swp0 (ok).")
            else:
                print(f"  - Found oob-mgmt-switch swp0 lease: {oob_ip}")
                if not ping(oob_ip):
                    print("  - Ping failed; cannot apply NVUE bridge config.")
                    return 1
                ok = configure_oob_bridge(oob_ip, args.oob_password, username=args.oob_username)
                if not ok:
                    print("  - Failed to configure oob-mgmt-switch bridging (nv/sudo).")
                    return 1
                print("  ✓ oob-mgmt-switch bridging configured/applied")
    else:
        print("\n[1/4] Preflight oob-mgmt-switch bridging... (skipped)")

    # 2) csv-from-dhcp
    print("\n[2/4] Generating CSV from DHCP leases...")
    rc = run(f"./scripts/csv-from-dhcp.py --output {shlex.quote(str(args.csv))}", timeout=120)
    if rc != 0:
        print("  ✗ csv-from-dhcp.py failed")
        return rc
    print(f"  ✓ CSV generated at {args.csv}")

    # 3) map-csv-topology
    print("\n[3/4] Mapping hostnames from topology...")
    rc = run(
        f"./scripts/map-csv-topology.py --csv {shlex.quote(str(args.csv))} --topology {shlex.quote(str(args.topology))}",
        timeout=120,
    )
    if rc != 0:
        print("  ✗ map-csv-topology.py failed")
        return rc
    print("  ✓ Hostnames mapped")

    # 4) change-switch-defaults
    print("\n[4/4] Changing switch defaults (password + hostname + disable ZTP)...")
    rc = run(
        f"./scripts/change-switch-defaults.py --csv {shlex.quote(str(args.csv))} --new-password {shlex.quote(args.password)}",
        timeout=600,
    )
    if rc != 0:
        print("  ✗ change-switch-defaults.py failed")
        return rc
    print("  ✓ Switch defaults updated")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


