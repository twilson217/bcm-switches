#!/usr/bin/env python3
"""
BCM Switch Deployment Validation Testing

A comprehensive automated validation test suite to verify that BCM switch
deployment completed successfully with desired outcomes and no side effects.

This script performs multi-layer validation:
1. BCM-side checks (device registration, settings, states)
2. Switch-side checks (cm-lite-daemon, ZTP, connectivity)
3. Communication checks (BCM <-> switch connectivity)
4. Configuration consistency checks
5. Log analysis for warnings/errors

Usage:
    ./scripts/validation-testing.py                    # Full validation
    ./scripts/validation-testing.py --csv FILE         # Validate switches from CSV
    ./scripts/validation-testing.py --switch IP        # Validate single switch
    ./scripts/validation-testing.py --bcm-only         # Only BCM-side checks
    ./scripts/validation-testing.py --switch-only      # Only switch-side checks
    ./scripts/validation-testing.py --json             # Output as JSON
    ./scripts/validation-testing.py --verbose          # Show detailed output
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import getpass
import shlex
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / ".configs"
DEFAULT_USERNAME = "cumulus"
DEFAULT_PASSWORD = ""

# BCM states that indicate success
BCM_SUCCESS_STATES = ['UP', 'IDLE', 'DOWN']  # DOWN is ok for monitoring-only

# BCM states that indicate problems
BCM_PROBLEM_STATES = ['INSTALLER_UNREACHABLE', 'INSTALLER_CALLFAILED', 'INSTALLING']

# Required pip packages on switches
REQUIRED_PIP_PACKAGES = ['netifaces', 'pyyaml', 'cffi']

# Important log patterns
LOG_PATTERNS = {
    'error': [
        r'error',
        r'failed',
        r'permission denied',
        r'connection refused',
        r'timeout',
    ],
    'warning': [
        r'warning',
        r'unable to',
        r'could not',
        r'retry',
    ],
    'success': [
        r'connected',
        r'registered',
        r'started',
        r'success',
    ]
}


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CheckResult:
    """Result of a single validation check."""
    name: str
    passed: bool
    message: str
    details: Optional[str] = None
    severity: str = "info"  # info, warning, error, critical
    

@dataclass
class SwitchValidation:
    """Validation results for a single switch."""
    hostname: str
    ip: str
    mac: str = ""
    bcm_checks: List[CheckResult] = field(default_factory=list)
    switch_checks: List[CheckResult] = field(default_factory=list)
    connectivity_checks: List[CheckResult] = field(default_factory=list)
    overall_passed: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ValidationReport:
    """Complete validation report."""
    switches: List[SwitchValidation] = field(default_factory=list)
    bcm_system_checks: List[CheckResult] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    duration_seconds: float = 0.0


# ============================================================================
# Utility Functions
# ============================================================================

def run_cmd(cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def run_ssh_cmd(ip: str, command: str, username: str, password: str, 
                timeout: int = 30) -> Tuple[bool, str]:
    """Run SSH command on remote host. Returns (success, output)."""
    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
    
    # Try with password
    cmd = f"sshpass -p {shlex.quote(password)} ssh {ssh_opts} {username}@{ip} {shlex.quote(command)}"
    rc, stdout, stderr = run_cmd(cmd, timeout)
    
    if rc == 0:
        return True, stdout
    
    # Try with sudo for commands that need it
    if 'permission denied' in stderr.lower() or 'sudo' in command.lower():
        cmd = (
            f"sshpass -p {shlex.quote(password)} ssh {ssh_opts} {username}@{ip} "
            f"{shlex.quote(f'echo {shlex.quote(password)} | sudo -S {command}')}"
        )
        rc, stdout, stderr = run_cmd(cmd, timeout)
        if rc == 0:
            return True, stdout
    
    return False, stderr


def _normalize_log_line_for_dedupe(line: str) -> str:
    """
    Best-effort normalization to dedupe repeated log lines where only timestamps/PIDs differ.
    """
    s = (line or "").strip()
    if not s:
        return s
    # Strip common syslog/journal prefixes
    s = re.sub(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\S+\s+", "", s)  # "Dec 16 11:22:33 host "
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+", "", s)
    s = re.sub(r"\[[0-9]+\]", "[PID]", s)  # replace [1234]
    s = re.sub(r"\bpid=\d+\b", "pid=PID", s, flags=re.IGNORECASE)
    return s.strip()


def dedupe_lines_with_counts(lines: List[str], limit: int = 10) -> List[Tuple[int, str]]:
    """Return a list of (count, representative_line) entries, sorted by count desc."""
    counts: Dict[str, int] = {}
    rep: Dict[str, str] = {}
    for line in lines:
        key = _normalize_log_line_for_dedupe(line)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        rep.setdefault(key, (line or "").strip())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[Tuple[int, str]] = []
    for key, cnt in items[:limit]:
        out.append((cnt, rep.get(key, key)))
    return out


def read_csv_file(csv_path: Path) -> List[Dict]:
    """Read devices from CSV file."""
    devices = []
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                device = {
                    'hostname': row.get('Hostname') or row.get('hostname', ''),
                    'ip': row.get('IP') or row.get('ip', ''),
                    'mac': (row.get('MAC') or row.get('mac', '')).upper(),
                }
                if device['ip']:
                    devices.append(device)
    except Exception as e:
        print(f"Error reading CSV: {e}")
    return devices


def load_config() -> Dict:
    """Load config.json if it exists."""
    config_file = CONFIG_DIR / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                return json.load(f)
        except:
            pass
    return {}


# ============================================================================
# BCM System Checks
# ============================================================================

class BCMSystemValidator:
    """Validates BCM system-level settings and state."""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
    
    def check_cmdaemon_running(self) -> CheckResult:
        """Check if BCM cmdaemon is running."""
        rc, out, err = run_cmd("pgrep -x cmd")
        if rc == 0:
            pid = out.strip()
            return CheckResult(
                name="BCM cmdaemon running",
                passed=True,
                message=f"cmdaemon is running (PID: {pid})"
            )
        return CheckResult(
            name="BCM cmdaemon running",
            passed=False,
            message="cmdaemon is NOT running",
            severity="critical"
        )
    
    def check_cmdaemon_ports(self) -> CheckResult:
        """Check if cmdaemon is listening on required ports."""
        rc, out, err = run_cmd("netstat -tlnp 2>/dev/null | grep -E ':808[01]'")
        if '8080' in out and '8081' in out:
            return CheckResult(
                name="BCM ports listening",
                passed=True,
                message="cmdaemon listening on ports 8080 and 8081"
            )
        return CheckResult(
            name="BCM ports listening",
            passed=False,
            message=f"cmdaemon not listening on expected ports",
            details=out or err,
            severity="error"
        )
    
    def check_dhcp_running(self) -> CheckResult:
        """Check if DHCP server is running."""
        rc, out, err = run_cmd("systemctl is-active dhcpd")
        if out.strip() == 'active':
            return CheckResult(
                name="DHCP server running",
                passed=True,
                message="DHCP server is active"
            )
        return CheckResult(
            name="DHCP server running",
            passed=False,
            message=f"DHCP server state: {out.strip()}",
            severity="warning"
        )
    
    def check_recent_errors(self) -> CheckResult:
        """Check syslog for recent BCM-related errors."""
        rc, out, err = run_cmd(
            "grep -i -E 'error|fail' /var/log/syslog 2>/dev/null | "
            "grep -i -E 'cmd|switch|cumulus' | tail -10"
        )
        if not out:
            return CheckResult(
                name="BCM syslog errors",
                passed=True,
                message="No recent BCM errors in syslog"
            )
        
        error_count = len(out.strip().split('\n'))
        lines = [l for l in out.split('\n') if l.strip()]
        deduped = dedupe_lines_with_counts(lines, limit=8)
        detail_lines = []
        for cnt, msg in deduped:
            prefix = f"({cnt}x) " if cnt > 1 else ""
            detail_lines.append(prefix + msg)
        return CheckResult(
            name="BCM syslog errors",
            passed=False,
            message=f"Found {error_count} recent error(s) in syslog",
            details="\n".join(detail_lines) if detail_lines else (out[:500] if self.verbose else ""),
            severity="warning"
        )
    
    def run_all_checks(self) -> List[CheckResult]:
        """Run all BCM system checks."""
        return [
            self.check_cmdaemon_running(),
            self.check_cmdaemon_ports(),
            self.check_dhcp_running(),
            self.check_recent_errors(),
        ]


# ============================================================================
# BCM Device Checks
# ============================================================================

class BCMDeviceValidator:
    """Validates BCM settings for a specific device."""
    
    def __init__(self, hostname: str, verbose: bool = False):
        self.hostname = hostname
        self.verbose = verbose
        self.device_info = {}
        self._load_device_info()
    
    def _load_device_info(self):
        """Load device info from BCM."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; show' 2>/dev/null"
        )
        if rc == 0:
            for line in out.split('\n'):
                if '  ' in line:
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        key = parts[0].strip().lower().replace(' ', '_')
                        value = parts[1].strip()
                        self.device_info[key] = value
    
    def check_device_exists(self) -> CheckResult:
        """Check if device exists in BCM."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; get hostname' 2>/dev/null"
        )
        if self.hostname in out:
            return CheckResult(
                name="Device exists in BCM",
                passed=True,
                message=f"{self.hostname} found in BCM"
            )
        return CheckResult(
            name="Device exists in BCM",
            passed=False,
            message=f"{self.hostname} NOT found in BCM",
            severity="critical"
        )
    
    def check_device_status(self) -> CheckResult:
        """Check device status in BCM."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; get status' 2>/dev/null"
        )
        status = out.strip()
        
        # Extract status from output like "[ UP ]" or "[ INSTALLER_UNREACHABLE ]"
        match = re.search(r'\[\s*([^\]]+)\s*\]', status)
        if match:
            state = match.group(1).strip()
        else:
            state = status
        
        if any(s in state for s in BCM_SUCCESS_STATES):
            return CheckResult(
                name="BCM device status",
                passed=True,
                message=f"Status: {state}"
            )
        
        if any(s in state for s in BCM_PROBLEM_STATES):
            return CheckResult(
                name="BCM device status",
                passed=False,
                message=f"Problem status: {state}",
                details="Device may not be communicating with BCM properly",
                severity="error"
            )
        
        return CheckResult(
            name="BCM device status",
            passed=True,
            message=f"Status: {state}",
            severity="warning"
        )
    
    def check_cumulus_mode(self) -> CheckResult:
        """Check if cumulusmode is set to MANUAL."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; get cumulusmode' 2>/dev/null"
        )
        mode = out.strip().upper()
        
        if 'MANUAL' in mode:
            return CheckResult(
                name="Cumulus mode",
                passed=True,
                message="cumulusmode is MANUAL (monitoring-only)"
            )
        return CheckResult(
            name="Cumulus mode",
            passed=False,
            message=f"cumulusmode is {mode}, expected MANUAL",
            details="Run deploy script to set monitoring-only mode",
            severity="warning"
        )
    
    def check_ztp_disabled(self) -> CheckResult:
        """Check if ZTP 'run on each boot' is disabled in BCM."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; ztpsettings; "
            f"get runztponeachboot' 2>/dev/null"
        )
        
        if 'no' in out.lower():
            return CheckResult(
                name="BCM ZTP setting",
                passed=True,
                message="ZTP 'run on each boot' is disabled"
            )
        return CheckResult(
            name="BCM ZTP setting",
            passed=False,
            message="ZTP 'run on each boot' is enabled",
            details="This may cause unexpected reboots",
            severity="warning"
        )
    
    def check_has_client_daemon(self) -> CheckResult:
        """Check if hasclientdaemon is set."""
        rc, out, err = run_cmd(
            f"cmsh -c 'device; use {self.hostname}; get hasclientdaemon' 2>/dev/null"
        )
        
        if 'yes' in out.lower():
            return CheckResult(
                name="Has client daemon",
                passed=True,
                message="hasclientdaemon is set"
            )
        return CheckResult(
            name="Has client daemon",
            passed=False,
            message="hasclientdaemon is NOT set",
            severity="warning"
        )
    
    def check_network_assignment(self) -> CheckResult:
        """Check if device is assigned to a network."""
        network = self.device_info.get('network', '')
        
        if network and network != 'globalnet':
            return CheckResult(
                name="Network assignment",
                passed=True,
                message=f"Assigned to network: {network}"
            )
        return CheckResult(
            name="Network assignment",
            passed=False if not network else True,
            message=f"Network: {network or 'not assigned'}",
            severity="warning" if network == 'globalnet' else "error"
        )
    
    def run_all_checks(self) -> List[CheckResult]:
        """Run all BCM device checks."""
        return [
            self.check_device_exists(),
            self.check_device_status(),
            self.check_cumulus_mode(),
            self.check_ztp_disabled(),
            self.check_has_client_daemon(),
            self.check_network_assignment(),
        ]


# ============================================================================
# Switch-Side Checks
# ============================================================================

class SwitchValidator:
    """Validates switch-side configuration and state."""
    
    def __init__(self, ip: str, username: str, password: str, 
                 expected_hostname: str = "", verbose: bool = False):
        self.ip = ip
        self.username = username
        self.password = password
        self.expected_hostname = expected_hostname
        self.verbose = verbose
    
    def check_ssh_connectivity(self) -> CheckResult:
        """Check if we can SSH to the switch."""
        success, out = run_ssh_cmd(self.ip, "echo 'SSH_OK'", 
                                   self.username, self.password)
        if success and 'SSH_OK' in out:
            return CheckResult(
                name="SSH connectivity",
                passed=True,
                message=f"SSH connection successful"
            )
        return CheckResult(
            name="SSH connectivity",
            passed=False,
            message="SSH connection failed",
            details=out,
            severity="critical"
        )
    
    def check_hostname(self) -> CheckResult:
        """Check if hostname matches expected."""
        success, out = run_ssh_cmd(self.ip, "hostname", 
                                   self.username, self.password)
        if not success:
            return CheckResult(
                name="Hostname check",
                passed=False,
                message="Could not retrieve hostname",
                severity="error"
            )
        
        actual = out.strip()
        if not self.expected_hostname:
            return CheckResult(
                name="Hostname check",
                passed=True,
                message=f"Hostname: {actual}"
            )
        
        if actual == self.expected_hostname:
            return CheckResult(
                name="Hostname check",
                passed=True,
                message=f"Hostname matches: {actual}"
            )
        return CheckResult(
            name="Hostname check",
            passed=False,
            message=f"Hostname mismatch: expected '{self.expected_hostname}', got '{actual}'",
            severity="warning"
        )
    
    def check_cm_lite_daemon_installed(self) -> CheckResult:
        """Check if cm-lite-daemon is installed."""
        success, out = run_ssh_cmd(
            self.ip, "ls /opt/cm-lite-daemon/cm-lite-daemon 2>/dev/null",
            self.username, self.password
        )
        if success and '/opt/cm-lite-daemon' in out:
            return CheckResult(
                name="cm-lite-daemon installed",
                passed=True,
                message="cm-lite-daemon is installed"
            )
        return CheckResult(
            name="cm-lite-daemon installed",
            passed=False,
            message="cm-lite-daemon is NOT installed",
            severity="critical"
        )
    
    def check_cm_lite_daemon_running(self) -> CheckResult:
        """Check if cm-lite-daemon service is running."""
        success, out = run_ssh_cmd(
            self.ip, "systemctl is-active cm-lite-daemon",
            self.username, self.password
        )
        if success and out.strip() == 'active':
            return CheckResult(
                name="cm-lite-daemon running",
                passed=True,
                message="cm-lite-daemon service is active"
            )
        return CheckResult(
            name="cm-lite-daemon running",
            passed=False,
            message=f"cm-lite-daemon service status: {out.strip()}",
            severity="error"
        )
    
    def check_cm_lite_daemon_enabled(self) -> CheckResult:
        """Check if cm-lite-daemon is enabled to start at boot."""
        success, out = run_ssh_cmd(
            self.ip, "systemctl is-enabled cm-lite-daemon",
            self.username, self.password
        )
        if success and out.strip() == 'enabled':
            return CheckResult(
                name="cm-lite-daemon enabled",
                passed=True,
                message="cm-lite-daemon is enabled at boot"
            )
        return CheckResult(
            name="cm-lite-daemon enabled",
            passed=False,
            message=f"cm-lite-daemon is NOT enabled at boot",
            severity="warning"
        )
    
    def check_ztp_disabled(self) -> CheckResult:
        """Check if ZTP is disabled on the switch."""
        success, out = run_ssh_cmd(
            self.ip, "systemctl is-active ztp.service 2>/dev/null; "
                     "systemctl is-enabled ztp.service 2>/dev/null",
            self.username, self.password
        )
        
        out_lower = out.lower()
        if 'inactive' in out_lower or 'disabled' in out_lower:
            return CheckResult(
                name="Switch ZTP disabled",
                passed=True,
                message="ZTP service is disabled"
            )
        
        if 'active' in out_lower:
            return CheckResult(
                name="Switch ZTP disabled",
                passed=False,
                message="ZTP service is ACTIVE",
                details="Run: sudo ztp --disable",
                severity="warning"
            )
        
        return CheckResult(
            name="Switch ZTP disabled",
            passed=True,
            message=f"ZTP status: {out.strip()}",
            severity="info"
        )
    
    def check_pip_packages(self) -> CheckResult:
        """Check if required pip packages are installed."""
        success, out = run_ssh_cmd(
            self.ip, "pip3 list 2>/dev/null",
            self.username, self.password
        )
        
        if not success:
            return CheckResult(
                name="Pip packages",
                passed=False,
                message="Could not check pip packages",
                severity="warning"
            )
        
        missing = []
        for pkg in REQUIRED_PIP_PACKAGES:
            if pkg.lower() not in out.lower():
                missing.append(pkg)
        
        if not missing:
            return CheckResult(
                name="Pip packages",
                passed=True,
                message=f"All required packages installed: {', '.join(REQUIRED_PIP_PACKAGES)}"
            )
        return CheckResult(
            name="Pip packages",
            passed=False,
            message=f"Missing packages: {', '.join(missing)}",
            severity="error"
        )
    
    def check_cm_lite_config(self) -> CheckResult:
        """Check cm-lite-daemon configuration."""
        success, out = run_ssh_cmd(
            self.ip, 
            "sudo -S cat /opt/cm-lite-daemon/etc/config.json 2>/dev/null",
            self.username, self.password
        )
        
        if not success or not out:
            return CheckResult(
                name="cm-lite-daemon config",
                passed=False,
                message="Could not read config.json",
                severity="warning"
            )
        
        try:
            config = json.loads(out)
            host = config.get('host', 'not set')
            port = config.get('port', 'not set')
            return CheckResult(
                name="cm-lite-daemon config",
                passed=True,
                message=f"Configured for BCM: {host}:{port}",
                details=json.dumps(config, indent=2) if self.verbose else None
            )
        except json.JSONDecodeError:
            return CheckResult(
                name="cm-lite-daemon config",
                passed=False,
                message="Invalid config.json",
                details=out[:200],
                severity="error"
            )
    
    def check_bcm_connectivity(self) -> CheckResult:
        """Check if switch can reach BCM head node."""
        # Get BCM IP from config
        success, out = run_ssh_cmd(
            self.ip,
            "sudo -S cat /opt/cm-lite-daemon/etc/config.json 2>/dev/null",
            self.username, self.password
        )
        
        bcm_ip = None
        try:
            config = json.loads(out)
            bcm_ip = config.get('host')
        except:
            pass
        
        if not bcm_ip:
            return CheckResult(
                name="BCM connectivity",
                passed=False,
                message="Could not determine BCM IP from config",
                severity="warning"
            )
        
        # Try ping with mgmt VRF
        success, out = run_ssh_cmd(
            self.ip,
            f"ping -I mgmt -c 1 -W 2 {bcm_ip} 2>&1",
            self.username, self.password
        )
        
        if success and '1 received' in out:
            return CheckResult(
                name="BCM connectivity",
                passed=True,
                message=f"Can reach BCM at {bcm_ip}"
            )
        return CheckResult(
            name="BCM connectivity",
            passed=False,
            message=f"Cannot reach BCM at {bcm_ip}",
            details=out[:200],
            severity="error"
        )
    
    def check_daemon_logs(self) -> CheckResult:
        """Check cm-lite-daemon logs for errors."""
        success, out = run_ssh_cmd(
            self.ip,
            "sudo -S journalctl -u cm-lite-daemon -n 50 --no-pager 2>/dev/null",
            self.username, self.password, timeout=60
        )
        
        if not success:
            return CheckResult(
                name="Daemon logs",
                passed=True,
                message="Could not retrieve logs (may require higher privileges)",
                severity="info"
            )
        
        error_lines = []
        for line in out.split('\n'):
            line_lower = line.lower()
            if any(p in line_lower for p in ['error', 'fail', 'exception']):
                error_lines.append(line)
        
        if not error_lines:
            return CheckResult(
                name="Daemon logs",
                passed=True,
                message="No errors in recent cm-lite-daemon logs"
            )
        deduped = dedupe_lines_with_counts(error_lines, limit=12 if self.verbose else 6)
        detail_lines = []
        for cnt, msg in deduped:
            prefix = f"({cnt}x) " if cnt > 1 else ""
            detail_lines.append(prefix + msg)
        return CheckResult(
            name="Daemon logs",
            passed=False,
            message=f"Found {len(error_lines)} error(s) in daemon logs",
            details='\n'.join(detail_lines),
            severity="warning"
        )
    
    def run_all_checks(self) -> List[CheckResult]:
        """Run all switch-side checks."""
        plan: List[Tuple[str, callable]] = [
            ("SSH connectivity", self.check_ssh_connectivity),
            ("Hostname check", self.check_hostname),
            ("cm-lite-daemon installed", self.check_cm_lite_daemon_installed),
            ("cm-lite-daemon running", self.check_cm_lite_daemon_running),
            ("cm-lite-daemon enabled", self.check_cm_lite_daemon_enabled),
            ("Switch ZTP disabled", self.check_ztp_disabled),
            ("Pip packages", self.check_pip_packages),
            ("cm-lite-daemon config", self.check_cm_lite_config),
            ("BCM connectivity", self.check_bcm_connectivity),
            ("Daemon logs", self.check_daemon_logs),
        ]

        checks: List[CheckResult] = []
        total = len(plan)
        for idx, (label, fn) in enumerate(plan, 1):
            # Always show minimal progress so it doesn't look stalled.
            print(f"    [{idx}/{total}] {label}...", flush=True)
            res = fn()
            checks.append(res)

            # Only continue if SSH works
            if label == "SSH connectivity" and not res.passed:
                return checks

        return checks


# ============================================================================
# Report Generation
# ============================================================================

def generate_report(report: ValidationReport, as_json: bool = False) -> str:
    """Generate human-readable or JSON report."""
    
    if as_json:
        # Convert dataclasses to dicts
        report_dict = asdict(report)
        return json.dumps(report_dict, indent=2)
    
    lines = []
    lines.append("=" * 80)
    lines.append("BCM SWITCH DEPLOYMENT VALIDATION REPORT")
    lines.append("=" * 80)
    lines.append(f"Timestamp: {report.timestamp}")
    lines.append(f"Duration: {report.duration_seconds:.1f} seconds")
    lines.append("")
    
    # Summary
    total_switches = len(report.switches)
    passed_switches = sum(1 for s in report.switches if s.overall_passed)
    
    lines.append("-" * 80)
    lines.append("SUMMARY")
    lines.append("-" * 80)
    lines.append(f"Total switches validated: {total_switches}")
    lines.append(f"Passed: {passed_switches}")
    lines.append(f"Failed: {total_switches - passed_switches}")
    
    if report.summary:
        lines.append(f"\nBCM System: {'OK' if report.summary.get('bcm_ok', False) else 'ISSUES DETECTED'}")
        lines.append(f"Total checks: {report.summary.get('total_checks', 0)}")
        lines.append(f"Passed: {report.summary.get('passed_checks', 0)}")
        lines.append(f"Failed: {report.summary.get('failed_checks', 0)}")
    lines.append("")
    
    # BCM System Checks
    if report.bcm_system_checks:
        lines.append("-" * 80)
        lines.append("BCM SYSTEM CHECKS")
        lines.append("-" * 80)
        for check in report.bcm_system_checks:
            status = "✓" if check.passed else "✗"
            lines.append(f"  {status} {check.name}: {check.message}")
            if check.details and not check.passed:
                for detail_line in check.details.split('\n')[:3]:
                    lines.append(f"      {detail_line}")
        lines.append("")
    
    # Per-switch results
    for switch in report.switches:
        lines.append("-" * 80)
        status = "PASSED" if switch.overall_passed else "FAILED"
        lines.append(f"SWITCH: {switch.hostname} ({switch.ip}) - {status}")
        lines.append("-" * 80)
        
        if switch.bcm_checks:
            lines.append("  BCM Checks:")
            for check in switch.bcm_checks:
                status_char = "✓" if check.passed else "✗"
                lines.append(f"    {status_char} {check.name}: {check.message}")
        
        if switch.switch_checks:
            lines.append("  Switch Checks:")
            for check in switch.switch_checks:
                status_char = "✓" if check.passed else "✗"
                lines.append(f"    {status_char} {check.name}: {check.message}")
                if check.details and not check.passed:
                    detail = (check.details or "").strip()
                    if detail:
                        # Print multi-line details with basic truncation per line.
                        for dl in detail.splitlines()[:20]:
                            lines.append(f"        {dl[:300]}")
        
        lines.append("")
    
    # Recommendations
    failed_checks = []
    for switch in report.switches:
        for check in switch.bcm_checks + switch.switch_checks:
            if not check.passed and check.severity in ['error', 'critical']:
                failed_checks.append((switch.hostname, check))
    
    if failed_checks:
        lines.append("-" * 80)
        lines.append("RECOMMENDED ACTIONS")
        lines.append("-" * 80)
        for hostname, check in failed_checks[:10]:
            lines.append(f"  • {hostname}: {check.name}")
            if check.details:
                detail = (check.details or "").strip()
                if detail:
                    first = detail.splitlines()[0]
                    lines.append(f"    → {first[:200]}")
        lines.append("")
    
    lines.append("=" * 80)
    overall = "PASSED" if all(s.overall_passed for s in report.switches) else "FAILED"
    lines.append(f"OVERALL RESULT: {overall}")
    lines.append("=" * 80)
    
    return '\n'.join(lines)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Validate BCM switch deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                        # Full validation of all switches
  %(prog)s --csv .configs/from-dhcp.csv
  %(prog)s --switch 192.168.200.166
  %(prog)s --bcm-only             # Only check BCM side
  %(prog)s --verbose --json       # Detailed JSON output
        """
    )
    
    parser.add_argument("--csv", type=str,
                       help="CSV file with switches to validate")
    parser.add_argument("--switch", type=str,
                       help="Single switch IP to validate")
    parser.add_argument("--username", type=str, default=DEFAULT_USERNAME,
                       help="SSH username")
    parser.add_argument("--password", type=str,
                       help="SSH password (prompted if not provided)")
    parser.add_argument("--bcm-only", action="store_true",
                       help="Only run BCM-side checks")
    parser.add_argument("--switch-only", action="store_true",
                       help="Only run switch-side checks")
    parser.add_argument("--json", action="store_true",
                       help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed output")
    parser.add_argument("--quiet", "-q", action="store_true",
                       help="Only show summary")
    
    args = parser.parse_args()
    
    import time
    start_time = time.time()
    
    # Get password (prefer CLI, then repo config, else prompt/error)
    password = args.password
    if not password:
        config = load_config()
        password = (config.get('password') or "").strip()
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass("Enter SSH password: ")
        else:
            print("Error: SSH password not provided and no password found in .configs/config.json.")
            print("       Provide --password or run interactively to be prompted.")
            sys.exit(1)
    
    # Determine switches to validate
    switches = []
    
    if args.switch:
        switches = [{'ip': args.switch, 'hostname': '', 'mac': ''}]
    elif args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Error: CSV file not found: {csv_path}")
            sys.exit(1)
        switches = read_csv_file(csv_path)
    else:
        # Try to get switches from BCM
        rc, out, err = run_cmd("cmsh -c 'device; list' 2>/dev/null")
        if rc == 0:
            for line in out.split('\n'):
                if 'Switch' in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        hostname = parts[1]
                        mac = parts[2]
                        ip = parts[3]
                        switches.append({
                            'hostname': hostname,
                            'ip': ip,
                            'mac': mac
                        })
    
    if not switches and not args.bcm_only:
        print("No switches found to validate.")
        print("Use --csv, --switch, or add switches to BCM first.")
        sys.exit(1)
    
    # Create report
    report = ValidationReport()
    
    # BCM System Checks
    if not args.switch_only:
        print("Running BCM system checks...")
        bcm_validator = BCMSystemValidator(verbose=args.verbose)
        report.bcm_system_checks = bcm_validator.run_all_checks()
    
    # Per-switch validation
    if not args.bcm_only:
        print(f"\nValidating {len(switches)} switch(es)...")
        
        for i, switch_info in enumerate(switches, 1):
            ip = switch_info['ip']
            hostname = switch_info.get('hostname', ip)
            mac = switch_info.get('mac', '')
            
            print(f"\n[{i}/{len(switches)}] {hostname} ({ip})...")
            if args.verbose:
                print("  (verbose) Running checks; this can take a bit if SSH/sudo commands are slow...", flush=True)
            
            switch_result = SwitchValidation(
                hostname=hostname,
                ip=ip,
                mac=mac
            )
            
            # BCM device checks
            if not args.switch_only and hostname:
                bcm_device = BCMDeviceValidator(hostname, verbose=args.verbose)
                switch_result.bcm_checks = bcm_device.run_all_checks()
            
            # Switch-side checks
            switch_validator = SwitchValidator(
                ip, args.username, password, 
                expected_hostname=hostname,
                verbose=args.verbose
            )
            switch_result.switch_checks = switch_validator.run_all_checks()
            
            # Determine overall pass/fail
            all_checks = switch_result.bcm_checks + switch_result.switch_checks
            critical_failures = [
                c for c in all_checks 
                if not c.passed and c.severity in ['error', 'critical']
            ]
            switch_result.overall_passed = len(critical_failures) == 0
            
            report.switches.append(switch_result)
            
            # Quick status
            if not args.quiet:
                status = "✓" if switch_result.overall_passed else "✗"
                print(f"  {status} {hostname}: ", end="")
                failed = [c.name for c in all_checks if not c.passed]
                if failed:
                    print(f"Issues: {', '.join(failed[:3])}")
                else:
                    print("All checks passed")
    
    # Calculate summary
    total_checks = len(report.bcm_system_checks)
    passed_checks = sum(1 for c in report.bcm_system_checks if c.passed)
    
    for switch in report.switches:
        all_checks = switch.bcm_checks + switch.switch_checks
        total_checks += len(all_checks)
        passed_checks += sum(1 for c in all_checks if c.passed)
    
    report.summary = {
        'bcm_ok': all(c.passed for c in report.bcm_system_checks),
        'total_switches': len(report.switches),
        'passed_switches': sum(1 for s in report.switches if s.overall_passed),
        'total_checks': total_checks,
        'passed_checks': passed_checks,
        'failed_checks': total_checks - passed_checks,
    }
    
    report.duration_seconds = time.time() - start_time
    
    # Output
    print("\n")
    output = generate_report(report, as_json=args.json)
    print(output)
    
    # Exit code
    if report.summary.get('failed_checks', 0) > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

