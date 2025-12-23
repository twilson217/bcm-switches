#!/usr/bin/env python3
"""
Automated Test Loop for BCM Switch Deployment

Runs the complete test cycle: reset → setup → deploy → validate

Test 1: Full Deployment from DHCP Leases
  - Generate CSV from DHCP leases
  - Change default passwords
  - Map hostnames from topology
  - Deploy using --csv option
  - Validate deployment

Test 2: Deployment with Switch Setup First
  - Generate CSV from DHCP leases
  - Map hostnames from topology
  - Change passwords AND set hostnames on switches
  - Deploy using --csv option (switches already have correct hostnames)
  - Validate deployment

Test 3: Install on Switches Already in BCM (--from-bcm mode)
  - Generate CSV from DHCP leases
  - Change passwords on switches
  - Map hostnames from topology
  - Add devices to BCM using --csv (phases 1-2 only)
  - Run deploy with --from-bcm to install cm-lite-daemon
  - Validate deployment

Usage:
    ./test-loop.py              # Run all tests (1, 2, and 3)
    ./test-loop.py --test1      # Run only Test 1
    ./test-loop.py --test2      # Run only Test 2
    ./test-loop.py --test3      # Run only Test 3
    ./test-loop.py --dry-run    # Show what would be done
    ./test-loop.py --no-reset   # Skip switch-only rebuild + BCM cleanup
    ./test-loop.py --verbose    # Show detailed output
"""

import argparse
import shlex
import re
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# BCM version compatibility
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bcm_compat import BCMProps, get_bcm_version, get_cmsh_cmd

# cmsh command - use full path to avoid dependency on "module load cmsh"
CMSH = get_cmsh_cmd()

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
CONFIG_DIR = REPO_DIR / ".configs"
LOGS_DIR = REPO_DIR / ".logs"
TOPOLOGY_FILE = SCRIPT_DIR / "sample-configs" / "test-topology.json"
DHCP_LEASES_FILE = Path("/var/lib/dhcpd/dhcpd.leases")
VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"

# CSV file paths
FROM_DHCP_CSV = CONFIG_DIR / "from-dhcp.csv"

# Switches we're testing with (excludes oob-mgmt-switch)
TEST_SWITCHES = ["spine-01", "spine-02", "leaf-01", "leaf-02", "leaf-03", "leaf-04"]

# Wait times
DHCP_WAIT_SECONDS = 90  # Wait for switches to get DHCP after reset
BOOT_STABILIZE_SECONDS = 30  # Extra wait for boot to stabilize

# Default test password (can be overridden via --password)
DEFAULT_TEST_PASSWORD = "Nvidia1234!"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class StepResult:
    """Result of a test step."""
    name: str
    success: bool
    duration: float
    message: str
    output: str = ""
    command: str = ""


@dataclass
class TestResult:
    """Result of a complete test."""
    name: str
    success: bool
    steps: List[StepResult] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    duration: float = 0.0


# ============================================================================
# Utility Functions
# ============================================================================

def run_cmd(cmd: str, cwd: Path = None, timeout: int = 600,
            capture: bool = True, input_text: str = None) -> Tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd or REPO_DIR,
            capture_output=capture, text=True, 
            timeout=timeout, input=input_text
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        # Preserve partial output when possible (helps debug long-running steps).
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="ignore")
        if isinstance(err, bytes):
            err = err.decode(errors="ignore")
        msg = f"Command timed out after {timeout}s"
        combined_err = (err + "\n" + msg).strip() if err else msg
        return -1, out, combined_err
    except Exception as e:
        return -1, "", str(e)


def run_script(script_path: str, args: str = "", timeout: int = 600,
               input_text: str = None, verbose: bool = False) -> StepResult:
    """Run a Python script and return the result."""
    start = time.time()
    
    # Build command
    if script_path.startswith("./"):
        script_path = script_path[2:]
    full_path = REPO_DIR / script_path
    
    # Prefer scripts/tests/.venv if present so test-loop can run non-interactively
    # without requiring the user to `source .venv/bin/activate`.
    python_exe = str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3"
    cmd = f"{python_exe} {full_path}"
    if args:
        cmd += f" {args}"
    
    if verbose:
        print(f"    Running: {cmd}")
    
    rc, stdout, stderr = run_cmd(cmd, timeout=timeout, input_text=input_text)
    duration = time.time() - start
    
    success = rc == 0
    output = stdout + stderr
    
    if verbose and output:
        for line in output.split('\n')[:20]:
            print(f"      {line}")
        if len(output.split('\n')) > 20:
            print(f"      ... ({len(output.split(chr(10))) - 20} more lines)")
    
    message = "Success" if success else f"Failed (exit code {rc})"
    
    return StepResult(
        name=script_path.split('/')[-1],
        success=success,
        duration=duration,
        message=message,
        output=output,
        command=cmd
    )


def wait_with_countdown(seconds: int, message: str):
    """Wait with a countdown display."""
    print(f"  {message}")
    for remaining in range(seconds, 0, -10):
        print(f"    Waiting... {remaining}s remaining", end="\r")
        time.sleep(min(10, remaining))
    print(f"    Waiting... done!                    ")


def filter_csv_remove_oob_switch(csv_path: Path) -> bool:
    """Remove oob-mgmt-switch from CSV file."""
    try:
        rows = []
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                hostname = row.get('Hostname', '') or row.get('hostname', '')
                if 'oob' not in hostname.lower():
                    rows.append(row)
        
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        
        return True
    except Exception as e:
        print(f"    Error filtering CSV: {e}")
        return False


def count_devices_in_csv(csv_path: Path) -> int:
    """Count devices in a CSV file."""
    try:
        with open(csv_path, 'r') as f:
            return sum(1 for _ in csv.DictReader(f))
    except:
        return 0


def clear_config():
    """Clear the config.json to start fresh."""
    config_file = CONFIG_DIR / "config.json"
    if config_file.exists():
        try:
            config_file.unlink()
            return True
        except:
            pass
    return True


def get_validation_summary(output: str) -> Tuple[int, int]:
    """Parse validation output to get passed/failed counts."""
    passed = 0
    failed = 0
    for line in output.split('\n'):
        if 'Passed:' in line and 'switches' not in line.lower():
            try:
                passed = int(line.split(':')[1].strip())
            except:
                pass
        elif 'Failed:' in line and 'switches' not in line.lower():
            try:
                failed = int(line.split(':')[1].strip())
            except:
                pass
    return passed, failed


def _normalize_mac(mac: str) -> str:
    return mac.strip().lower()


def parse_topology_macs(topology_path: Path) -> Tuple[Optional[str], Dict[str, str]]:
    """Return (oob_swp0_mac, {switch_name: eth0_mac}) from the topology JSON."""
    data = json.loads(topology_path.read_text())
    links = data.get("content", {}).get("links", [])

    oob_swp0_mac: Optional[str] = None
    switch_eth0_macs: Dict[str, str] = {}

    for link in links:
        if not isinstance(link, list):
            continue
        for endpoint in link:
            if not isinstance(endpoint, dict):
                continue
            node = endpoint.get("node")
            iface = endpoint.get("interface")
            mac = endpoint.get("mac")
            if not (node and iface and mac):
                continue

            if node == "oob-mgmt-switch" and iface == "swp0":
                oob_swp0_mac = _normalize_mac(mac)

            if node in TEST_SWITCHES and iface == "eth0":
                switch_eth0_macs[node] = _normalize_mac(mac)

    return oob_swp0_mac, switch_eth0_macs


def parse_dhcpd_leases(leases_path: Path) -> Dict[str, str]:
    """Parse dhcpd.leases and return dict of active leases: {mac: ip} (last active wins)."""
    leases: Dict[str, str] = {}
    if not leases_path.exists():
        return leases

    current_ip = None
    current_mac = None
    current_state = None

    try:
        for raw in leases_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("lease ") and line.endswith("{"):
                # lease 192.168.200.10 {
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
                if current_ip and current_mac and current_state == "active":
                    leases[current_mac] = current_ip
                current_ip = None
                current_mac = None
                current_state = None
                continue
    except Exception:
        return leases

    return leases


def ping_host(ip: str, timeout_sec: int = 1) -> bool:
    try:
        res = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_sec), ip],
            capture_output=True,
            text=True,
            timeout=timeout_sec + 2,
        )
        return res.returncode == 0
    except Exception:
        return False


def _ssh_base(user: str, ip: str) -> List[str]:
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        f"{user}@{ip}",
    ]


def sshpass_run(user: str, password: str, ip: str, cmd: str, timeout: int = 60) -> Tuple[int, str, str]:
    """Run a remote command via sshpass and return (rc, stdout, stderr)."""
    full = ["sshpass", "-p", password] + _ssh_base(user, ip) + [cmd]
    try:
        res = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"


def expect_available() -> bool:
    return shutil.which("expect") is not None


def expect_handle_forced_password_change(ip: str, old_password: str, new_password: str,
                                        user: str = "cumulus", timeout: int = 60) -> Tuple[bool, str]:
    """
    If the switch forces an immediate password change, perform it via expect.
    Returns (success, output).
    """
    if not expect_available():
        return False, "expect not found on this system (required for forced password change handling)"

    expect_script = f"""
set timeout {timeout}
log_user 1
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {user}@{ip}

expect {{
  "Are you sure you want to continue connecting" {{
    send "yes\\r"
    exp_continue
  }}
  "Current password:" {{
    send "{old_password}\\r"
    exp_continue
  }}
  "New password:" {{
    send "{new_password}\\r"
    exp_continue
  }}
  "Retype new password:" {{
    send "{new_password}\\r"
    exp_continue
  }}
  -re {{\\$ $}} {{
    send "exit\\r"
  }}
  -re {{# $}} {{
    send "exit\\r"
  }}
  -re "password:" {{
    send "{old_password}\\r"
    exp_continue
  }}
  eof {{
    # done
  }}
  timeout {{
    puts "EXPECT_TIMEOUT"
  }}
}}
"""

    try:
        proc = subprocess.run(["expect", "-c", expect_script], capture_output=True, text=True, timeout=timeout + 10)
        out = (proc.stdout or "") + (proc.stderr or "")
        if "EXPECT_TIMEOUT" in out:
            return False, out[-4000:]
        return True, out[-4000:]
    except subprocess.TimeoutExpired:
        return False, "expect execution timed out"
    except Exception as e:
        return False, str(e)


def configure_oob_bridge(ip: str, password: str, user: str = "cumulus") -> Tuple[bool, str]:
    """Configure oob-mgmt-switch swp0-50 as bridged ports (NVUE) and apply."""
    outputs: List[str] = []

    cmds = [
        "nv set interface swp0-50 bridge domain br_default",
        "nv config apply -y",
    ]

    for cmd in cmds:
        rc, out, err = sshpass_run(user, password, ip, cmd, timeout=120)
        outputs.append(f"$ {cmd}\nRC={rc}\n{out}{err}")
        if rc == 0:
            continue

        # Retry with sudo (some images require it)
        sudo_cmd = f"echo {password} | sudo -S {cmd}"
        rc2, out2, err2 = sshpass_run(user, password, ip, sudo_cmd, timeout=120)
        outputs.append(f"$ {sudo_cmd}\nRC={rc2}\n{out2}{err2}")
        if rc2 != 0:
            return False, "\n\n".join(outputs)[-8000:]

    return True, "\n\n".join(outputs)[-8000:]


def wait_for_switch_leases_and_connectivity(expected_macs: Dict[str, str],
                                            timeout_sec: int = 240,
                                            poll_sec: int = 5) -> Tuple[bool, str, Dict[str, str]]:
    """
    Wait until all expected switch MACs appear with active leases and are pingable.
    Returns (success, output, {switch_name: ip}).
    """
    start = time.time()
    resolved: Dict[str, str] = {}
    log_lines: List[str] = []

    while time.time() - start < timeout_sec:
        leases = parse_dhcpd_leases(DHCP_LEASES_FILE)

        missing = []
        for sw, mac in expected_macs.items():
            ip = leases.get(_normalize_mac(mac))
            if ip:
                resolved[sw] = ip
            else:
                missing.append(sw)

        not_pingable = []
        for sw, ip in sorted(resolved.items()):
            if not ping_host(ip):
                not_pingable.append(sw)

        log_lines.append(
            f"[{int(time.time()-start)}s] leases: {len(resolved)}/{len(expected_macs)} "
            f"(missing: {', '.join(missing) if missing else 'none'}; "
            f"not-pingable: {', '.join(not_pingable) if not_pingable else 'none'})"
        )

        if not missing and not not_pingable:
            return True, "\n".join(log_lines)[-8000:], resolved

        time.sleep(poll_sec)

    return False, "\n".join(log_lines)[-8000:], resolved


def add_devices_to_bcm_only(csv_path: Path, username: str, password: str) -> bool:
    """Add devices to BCM without installing cm-lite-daemon.
    
    Uses cmsh commands directly to add devices from CSV.
    """
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            devices = list(reader)
        
        for device in devices:
            hostname = device.get('Hostname') or device.get('hostname', '')
            ip = device.get('IP') or device.get('ip', '')
            mac = device.get('MAC') or device.get('mac', '')
            network = device.get('Network') or device.get('network', 'internalnet')
            
            if not hostname or not ip:
                continue
            
            # Add device to BCM using cmsh
            cmds = [
                f"{CMSH} -c 'device; add switch {hostname}; commit'",
                f"{CMSH} -c \"device; use {hostname}; set ip {ip}; set mac {mac}; set network {network}; set hasclientdaemon yes; commit\"",
                f"{CMSH} -c \"device; use {hostname}; accesssettings; set username {username}; set password {password}; set -e {BCMProps().access_force_param} yes; commit\"",
                f"{CMSH} -c \"device; use {hostname}; ztpsettings; set enableapi yes; commit\"",
                f"{CMSH} -c \"device; use {hostname}; initialize\"",
            ]
            for cmd in cmds:
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if res.returncode != 0:
                    stderr = (res.stderr or '').strip()
                    # cmsh sometimes returns non-zero for idempotent operations like 'already exists'
                    if 'already' in stderr.lower() or 'exists' in stderr.lower():
                        continue
                    print(f"    cmsh failed for {hostname} ({ip}): {cmd}")
                    if stderr:
                        print(f"      stderr: {stderr[:300]}")
                    return False
        
        return True
    except Exception as e:
        print(f"    Error adding devices to BCM: {e}")
        return False






def _redact_argv(argv):
    """Redact secrets from argv list."""
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--password' and i + 1 < len(argv):
            out.extend(['--password', '******'])
            i += 2
            continue
        if a == '--new-password' and i + 1 < len(argv):
            out.extend(['--new-password', '******'])
            i += 2
            continue
        if a.startswith('--password='):
            out.append('--password=******')
            i += 1
            continue
        if a.startswith('--new-password='):
            out.append('--new-password=******')
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _redact_cmd(cmd: str) -> str:
    # Replace common password forms
    cmd = re.sub(r"(--password)\s+([^\s]+)", r"\1 ******", cmd)
    cmd = re.sub(r"--password=([^\s]+)", "--password=******", cmd)
    cmd = re.sub(r"(--new-password)\s+([^\s]+)", r"\1 ******", cmd)
    cmd = re.sub(r"--new-password=([^\s]+)", "--new-password=******", cmd)
    return cmd
# ============================================================================
# Logging
# ============================================================================

def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


class RunLogger:
    """Writes per-step logs + a run summary under .logs/."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.run_dir = LOGS_DIR / "test-loop" / self.run_id
        self.steps = []

        if self.enabled:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_run_info(self, argv: List[str]):
        if not self.enabled:
            return
        info = {
            "run_id": self.run_id,
            "start_time": datetime.now().isoformat(),
            "cwd": str(REPO_DIR),
            "argv": _redact_argv(argv),
        }
        (self.run_dir / "run-info.json").write_text(json.dumps(info, indent=2))

    def write_step(self, test_name: str, step: StepResult):
        if not self.enabled:
            return

        idx = len(self.steps) + 1
        fname = f"{idx:02d}_{_safe_filename(test_name)}_{_safe_filename(step.name)}.log"
        meta = {
            "test": test_name,
            "step": step.name,
            "success": step.success,
            "duration_seconds": step.duration,
            "message": step.message,
            "command": _redact_cmd(step.command),
        }
        body = []
        body.append("=" * 80)
        body.append("META")
        body.append("=" * 80)
        body.append(json.dumps(meta, indent=2))
        body.append("\n" + "=" * 80)
        body.append("OUTPUT")
        body.append("=" * 80)
        body.append(step.output or "")
        (self.run_dir / fname).write_text("\n".join(body))

        self.steps.append(meta)

    def write_summary(self, results: List[TestResult]):
        if not self.enabled:
            return
        summary = {
            "run_id": self.run_id,
            "end_time": datetime.now().isoformat(),
            "tests": [],
        }
        for t in results:
            summary["tests"].append({
                "name": t.name,
                "success": t.success,
                "start_time": t.start_time,
                "end_time": t.end_time,
                "duration_seconds": t.duration,
                "steps": [
                    {
                        "name": s.name,
                        "success": s.success,
                        "duration_seconds": s.duration,
                        "message": s.message,
                        "command": _redact_cmd(getattr(s, "command", "")),
                    }
                    for s in t.steps
                ],
            })
        (self.run_dir / "run-summary.json").write_text(json.dumps(summary, indent=2))
# ============================================================================
# Test Implementations
# ============================================================================

class TestRunner:
    """Runs automated tests."""
    
    def __init__(self, verbose: bool = False, dry_run: bool = False, 
                 password: str = DEFAULT_TEST_PASSWORD, logger: "RunLogger" = None):
        self.verbose = verbose
        self.dry_run = dry_run
        self.password = password
        self.results: List[TestResult] = []
        self.logger = logger
    
    def run_step(self, name: str, script: str, args: str = "", 
                 timeout: int = 600, input_text: str = None) -> StepResult:
        """Run a single test step."""
        print(f"\n  Step: {name}")

        if self.dry_run:
            print(f"    [DRY RUN] Would run: {script} {args}")
            cmd = f"python3 {REPO_DIR / script} {args}".strip()
            return StepResult(name=name, success=True, duration=0,
                            message="[DRY RUN] Skipped", command=cmd)
        
        result = run_script(script, args, timeout, input_text, self.verbose)
        
        status = "✓" if result.success else "✗"
        print(f"    {status} {result.message} ({result.duration:.1f}s)")

        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), result)

        return result
    
    def reset_simulation(self) -> StepResult:
        """
        Rebuild the *test switches only* in NVIDIA Air and remove those test switches from BCM.

        IMPORTANT: This does NOT reset the entire NVIDIA Air simulation and does NOT rebuild/reset
        the BCM node. It only rebuilds the specific switch nodes defined by the test topology and
        performs BCM device cleanup for those switches.
        """
        print("\n  Step: Rebuild test switches + BCM cleanup (NOT full simulation reset)")
        
        if self.dry_run:
            print("    [DRY RUN] Would rebuild test switches + clean BCM (NOT full simulation reset)")
            return StepResult(name="reset", success=True, duration=0,
                            message="[DRY RUN] Skipped")
        
        start = time.time()
        
        # Run test-sim-reset.py (switch-only rebuild + BCM device cleanup)
        result = run_script(
            "scripts/tests/test-sim-reset.py", 
            "", 
            timeout=400,  # 6+ minutes for rebuild
            verbose=self.verbose
        )
        
        if not result.success:
            return result
        
        # Wait for switches to boot and get DHCP
        wait_with_countdown(DHCP_WAIT_SECONDS, "Waiting for switches to get DHCP addresses...")
        wait_with_countdown(BOOT_STABILIZE_SECONDS, "Waiting for boot stabilization...")
        
        result.duration = time.time() - start
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), result)
        return result

    def preflight_oob_bridge_and_wait_for_dhcp(self) -> StepResult:
        """
        Pre-flight network fix for NVIDIA Air topology:
        - Detect if oob-mgmt-switch swp0 has taken a DHCP lease (routed mode issue)
        - If reachable, login with default creds and handle forced password reset
        - Apply NVUE bridge config on swp0-50 (br_default) and apply
        - Wait for spine/leaf switches to receive DHCP leases and be pingable
        """
        step_name = "preflight-oob-bridge"
        start = time.time()

        if self.dry_run:
            return StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output="Would: detect oob-mgmt-switch swp0 lease, fix NVUE bridge config, wait for switch DHCP leases",
                command=f"topology={TOPOLOGY_FILE} leases={DHCP_LEASES_FILE}",
            )

        logs: List[str] = []
        logs.append(f"Topology file: {TOPOLOGY_FILE}")
        logs.append(f"DHCP leases:   {DHCP_LEASES_FILE}")

        try:
            oob_swp0_mac, switch_eth0_macs = parse_topology_macs(TOPOLOGY_FILE)
        except Exception as e:
            duration = time.time() - start
            step = StepResult(
                name=step_name,
                success=False,
                duration=duration,
                message="Failed to parse topology file",
                output=str(e),
                command=f"parse_topology {TOPOLOGY_FILE}",
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        if not oob_swp0_mac:
            logs.append("WARNING: Could not find oob-mgmt-switch swp0 MAC in topology.")
        else:
            logs.append(f"oob-mgmt-switch swp0 MAC: {oob_swp0_mac}")

        # Look for a DHCP lease for oob swp0 MAC
        leases = parse_dhcpd_leases(DHCP_LEASES_FILE)
        oob_ip = leases.get(oob_swp0_mac) if oob_swp0_mac else None
        if not oob_ip:
            logs.append("No active DHCP lease found for oob-mgmt-switch swp0 MAC (ok if not present).")
        else:
            logs.append(f"Found DHCP lease for oob swp0: {oob_ip}")
            if ping_host(oob_ip):
                logs.append(f"Ping OK: {oob_ip}")

                # Try a simple non-interactive command with default creds.
                rc, out, err = sshpass_run("cumulus", "cumulus", oob_ip, "echo ok", timeout=20)
                logs.append(f"ssh default creds rc={rc} out={out.strip()} err={err.strip()[:200]}")

                working_pw = "cumulus"
                if rc != 0:
                    # Might be forced password change on first login.
                    ok, exp_out = expect_handle_forced_password_change(
                        ip=oob_ip,
                        old_password="cumulus",
                        new_password=self.password,
                        user="cumulus",
                        timeout=60,
                    )
                    logs.append("expect password-reset output (tail):")
                    logs.append(exp_out)
                    if not ok:
                        duration = time.time() - start
                        step = StepResult(
                            name=step_name,
                            success=False,
                            duration=duration,
                            message="Failed to handle forced password change on oob-mgmt-switch",
                            output="\n".join(logs)[-8000:],
                            command=f"expect ssh cumulus@{oob_ip}",
                        )
                        if self.logger is not None:
                            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
                        return step
                    working_pw = self.password
                else:
                    # Default creds worked; still prefer using the test password if already set.
                    working_pw = "cumulus"

                # Apply NVUE bridge configuration
                ok, cfg_out = configure_oob_bridge(oob_ip, working_pw, user="cumulus")
                logs.append("NVUE bridge config output (tail):")
                logs.append(cfg_out)
                if not ok:
                    # Retry with test password (in case password was already changed but not forced)
                    ok2, cfg_out2 = configure_oob_bridge(oob_ip, self.password, user="cumulus")
                    logs.append("NVUE bridge config retry output (tail):")
                    logs.append(cfg_out2)
                    if not ok2:
                        duration = time.time() - start
                        step = StepResult(
                            name=step_name,
                            success=False,
                            duration=duration,
                            message="Failed to configure oob-mgmt-switch NVUE bridge settings",
                            output="\n".join(logs)[-8000:],
                            command=f"nv set/apply on {oob_ip}",
                        )
                        if self.logger is not None:
                            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
                        return step
            else:
                logs.append(f"Ping FAILED: {oob_ip} (cannot apply bridge fix)")

        # Wait for the other switches to get DHCP leases and be pingable
        if len(switch_eth0_macs) != len(TEST_SWITCHES):
            logs.append(
                f"WARNING: Topology did not yield eth0 MACs for all switches "
                f"({len(switch_eth0_macs)}/{len(TEST_SWITCHES)} found). Will wait for those found."
            )

        ok, wait_out, resolved = wait_for_switch_leases_and_connectivity(
            expected_macs=switch_eth0_macs,
            timeout_sec=300,
            poll_sec=5,
        )
        logs.append("DHCP wait summary:")
        logs.append(wait_out)
        logs.append("Resolved switch IPs:")
        for sw in TEST_SWITCHES:
            if sw in resolved:
                logs.append(f"  - {sw}: {resolved[sw]}")

        duration = time.time() - start
        step = StepResult(
            name=step_name,
            success=ok,
            duration=duration,
            message="Success" if ok else "Failed to observe DHCP leases/connectivity for all switches",
            output="\n".join(logs)[-8000:],
            command=f"oob_mac={oob_swp0_mac or 'unknown'} wait_dhcp",
        )
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
        return step
    
    def generate_csv_from_dhcp(self) -> StepResult:
        """Generate CSV from DHCP leases."""
        result = self.run_step(
            "Generate CSV from DHCP",
            "scripts/csv-from-dhcp.py",
            ""
        )
        
        if result.success and not self.dry_run:
            # Filter out oob-mgmt-switch
            print("    Filtering out oob-mgmt-switch...")
            if filter_csv_remove_oob_switch(FROM_DHCP_CSV):
                count = count_devices_in_csv(FROM_DHCP_CSV)
                print(f"    ✓ CSV has {count} devices")
            else:
                result.success = False
                result.message = "Failed to filter CSV"
        
        return result
    
    def map_hostnames(self) -> StepResult:
        """Map hostnames from topology file."""
        return self.run_step(
            "Map hostnames from topology",
            "scripts/map-csv-topology.py",
            f"--csv {FROM_DHCP_CSV} --topology {TOPOLOGY_FILE}"
        )
    
    def change_defaults_all(self) -> StepResult:
        """Change all defaults on switches: password + hostname + disable ZTP (non-interactive)."""
        pwd = shlex.quote(self.password)
        return self.run_step(
            "Change defaults (password + hostname + disable ZTP)",
            "scripts/change-switch-defaults.py",
            # No action flags => change-switch-defaults.py does ALL actions by default.
            f"--csv {FROM_DHCP_CSV} --new-password {pwd}",
            timeout=300
        )
    
    def deploy_switches(self) -> StepResult:
        """Deploy switches to BCM using --csv with --non-interactive."""
        csv_path = shlex.quote(str(FROM_DHCP_CSV))
        pwd = shlex.quote(self.password)
        return self.run_step(
            "Deploy to BCM",
            "deploy_bcm_switches.py",
            f"--csv {csv_path} --non-interactive --username cumulus --password {pwd}",
            timeout=900  # 15 minutes for full deployment
        )
    
    def deploy_from_bcm(self) -> StepResult:
        """Deploy using --from-bcm mode (install on existing BCM devices)."""
        pwd = shlex.quote(self.password)
        return self.run_step(
            "Deploy from BCM (install cm-lite-daemon)",
            "deploy_bcm_switches.py",
            f"--from-bcm --non-interactive --username cumulus --password {pwd}",
            timeout=900  # 15 minutes for full deployment
        )
    
    def add_devices_to_bcm(self) -> StepResult:
        """Add devices to BCM without installing cm-lite-daemon."""
        print("\n  Step: Add devices to BCM (without daemon install)")
        
        if self.dry_run:
            print(f"    [DRY RUN] Would add devices from {FROM_DHCP_CSV} to BCM")
            return StepResult(name="add_to_bcm", success=True, duration=0,
                            message="[DRY RUN] Skipped")
        
        start = time.time()
        success = add_devices_to_bcm_only(FROM_DHCP_CSV, "cumulus", self.password)
        duration = time.time() - start
        
        status = "✓" if success else "✗"
        message = "Success" if success else "Failed"
        print(f"    {status} {message} ({duration:.1f}s)")
        
        return StepResult(
            name="add_to_bcm",
            success=success,
            duration=duration,
            message=message
        )
    
    def validate_deployment(self) -> StepResult:
        """Run validation testing."""
        pwd = shlex.quote(self.password)
        result = self.run_step(
            "Validate deployment",
            "scripts/validation-testing.py",
            # Always run validation with --verbose so failures include actionable detail
            # in the step log even if test-loop itself is not running with --verbose.
            f"--password {pwd} --verbose",
            timeout=600
        )
        
        if not self.dry_run:
            passed, failed = get_validation_summary(result.output)
            print(f"    Validation: {passed} passed, {failed} failed")
        
        return result
    
    def run_test_1(self, skip_reset: bool = False) -> TestResult:
        """
        Test 1: Full Deployment from DHCP Leases
        
        Steps:
        1. Rebuild test switches + BCM cleanup (NOT full simulation reset)
        2. Generate CSV from DHCP
        3. Map hostnames from topology
        4. Change defaults (password + hostname + disable ZTP)
        5. Deploy using --csv
        6. Validate deployment
        """
        print("\n" + "=" * 70)
        print("TEST 1: Full Deployment from DHCP Leases")
        print("=" * 70)
        
        test = TestResult(
            name="Test 1: Full Deployment from DHCP Leases",
            success=True,
            start_time=datetime.now().isoformat()
        )
        
        start = time.time()
        
        # Clear any existing config
        if not self.dry_run:
            clear_config()
        
        steps = []
        
        # Step 1: Rebuild test switches + BCM cleanup
        if not skip_reset:
            step = self.reset_simulation()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test
        
        # Step 1b: Preflight oob-mgmt-switch bridge fix + wait for DHCP leases
        step = self.preflight_oob_bridge_and_wait_for_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 2: Generate CSV from DHCP
        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 3: Map hostnames (so we can set them on switches during setup)
        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test

        # Step 4: Change defaults (password + hostname + disable ZTP)
        step = self.change_defaults_all()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 5: Deploy
        step = self.deploy_switches()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 6: Validate (even if deploy failed, to see what state we're in)
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test
    
    def run_test_2(self, skip_reset: bool = False) -> TestResult:
        """
        Test 2: Deployment with Switch Setup First
        
        Steps:
        1. Rebuild test switches + BCM cleanup (NOT full simulation reset)
        2. Generate CSV from DHCP
        3. Map hostnames from topology
        4. Change defaults (password + hostname + disable ZTP)
        5. Deploy using --csv
        6. Validate deployment
        """
        print("\n" + "=" * 70)
        print("TEST 2: Deployment with Switch Setup First")
        print("=" * 70)
        
        test = TestResult(
            name="Test 2: Deployment with Switch Setup First",
            success=True,
            start_time=datetime.now().isoformat()
        )
        
        start = time.time()
        
        # Clear any existing config
        if not self.dry_run:
            clear_config()
        
        steps = []
        
        # Step 1: Rebuild test switches + BCM cleanup
        if not skip_reset:
            step = self.reset_simulation()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test
        
        # Step 1b: Preflight oob-mgmt-switch bridge fix + wait for DHCP leases
        step = self.preflight_oob_bridge_and_wait_for_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 2: Generate CSV from DHCP
        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 3: Map hostnames (before changing on switches)
        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 4: Change defaults (password + hostname + disable ZTP)
        step = self.change_defaults_all()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 5: Deploy
        step = self.deploy_switches()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 6: Validate
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test
    
    def run_test_3(self, skip_reset: bool = False) -> TestResult:
        """
        Test 3: Install on Switches Already in BCM (--from-bcm mode)
        
        Steps:
        1. Rebuild test switches + BCM cleanup (NOT full simulation reset)
        2. Generate CSV from DHCP
        3. Map hostnames from topology
        4. Change defaults (password + hostname + disable ZTP)
        5. Add devices to BCM (without installing daemon)
        6. Deploy using --from-bcm (installs cm-lite-daemon on existing BCM devices)
        7. Validate deployment
        """
        print("\n" + "=" * 70)
        print("TEST 3: Install on Switches Already in BCM (--from-bcm)")
        print("=" * 70)
        
        test = TestResult(
            name="Test 3: Install on Switches Already in BCM",
            success=True,
            start_time=datetime.now().isoformat()
        )
        
        start = time.time()
        
        # Clear any existing config
        if not self.dry_run:
            clear_config()
        
        steps = []
        
        # Step 1: Rebuild test switches + BCM cleanup
        if not skip_reset:
            step = self.reset_simulation()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test
        
        # Step 1b: Preflight oob-mgmt-switch bridge fix + wait for DHCP leases
        step = self.preflight_oob_bridge_and_wait_for_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 2: Generate CSV from DHCP
        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 3: Map hostnames (so we can set them on switches during setup)
        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test

        # Step 4: Change defaults (password + hostname + disable ZTP)
        step = self.change_defaults_all()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 5: Add devices to BCM (without installing daemon)
        step = self.add_devices_to_bcm()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 6: Deploy using --from-bcm
        step = self.deploy_from_bcm()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 7: Validate
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test
    
    def print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        
        all_passed = True
        
        for test in self.results:
            status = "✓ PASSED" if test.success else "✗ FAILED"
            print(f"\n{test.name}")
            print(f"  Status: {status}")
            print(f"  Duration: {test.duration:.1f}s")
            print(f"  Steps:")
            
            for step in test.steps:
                step_status = "✓" if step.success else "✗"
                print(f"    {step_status} {step.name}: {step.message} ({step.duration:.1f}s)")
            
            if not test.success:
                all_passed = False
        
        print("\n" + "-" * 70)
        if all_passed:
            print("OVERALL: ✓ ALL TESTS PASSED")
        else:
            passed = sum(1 for t in self.results if t.success)
            total = len(self.results)
            print(f"OVERALL: ✗ {passed}/{total} TESTS PASSED")
        print("=" * 70)
        
        return all_passed


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Automated test loop for BCM switch deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tests Available:
  Test 1: Full Deployment from DHCP Leases
    - CSV generated from DHCP, passwords changed, hostnames mapped, deploy
  
  Test 2: Deployment with Switch Setup First  
    - Hostnames set on switches before deploy, then deploy discovers them

  Test 3: Install on Switches Already in BCM
    - Devices added to BCM first, then --from-bcm installs cm-lite-daemon

Examples:
  %(prog)s                    # Run all tests
  %(prog)s --test1            # Run only Test 1
  %(prog)s --test2            # Run only Test 2
  %(prog)s --test3            # Run only Test 3
  %(prog)s --test1 --no-reset # Run Test 1 without rebuilding test switches / BCM cleanup
  %(prog)s --stop-on-fail     # Stop after the first test failure (preserve state for debugging)
  %(prog)s --dry-run          # Show what would be done

Prerequisites:
  - scripts/tests/.env configured with NVIDIA Air credentials
  - NVIDIA Air simulation running
  - BCM head node with network access to switches
        """
    )
    
    parser.add_argument("--test1", action="store_true",
                       help="Run only Test 1 (DHCP lease deployment)")
    parser.add_argument("--test2", action="store_true",
                       help="Run only Test 2 (switch setup first)")
    parser.add_argument("--test3", action="store_true",
                       help="Run only Test 3 (--from-bcm mode)")
    parser.add_argument("--no-reset", action="store_true",
                       help="Skip switch-only rebuild + BCM device cleanup (use existing test state)")
    parser.add_argument("--stop-on-fail", action="store_true",
                       help="Stop after the first test failure (do not proceed to subsequent tests)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without running")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed output from each step")
    parser.add_argument("--password", type=str, default=DEFAULT_TEST_PASSWORD,
                       help=f"Password to set on switches (default: {DEFAULT_TEST_PASSWORD})")
    
    args = parser.parse_args()
    
    # Determine which tests to run
    any_specific = args.test1 or args.test2 or args.test3
    run_test1 = args.test1 or not any_specific
    run_test2 = args.test2 or not any_specific
    run_test3 = args.test3 or not any_specific
    
    # Password is passed to TestRunner
    test_password = args.password
    
    print("=" * 70)
    print("BCM SWITCH DEPLOYMENT - AUTOMATED TEST LOOP")
    print("=" * 70)
    print(f"Start time: {datetime.now().isoformat()}")
    print(f"Tests to run: ", end="")
    tests_to_run = []
    if run_test1:
        tests_to_run.append("Test 1")
    if run_test2:
        tests_to_run.append("Test 2")
    if run_test3:
        tests_to_run.append("Test 3")
    print(", ".join(tests_to_run))
    print(f"Skip reset: {args.no_reset}")
    print(f"Dry run: {args.dry_run}")
    
    # Check prerequisites
    if not args.dry_run:
        env_file = SCRIPT_DIR / ".env"
        if not env_file.exists():
            print(f"\nError: {env_file} not found")
            print("Please copy sample-configs/sample.env to .env and configure it.")
            sys.exit(1)
    
    # Logging
    logger = RunLogger(enabled=not args.dry_run)
    if not args.dry_run:
        logger.write_run_info(sys.argv)
        print(f"\nLogs will be written to: {logger.run_dir}")

    # Run tests
    runner = TestRunner(verbose=args.verbose, dry_run=args.dry_run, password=test_password, logger=logger)
    
    try:
        selected = []
        if run_test1:
            selected.append(("Test 1", runner.run_test_1))
        if run_test2:
            selected.append(("Test 2", runner.run_test_2))
        if run_test3:
            selected.append(("Test 3", runner.run_test_3))

        # If running multiple tests, we *always* rebuild the test switches + clean BCM before each test to ensure isolation.
        # --no-reset is only respected when running exactly one test.
        if args.no_reset and len(selected) > 1:
            print("\nNOTE: --no-reset was specified, but multiple tests were selected.")
            print("      For isolation, this run will rebuild test switches + clean BCM between tests (ignoring --no-reset).")

        for name, fn in selected:
            runner._current_test_name = name
            skip_reset = args.no_reset and len(selected) == 1
            result = fn(skip_reset=skip_reset)
            runner.results.append(result)
            if args.stop_on_fail and not result.success:
                print("\nNOTE: --stop-on-fail enabled; stopping after first failure to preserve state for debugging.")
                break

        # Print summary
        all_passed = runner.print_summary()
        if not args.dry_run:
            logger.write_summary(runner.results)

        # Exit code
        sys.exit(0 if all_passed else 1)

    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        runner.print_summary()
        sys.exit(130)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
