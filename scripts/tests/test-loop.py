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

Test 4: ZTP End-to-End Recovery (rebuild switches, keep BCM + staged ZTP)
  - Assume switches are already in BCM and ZTP is staged
  - Add a known marker to config (eth0 description: "ZTP Works!")
  - Rebuild switches in Air (skip BCM cleanup) so they boot via ZTP
  - Poll ZTP status until complete
  - Validate: marker present, cm-lite-daemon running, BCM status UP

Usage:
    ./test-loop.py              # Run all tests (1, 2, and 3)
    ./test-loop.py --test1      # Run only Test 1
    ./test-loop.py --test2      # Run only Test 2
    ./test-loop.py --test3      # Run only Test 3
    ./test-loop.py --test4      # Run only Test 4 (ZTP end-to-end)
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
FILES_DIR = REPO_DIR / ".files"
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
ZTP_POLL_SECONDS = 30
ZTP_TIMEOUT_SECONDS = 600  # 10 minutes

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


def clear_files_dir() -> bool:
    """
    Clear the .files/ cache directory.

    This matters for test isolation: deploy_bcm_switches.py changes behavior based on whether
    cached artifacts are present in .files/ (pip wheelhouse, deb_packages, extracted cm-lite-daemon).
    """
    if not FILES_DIR.exists():
        return True
    try:
        shutil.rmtree(FILES_DIR)
        return True
    except Exception:
        return False


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


def ssh_try_passwords(user: str, passwords: List[str], ip: str, cmd: str, timeout: int = 60) -> Tuple[int, str, str, str]:
    """
    Try multiple passwords in order. Returns (rc, stdout, stderr, password_used).
    """
    for pw in passwords:
        rc, out, err = sshpass_run(user, pw, ip, cmd, timeout=timeout)
        if rc == 0:
            return rc, out, err, pw
    return rc, out, err, passwords[-1] if passwords else ""


def parse_ztp_show(output: str) -> Tuple[str, str]:
    """
    Parse `nv show system ztp` into (service, status).
    """
    service = ""
    status = ""
    for line in (output or "").splitlines():
        m = re.match(r"^\s*service\s+(\S+)\s*$", line)
        if m:
            service = m.group(1).strip().lower()
        m = re.match(r"^\s*status\s+(.+?)\s*$", line)
        if m:
            status = m.group(1).strip().lower()
    return service, status


def expect_available() -> bool:
    return shutil.which("expect") is not None


def sshpass_available() -> bool:
    return shutil.which("sshpass") is not None


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

    def air_health_check(self) -> StepResult:
        """
        Fast validation that NVIDIA Air API + simulation are accessible.

        This catches cases where the sim may appear "not loaded" in the UI (or API is unhealthy)
        even if SSH to nodes still works. Without this, resets can hang until timeout and mask root cause.
        """
        return self.run_step(
            "NVIDIA Air health-check (API + simulation access)",
            "scripts/tests/test-sim-reset.py",
            "--health-check",
            timeout=90,
        )
    
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

        # Always log the step, even on failure/timeout, so test-loop runs are debuggable.
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), result)

        if not result.success:
            return result
        
        # Wait for switches to boot and get DHCP
        wait_with_countdown(DHCP_WAIT_SECONDS, "Waiting for switches to get DHCP addresses...")
        wait_with_countdown(BOOT_STABILIZE_SECONDS, "Waiting for boot stabilization...")
        
        result.duration = time.time() - start
        return result

    def reset_switches_keep_bcm(self) -> StepResult:
        """
        Rebuild test switches in Air but keep BCM configuration/staged ZTP.

        This uses scripts/tests/test-sim-reset.py --skip-bcm.
        """
        step_name = "reset-switches-keep-bcm"
        start = time.time()

        if self.dry_run:
            return StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output="Would: rebuild test switches in Air, skipping BCM cleanup",
                command="scripts/tests/test-sim-reset.py --skip-bcm",
            )

        result = run_script(
            "scripts/tests/test-sim-reset.py",
            "--skip-bcm",
            timeout=400,
            verbose=self.verbose,
        )
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), result)
        if not result.success:
            return result

        # Wait for switches to boot and get DHCP
        wait_with_countdown(DHCP_WAIT_SECONDS, "Waiting for switches to get DHCP addresses...")
        wait_with_countdown(BOOT_STABILIZE_SECONDS, "Waiting for boot stabilization...")

        result.name = step_name
        result.duration = time.time() - start
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
            f"--csv {csv_path} --non-interactive --username cumulus --password {pwd} --stage-ztp",
            timeout=900  # 15 minutes for full deployment
        )
    
    def deploy_from_bcm(self) -> StepResult:
        """Deploy using --from-bcm mode (install on existing BCM devices)."""
        pwd = shlex.quote(self.password)
        return self.run_step(
            "Deploy from BCM (install cm-lite-daemon)",
            "deploy_bcm_switches.py",
            f"--from-bcm --non-interactive --username cumulus --password {pwd} --stage-ztp",
            timeout=900  # 15 minutes for full deployment
        )

    def ztp_preflight_config(self) -> StepResult:
        """Run ZTP preflight (config-only) to confirm staging artifacts are present."""
        csv_path = shlex.quote(str(FROM_DHCP_CSV))
        return self.run_step(
            "ZTP preflight (config-only)",
            "scripts/ztp-preflight.py",
            f"--csv {csv_path} --config-only",
            timeout=300
        )

    def enable_cm_lite_daemon_via_ztp(self) -> StepResult:
        """
        Ensure BCM is configured to include cm-lite-daemon installation in generated ZTP scripts.

        BCM 11: ztpsettings -> install lite daemon
        BCM 10: device.hasclientdaemon
        """
        step_name = "Enable cm-lite-daemon via ZTP + initialize"
        start = time.time()

        if self.dry_run:
            step = StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output="Would: cmsh set installlitedaemon/hasclientdaemon and run initialize for test switches",
                command="cmsh device;use <sw>; ...; initialize",
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        bcm_major, bcm_minor = get_bcm_version()
        logs: List[str] = [f"BCM version: {bcm_major}.{bcm_minor}"]

        ok_all = True
        for sw in TEST_SWITCHES:
            if bcm_major >= 11:
                cmds = [
                    f"{CMSH} -c \"device; use {sw}; ztpsettings; set installlitedaemon yes; commit\"",
                    f"{CMSH} -c \"device; use {sw}; initialize\"",
                ]
            else:
                cmds = [
                    f"{CMSH} -c \"device; use {sw}; set hasclientdaemon yes; commit\"",
                    f"{CMSH} -c \"device; use {sw}; initialize\"",
                ]

            for cmd in cmds:
                rc, out, err = run_cmd(cmd, timeout=120)
                logs.append(f"$ {cmd}\nrc={rc}\n{(out or '').strip()}\n{(err or '').strip()}")
                if rc != 0:
                    # tolerate idempotent "already" / "exists"
                    if "already" in (err or "").lower() or "exists" in (err or "").lower():
                        continue
                    ok_all = False

        duration = time.time() - start
        step = StepResult(
            name=step_name,
            success=ok_all,
            duration=duration,
            message="Success" if ok_all else "Failed",
            output="\n\n".join(logs)[-8000:],
            command="; ".join(["cmsh ..."] * 2),
        )
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
        return step
    
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

    def set_eth0_description_marker(self, description: str = "ZTP Works!") -> StepResult:
        """
        Add a deterministic marker to the running config on each switch so Test 4 can verify ZTP restored it.
        """
        step_name = f"Set eth0 description marker ({description})"
        start = time.time()

        if self.dry_run:
            step = StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output="Would: nv set interface eth0 description + apply on each switch",
                command="nv set interface eth0 description ...; nv config apply -y",
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        # Use current CSV mapping (generated earlier in the test).
        try:
            with open(FROM_DHCP_CSV, "r") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            step = StepResult(
                name=step_name,
                success=False,
                duration=time.time() - start,
                message="Failed to read from-dhcp.csv",
                output=str(e),
                command=str(FROM_DHCP_CSV),
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        hn_to_ip: Dict[str, str] = {}
        for r in rows:
            hn = (r.get("Hostname") or r.get("hostname") or "").strip()
            ip = (r.get("IP") or r.get("ip") or "").strip()
            if hn and ip:
                hn_to_ip[hn] = ip

        logs: List[str] = []
        ok_all = True
        for sw in TEST_SWITCHES:
            ip = hn_to_ip.get(sw, "")
            if not ip:
                ok_all = False
                logs.append(f"{sw}: missing IP in {FROM_DHCP_CSV}")
                continue

            # Apply marker via NVUE (writes canonical startup.yaml).
            cmds = [
                f"nv set interface eth0 description \"{description}\"",
                "nv config apply -y",
            ]

            for cmd in cmds:
                rc, out, err = sshpass_run("cumulus", self.password, ip, cmd, timeout=120)
                logs.append(f"{sw} ({ip}) $ {cmd}\nrc={rc}\n{(out or '').strip()}\n{(err or '').strip()}")
                if rc != 0:
                    # Retry with sudo
                    sudo_cmd = f"echo {self.password} | sudo -S {cmd}"
                    rc2, out2, err2 = sshpass_run("cumulus", self.password, ip, sudo_cmd, timeout=120)
                    logs.append(f"{sw} ({ip}) $ {sudo_cmd}\nrc={rc2}\n{(out2 or '').strip()}\n{(err2 or '').strip()}")
                    if rc2 != 0:
                        ok_all = False
                        break

            # Verify marker landed in startup.yaml
            verify = f"echo {self.password} | sudo -S grep -q \"description: {description}\" /etc/nvue.d/startup.yaml && echo OK"
            rc3, out3, err3 = sshpass_run("cumulus", self.password, ip, verify, timeout=60)
            logs.append(f"{sw} ({ip}) $ verify startup.yaml marker\nrc={rc3}\n{(out3 or '').strip()}\n{(err3 or '').strip()}")
            if rc3 != 0:
                ok_all = False

        step = StepResult(
            name=step_name,
            success=ok_all,
            duration=time.time() - start,
            message="Success" if ok_all else "Failed",
            output="\n\n".join(logs)[-8000:],
            command="nv set/apply on switches",
        )
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
        return step

    def wait_for_ztp_complete(self) -> StepResult:
        """
        Poll `nv show system ztp` on each switch until:
          - service == disabled
          - status == success
        """
        step_name = "Wait for ZTP completion (nv show system ztp)"
        start = time.time()

        if self.dry_run:
            step = StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output=f"Would: poll every {ZTP_POLL_SECONDS}s for up to {ZTP_TIMEOUT_SECONDS}s",
                command="nv show system ztp",
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        # Refresh CSV after rebuild so we have current IPs.
        try:
            with open(FROM_DHCP_CSV, "r") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            step = StepResult(
                name=step_name,
                success=False,
                duration=time.time() - start,
                message="Failed to read from-dhcp.csv",
                output=str(e),
                command=str(FROM_DHCP_CSV),
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        hn_to_ip: Dict[str, str] = {}
        for r in rows:
            hn = (r.get("Hostname") or r.get("hostname") or "").strip()
            ip = (r.get("IP") or r.get("ip") or "").strip()
            if hn and ip:
                hn_to_ip[hn] = ip

        passwords = [self.password, "cumulus"]
        done: Dict[str, bool] = {sw: False for sw in TEST_SWITCHES}
        logs: List[str] = []

        deadline = time.time() + ZTP_TIMEOUT_SECONDS
        while time.time() < deadline:
            all_done = True
            for sw in TEST_SWITCHES:
                if done.get(sw):
                    continue
                ip = hn_to_ip.get(sw, "")
                if not ip:
                    all_done = False
                    continue

                rc, out, err, used_pw = ssh_try_passwords("cumulus", passwords, ip, "nv show system ztp", timeout=30)
                if rc != 0:
                    all_done = False
                    continue
                service, status = parse_ztp_show(out)
                logs.append(f"{sw} ({ip}) ztp: service={service or '?'} status={status or '?'} (pw={used_pw})")
                if service == "disabled" and status == "success":
                    done[sw] = True
                else:
                    all_done = False

            if all_done:
                step = StepResult(
                    name=step_name,
                    success=True,
                    duration=time.time() - start,
                    message="Success",
                    output="\n".join(logs)[-8000:],
                    command="nv show system ztp",
                )
                if self.logger is not None:
                    self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
                return step
            time.sleep(ZTP_POLL_SECONDS)

        missing = [sw for sw, ok in done.items() if not ok]
        logs.append(f"Timed out waiting for: {', '.join(missing)}")
        step = StepResult(
            name=step_name,
            success=False,
            duration=time.time() - start,
            message="Timed out",
            output="\n".join(logs)[-8000:],
            command="nv show system ztp",
        )
        if self.logger is not None:
            self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
        return step

    def validate_ztp_recovery(self, description: str = "ZTP Works!") -> StepResult:
        """
        After ZTP completes, validate:
          1) eth0 description marker is present in startup.yaml
          2) cm-lite-daemon service is active(running)
          3) BCM device status is UP
        """
        step_name = "Validate ZTP recovery outcomes"
        start = time.time()

        if self.dry_run:
            step = StepResult(
                name=step_name,
                success=True,
                duration=0,
                message="[DRY RUN] Skipped",
                output="Would: check startup.yaml marker, systemctl is-active, and cmsh device status",
                command="ssh + cmsh checks",
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        try:
            with open(FROM_DHCP_CSV, "r") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            step = StepResult(
                name=step_name,
                success=False,
                duration=time.time() - start,
                message="Failed to read from-dhcp.csv",
                output=str(e),
                command=str(FROM_DHCP_CSV),
            )
            if self.logger is not None:
                self.logger.write_step(getattr(self, "_current_test_name", "unknown"), step)
            return step

        hn_to_ip: Dict[str, str] = {}
        for r in rows:
            hn = (r.get("Hostname") or r.get("hostname") or "").strip()
            ip = (r.get("IP") or r.get("ip") or "").strip()
            if hn and ip:
                hn_to_ip[hn] = ip

        passwords = [self.password, "cumulus"]
        logs: List[str] = []
        ok_all = True

        for sw in TEST_SWITCHES:
            ip = hn_to_ip.get(sw, "")
            if not ip:
                ok_all = False
                logs.append(f"{sw}: missing IP in {FROM_DHCP_CSV}")
                continue

            # 1) Marker present
            marker_cmd = f"echo {self.password} | sudo -S grep -q \"description: {description}\" /etc/nvue.d/startup.yaml && echo OK"
            rc1, out1, err1, _ = ssh_try_passwords("cumulus", passwords, ip, marker_cmd, timeout=60)
            logs.append(f"{sw} ({ip}) marker rc={rc1} out={out1.strip()} err={err1.strip()[:200]}")
            if rc1 != 0:
                ok_all = False

            # 2) cm-lite-daemon service running
            svc_cmd = f"echo {self.password} | sudo -S systemctl is-active cm-lite-daemon.service"
            rc2, out2, err2, _ = ssh_try_passwords("cumulus", passwords, ip, svc_cmd, timeout=60)
            logs.append(f"{sw} ({ip}) cm-lite-daemon is-active rc={rc2} out={out2.strip()} err={err2.strip()[:200]}")
            if rc2 != 0 or (out2 or "").strip() != "active":
                ok_all = False

            # 3) BCM status UP
            rc3, out3, err3 = run_cmd(f"{CMSH} -c \"device;use {sw};status\"", timeout=30)
            logs.append(f"BCM {sw} status rc={rc3} out={(out3 or '').strip()} err={(err3 or '').strip()[:200]}")
            if rc3 != 0 or "UP" not in (out3 or ""):
                ok_all = False

        return StepResult(
            name=step_name,
            success=ok_all,
            duration=time.time() - start,
            message="Success" if ok_all else "Failed",
            output="\n".join(logs)[-8000:],
            command="marker+service+cmsh status",
        )
    
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
            step = self.air_health_check()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

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

        # Step 5b: Ensure cm-lite-daemon install via ZTP is enabled and initialize regenerates scripts
        step = self.enable_cm_lite_daemon_via_ztp()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 6: Validate (even if deploy failed, to see what state we're in)
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 6b: ZTP preflight (config-only) to confirm staging happened
        step = self.ztp_preflight_config()
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
            step = self.air_health_check()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

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

        # Step 6b: Ensure cm-lite-daemon install via ZTP is enabled and initialize regenerates scripts
        step = self.enable_cm_lite_daemon_via_ztp()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 6c: ZTP preflight (config-only) to confirm staging happened
        step = self.ztp_preflight_config()
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
            step = self.air_health_check()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

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

        # Step 7b: Ensure cm-lite-daemon install via ZTP is enabled and initialize regenerates scripts
        step = self.enable_cm_lite_daemon_via_ztp()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 7c: ZTP preflight (config-only) to confirm staging happened
        step = self.ztp_preflight_config()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test

    def run_test_4(self, skip_reset: bool = False) -> TestResult:
        """
        Test 4: ZTP End-to-End Recovery

        Assumptions:
        - Switches are already in BCM
        - ZTP is already staged

        Steps:
        1. (Optional) Confirm ZTP staging is present (preflight config-only)
        2. Ensure cm-lite-daemon install via ZTP is enabled + initialize regenerates scripts
        3. Add marker to config (eth0 description: "ZTP Works!")
        4. Rebuild switches in Air (skip BCM cleanup) so they boot via ZTP
        5. Preflight oob bridge + regenerate DHCP CSV + map hostnames
        6. Wait for ZTP completion
        7. Validate marker present, cm-lite-daemon running, BCM status UP
        """
        print("\n" + "=" * 70)
        print("TEST 4: ZTP End-to-End Recovery")
        print("=" * 70)

        test = TestResult(
            name="Test 4: ZTP End-to-End Recovery",
            success=True,
            start_time=datetime.now().isoformat()
        )
        start = time.time()
        steps: List[StepResult] = []

        if not skip_reset:
            # We intentionally do NOT clear config.json here because we want to preserve BCM state.
            pass

        # Step 1: Preflight oob bridge + refresh DHCP CSV and hostnames
        # We do this BEFORE attempting any step that depends on from-dhcp.csv (marker, preflight config-only),
        # so Test 4 can run standalone and doesn't fail with "Failed to read from-dhcp.csv" as a secondary symptom.
        step = self.preflight_oob_bridge_and_wait_for_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test

        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test

        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test

        # Step 2: Confirm staging (config-only)
        step = self.ztp_preflight_config()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 3: Enable lite-daemon via ZTP + initialize
        step = self.enable_cm_lite_daemon_via_ztp()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 4: Add marker to config
        step = self.set_eth0_description_marker("ZTP Works!")
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 5: Rebuild switches (keep BCM)
        if not skip_reset:
            step = self.air_health_check()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

            step = self.reset_switches_keep_bcm()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

        # Step 6: Preflight oob bridge + refresh DHCP CSV and hostnames AFTER rebuild
        # (IP leases can change after rebuild)
        if not skip_reset:
            step = self.preflight_oob_bridge_and_wait_for_dhcp()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

            step = self.generate_csv_from_dhcp()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

            step = self.map_hostnames()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test

        # Step 7: Wait for ZTP complete
        step = self.wait_for_ztp_complete()
        steps.append(step)
        if not step.success:
            test.success = False

        # Step 8: Validate outcomes
        step = self.validate_ztp_recovery("ZTP Works!")
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
    parser.add_argument("--test4", action="store_true",
                       help="Run only Test 4 (ZTP end-to-end recovery)")
    parser.add_argument("--online", action="store_true",
                       help="Run tests with switch internet access enabled (ip_forward=1)")
    parser.add_argument("--airgapped", action="store_true",
                       help="Run tests with switch internet access disabled (ip_forward=0)")
    parser.add_argument("--keep-files", action="store_true",
                       help="Do not delete .files between online/airgapped mode runs (faster, but less isolated)")
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
    any_specific = args.test1 or args.test2 or args.test3 or args.test4
    run_test1 = args.test1 or not any_specific
    run_test2 = args.test2 or not any_specific
    run_test3 = args.test3 or not any_specific
    run_test4 = args.test4 or not any_specific

    if args.online and args.airgapped:
        print("Error: --online and --airgapped are mutually exclusive")
        sys.exit(2)

    modes: List[str] = []
    if args.online:
        modes = ["online"]
    elif args.airgapped:
        modes = ["airgapped"]
    else:
        modes = ["online", "airgapped"]
    
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
    if run_test4:
        tests_to_run.append("Test 4")
    print(", ".join(tests_to_run))
    print(f"Modes: {', '.join(modes)} (switch internet via ip_forward)")
    print(f"Skip reset: {args.no_reset}")
    print(f"Dry run: {args.dry_run}")
    
    # Check prerequisites
    if not args.dry_run:
        env_file = SCRIPT_DIR / ".env"
        if not env_file.exists():
            print(f"\nError: {env_file} not found")
            print("Please copy sample-configs/sample.env to .env and configure it.")
            sys.exit(1)
        if not sshpass_available():
            print("\nError: required tool 'sshpass' was not found in PATH.")
            print("test-loop uses sshpass for non-interactive SSH to switches.")
            sys.exit(1)
        if not expect_available():
            # Not always required, but extremely helpful when devices force a password change.
            print("\nWarning: 'expect' was not found in PATH.")
            print("Some NVIDIA Air topologies may require expect to handle forced password changes on first login.")
    
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
        if run_test4:
            selected.append(("Test 4", runner.run_test_4))

        # If running multiple tests, we *always* rebuild the test switches + clean BCM before each test to ensure isolation.
        # --no-reset is only respected when running exactly one test.
        if args.no_reset and len(selected) > 1:
            print("\nNOTE: --no-reset was specified, but multiple tests were selected.")
            print("      For isolation, this run will rebuild test switches + clean BCM between tests (ignoring --no-reset).")

        stop_now = False
        for mode in modes:
            if not args.dry_run:
                # ip_forward controls whether switches can reach the internet through this system (Air topology).
                forward = "1" if mode == "online" else "0"
                rc, out, err = run_cmd(f"sysctl -w net.ipv4.ip_forward={forward}", timeout=10)
                if rc != 0:
                    print(f"\nError: failed to set ip_forward={forward}: {err or out}")
                    if os.geteuid() != 0:
                        print("Hint: run test-loop as root (or via sudo) so it can change sysctl settings.")
                    sys.exit(1)
                print(f"\nMode '{mode}': set net.ipv4.ip_forward={forward}")

                # Ensure mode isolation: clear cached artifacts so deploy behavior isn't influenced
                # by whatever the previous mode downloaded/prepared.
                if not args.keep_files and len(modes) > 1:
                    print("Mode isolation: clearing .files/ cache...")
                    if not clear_files_dir():
                        print("Error: failed to delete .files/ (check permissions)")
                        sys.exit(1)
                    print("Mode isolation: .files/ cleared")

            for name, fn in selected:
                runner._current_test_name = f"{mode}:{name}"
                skip_reset = args.no_reset and len(selected) == 1 and len(modes) == 1
                result = fn(skip_reset=skip_reset)
                # Prefix test names with mode for clarity in summary/logs.
                result.name = f"[{mode}] {result.name}"
                runner.results.append(result)
                if args.stop_on_fail and not result.success:
                    print("\nNOTE: --stop-on-fail enabled; stopping after first failure to preserve state for debugging.")
                    stop_now = True
                    break
            if stop_now:
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
