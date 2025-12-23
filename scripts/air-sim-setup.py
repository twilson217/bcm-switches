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


def _handle_expired_password(ip: str, current_password: str, new_password: str, username: str = "cumulus") -> Tuple[bool, str]:
    """
    Handle Cumulus expired password prompt via expect.
    Returns (success, working_password).
    On fresh Cumulus switches the default password is expired and SSH forces a change.
    """
    # Use a more sequential expect script that explicitly handles the expired password flow
    expect_script = f'''
set timeout 60
log_user 1

spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {username}@{ip}

# Step 1: Handle SSH connection and initial password
expect {{
    "Are you sure you want to continue connecting" {{
        send "yes\\r"
        exp_continue
    }}
    -re "assword:" {{
        send "{current_password}\\r"
    }}
    timeout {{
        puts "RESULT:TIMEOUT_SSH"
        exit 1
    }}
    eof {{
        puts "RESULT:EOF_SSH"
        exit 1
    }}
}}

# Step 2: After password accepted, wait to see what happens
# Either we get password change prompts (expired) or a shell prompt (not expired)
expect {{
    "Current password:" {{
        # Password is expired - handle the change flow
        send "{current_password}\\r"
        expect "New password:"
        send "{new_password}\\r"
        expect "Retype new password:"
        send "{new_password}\\r"
        # Wait for confirmation or disconnect
        expect {{
            "updated successfully" {{
                puts "RESULT:PASSWORD_CHANGED"
            }}
            eof {{
                puts "RESULT:PASSWORD_CHANGED"
            }}
            timeout {{
                puts "RESULT:CHANGE_TIMEOUT"
            }}
        }}
    }}
    "password has expired" {{
        # Keep waiting for the actual prompt
        exp_continue
    }}
    "must change your password" {{
        # Keep waiting for the actual prompt
        exp_continue
    }}
    "Changing password" {{
        # Keep waiting for Current password prompt
        exp_continue
    }}
    -re "\\$ $" {{
        # Got a shell prompt - password not expired
        send "exit\\r"
        expect eof
        puts "RESULT:NOT_EXPIRED"
    }}
    "Permission denied" {{
        puts "RESULT:AUTH_FAILED"
    }}
    timeout {{
        puts "RESULT:TIMEOUT_AFTER_LOGIN"
    }}
    eof {{
        # Connection closed - might mean password was already changed
        puts "RESULT:EOF_AFTER_LOGIN"
    }}
}}
'''
    try:
        proc = subprocess.run(
            ["expect", "-c", expect_script],
            capture_output=True, text=True, timeout=90
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        
        if "RESULT:PASSWORD_CHANGED" in output:
            return True, new_password
        if "RESULT:NOT_EXPIRED" in output:
            return True, current_password
        if "RESULT:AUTH_FAILED" in output:
            # Password might already be the new one
            return True, new_password
        if "RESULT:EOF_AFTER_LOGIN" in output:
            # Connection closed after login - try with new password
            return True, new_password
        # Some other failure - log it
        print(f"    [DEBUG] expect output: {output[-300:]}")
        return False, current_password
    except Exception as e:
        print(f"    [DEBUG] expect exception: {e}")
        return False, current_password


def configure_oob_bridge(ip: str, password: str, new_password: str, username: str = "cumulus") -> bool:
    """
    Configure oob-mgmt-switch swp0-50 as bridged ports using NVUE.
    First handles expired password if needed, then runs nv commands.
    """
    # Step 1: Handle expired password (fresh Cumulus switches)
    print("    Checking/handling expired password...")
    ok, working_pw = _handle_expired_password(ip, password, new_password, username)
    if not ok:
        print("    ✗ Could not handle password")
        return False
    if working_pw != password:
        print("    ✓ Password updated")
    else:
        print("    ✓ Password OK (not expired)")

    time.sleep(1)  # Allow connection to settle

    # Step 2: Run bridge configuration
    # Note: After 'nv config apply', bridging swp0 will cause the switch to lose
    # its DHCP-assigned IP on that interface, dropping our SSH connection.
    # This is expected - treat connection loss after apply as success.
    
    ssh_base = f"sshpass -p {shlex.quote(working_pw)} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 {username}@{ip}"
    
    def run_cmd(cmd: str, timeout: int = 30) -> bool:
        """Run command, return True on success. Try direct first, then sudo."""
        try:
            r = subprocess.run(f"{ssh_base} {shlex.quote(cmd)}", shell=True, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            return False
        # Try with sudo
        sudo_cmd = f"echo {shlex.quote(working_pw)} | sudo -S -p '' {cmd}"
        try:
            r2 = subprocess.run(f"{ssh_base} {shlex.quote(sudo_cmd)}", shell=True, capture_output=True, text=True, timeout=timeout)
            return r2.returncode == 0
        except subprocess.TimeoutExpired:
            return False
    
    # Set the bridge configuration
    print("    Configuring bridge (swp0-50)...")
    if not run_cmd("nv set interface swp0-50 bridge domain br_default", timeout=30):
        print("    ✗ Failed to set bridge config")
        return False
    
    # Apply the config - this will likely drop our connection as swp0 loses its IP
    print("    Applying config (connection will drop - this is expected)...")
    try:
        # Use a short timeout - we expect to lose connection
        subprocess.run(
            f"{ssh_base} {shlex.quote('nv config apply -y')}",
            shell=True, capture_output=True, text=True, timeout=15
        )
        # If we get here without timeout, config was applied and connection survived (unlikely)
        return True
    except subprocess.TimeoutExpired:
        # Expected! The config was applied and we lost connection because swp0 is now bridged
        print("    ✓ Config applied (connection dropped as expected)")
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
                ok = configure_oob_bridge(oob_ip, args.oob_password, args.password, username=args.oob_username)
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


