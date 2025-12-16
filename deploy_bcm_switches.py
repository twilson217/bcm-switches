#!/usr/bin/env python3
"""
BCM Switch Deployment Script

A comprehensive tool for deploying Cumulus switches to BCM (Base Command Manager).
This script automates the entire process from switch discovery to full deployment.

The script uses a "partially airgapped" approach:
- Required files (cm-lite-daemon, pip packages) are downloaded once to .files/
- Files are then distributed to switches via local network (no switch internet access)
- If .files/ already exists (e.g., from prep-airgapped.py), those files are used

Usage:
    python3 deploy_bcm_switches.py              # Auto-downloads files if needed
    python3 deploy_bcm_switches.py --resume     # Resume from previous progress
    python3 deploy_bcm_switches.py --dry-run    # Show what would be done
"""

import argparse
import csv
import getpass
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_DIR = SCRIPT_DIR / ".configs"
CONFIG_FILE = CONFIG_DIR / "config.json"
CSV_FILE = CONFIG_DIR / "bcm_switches.csv"
FILES_DIR = SCRIPT_DIR / ".files"
CM_LITE_ZIP_PATH = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")

# Default progress structure
DEFAULT_PROGRESS = {
    'phase': 'discovery',
    'completed_ips': [],
    'failed_ips': [],
    'devices': []
}


class IPAddressParser:
    """Parse IP addresses from various formats."""
    
    @staticmethod
    def parse(ip_string: str) -> List[str]:
        """
        Parse IP addresses from a string supporting multiple formats:
        - Single IP: 192.168.0.1
        - Range with last octet: 192.168.0.1-100
        - Full range: 192.168.0.1-192.168.0.100
        - Comma-separated: 192.168.0.1, 192.168.0.2
        - Mixed: 192.168.0.1-10,192.168.2.1-10
        """
        ips = []
        # Split by comma, handling spaces
        parts = [p.strip() for p in ip_string.replace(' ', '').split(',')]
        
        for part in parts:
            if not part:
                continue
            
            if '-' in part:
                ips.extend(IPAddressParser._parse_range(part))
            else:
                # Single IP
                try:
                    ipaddress.ip_address(part)
                    ips.append(part)
                except ValueError:
                    print(f"Warning: Invalid IP address '{part}', skipping")
        
        return ips
    
    @staticmethod
    def _parse_range(range_str: str) -> List[str]:
        """Parse an IP range."""
        ips = []
        parts = range_str.split('-')
        
        if len(parts) != 2:
            print(f"Warning: Invalid range format '{range_str}', skipping")
            return ips
        
        start_str, end_str = parts
        
        try:
            # Check if end is a full IP or just the last octet
            if '.' in end_str:
                # Full range: 192.168.0.1-192.168.0.100
                start_ip = ipaddress.ip_address(start_str)
                end_ip = ipaddress.ip_address(end_str)
            else:
                # Last octet range: 192.168.0.1-100
                start_ip = ipaddress.ip_address(start_str)
                # Extract base and replace last octet
                base_parts = start_str.rsplit('.', 1)
                end_ip = ipaddress.ip_address(f"{base_parts[0]}.{end_str}")
            
            # Generate all IPs in range
            current = int(start_ip)
            end = int(end_ip)
            
            if current > end:
                current, end = end, current
            
            while current <= end:
                ips.append(str(ipaddress.ip_address(current)))
                current += 1
                
        except ValueError as e:
            print(f"Warning: Error parsing range '{range_str}': {e}")
        
        return ips


class ConfigManager:
    """Manage configuration and progress tracking in a single file."""
    
    PHASES = ['discovery', 'bcm_add', 'transfer', 'install', 'register', 'complete']
    
    def __init__(self):
        self.config = {}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> bool:
        """Load configuration from file. Returns True if config exists."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.config = json.load(f)
                # Ensure progress structure exists
                if 'progress' not in self.config:
                    self.config['progress'] = DEFAULT_PROGRESS.copy()
                return True
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Error loading config: {e}")
        return False
    
    def save(self):
        """Save configuration to file."""
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get(self, key: str, default=None):
        """Get a configuration value."""
        return self.config.get(key, default)
    
    def set(self, key: str, value):
        """Set a configuration value."""
        self.config[key] = value
    
    # Progress tracking methods
    @property
    def progress(self) -> Dict:
        """Get the progress sub-object."""
        if 'progress' not in self.config:
            self.config['progress'] = DEFAULT_PROGRESS.copy()
        return self.config['progress']
    
    def has_progress(self) -> bool:
        """Check if there is existing progress to resume."""
        return ('progress' in self.config and 
                (self.config['progress'].get('completed_ips') or 
                 self.config['progress'].get('devices')))
    
    def clear_progress(self):
        """Clear progress data (keeps config settings)."""
        self.config['progress'] = DEFAULT_PROGRESS.copy()
        self.save()
    
    def set_phase(self, phase: str):
        """Set current deployment phase."""
        self.progress['phase'] = phase
        self.save()
    
    def mark_ip_completed(self, ip: str):
        """Mark an IP as completed."""
        if ip not in self.progress['completed_ips']:
            self.progress['completed_ips'].append(ip)
        self.save()
    
    def mark_ip_failed(self, ip: str):
        """Mark an IP as failed."""
        if ip not in self.progress['failed_ips']:
            self.progress['failed_ips'].append(ip)
        self.save()
    
    def add_device(self, device: Dict):
        """Add a discovered device."""
        # Check if device already exists (by IP)
        for i, d in enumerate(self.progress['devices']):
            if d['ip'] == device['ip']:
                self.progress['devices'][i] = device
                self.save()
                return
        self.progress['devices'].append(device)
        self.save()
    
    def get_remaining_ips(self, all_ips: List[str]) -> List[str]:
        """Get IPs that haven't been processed."""
        completed = set(self.progress['completed_ips'])
        failed = set(self.progress['failed_ips'])
        return [ip for ip in all_ips if ip not in completed and ip not in failed]
    
    def prompt_for_config(self, use_existing: bool = False):
        """Prompt user for configuration values. Saves after each answer."""
        if use_existing:
            print("\nUpdating configuration. Press Enter to keep existing values.\n")
        else:
            print("\nNo configuration found. Let's set things up.\n")
        
        # IP Addresses
        current_ips = self.get('switch_ips', [])
        if use_existing and current_ips:
            print(f"Current switch IPs: {', '.join(current_ips[:5])}{'...' if len(current_ips) > 5 else ''}")
            print(f"  ({len(current_ips)} total IPs)")
            ip_prompt = "Enter switch IP addresses (formats: 192.168.0.1-100, 192.168.0.1-192.168.0.100,\n  or comma-separated, or combinations) [Enter to keep]: "
        else:
            ip_prompt = "Enter switch IP addresses (formats: 192.168.0.1-100, 192.168.0.1-192.168.0.100,\n  or comma-separated, or combinations): "
        
        ip_input = input(ip_prompt).strip()
        
        if ip_input:
            self.config['switch_ips'] = IPAddressParser.parse(ip_input)
            print(f"  Parsed {len(self.config['switch_ips'])} IP address(es)")
            self.save()  # Save after IP addresses
        elif not current_ips:
            print("Error: At least one IP address is required.")
            sys.exit(1)
        
        # Username
        current_user = self.get('username', 'cumulus')
        if use_existing:
            print(f"\nCurrent SSH username: {current_user}")
        user_input = input(f"Enter SSH username for switches [{current_user}]: ").strip()
        self.config['username'] = user_input if user_input else current_user
        self.save()  # Save after username
        
        # Password
        current_password = self.get('password')
        if use_existing and current_password:
            print("\nCurrent password: *******")
            password = getpass.getpass("Enter SSH password for switches [Enter to keep]: ")
        else:
            password = getpass.getpass("Enter SSH password for switches: ")
        
        if password:
            self.config['password'] = password
            self.save()  # Save after password
        elif not current_password:
            print("Error: Password is required.")
            sys.exit(1)


def run_connectivity_test(config: ConfigManager) -> Optional[str]:
    """Run connectivity test on all switches and auto-detect VRF.
    
    Returns:
        Detected VRF name if consistent across all switches, None if user cancelled
    """
    switch_ips = config.get('switch_ips', [])
    username = config.get('username', 'cumulus')
    password = config.get('password', '')
    
    if not switch_ips:
        print("No switch IPs configured.")
        return None
    
    print("\n" + "=" * 60)
    print("CONNECTIVITY TEST & VRF DETECTION")
    print("=" * 60)
    
    discovery = SwitchDiscovery(username, password)
    
    results = []  # List of (ip, success, vrf)
    vrf_counts = {}  # Track VRF occurrences
    
    for i, ip in enumerate(switch_ips, 1):
        print(f"\n[{i}/{len(switch_ips)}] Testing {ip}...")
        success, vrf = discovery.test_connectivity_and_vrf(ip)
        
        if success:
            if vrf:
                print(f"  Connection Success! VRF is '{vrf}'")
                vrf_counts[vrf] = vrf_counts.get(vrf, 0) + 1
            else:
                print(f"  Connection Success! Could not determine VRF")
                vrf = "unknown"
                vrf_counts[vrf] = vrf_counts.get(vrf, 0) + 1
        else:
            print(f"  Connection Failed!")
            vrf = None
        
        results.append((ip, success, vrf))
    
    # Summarize results
    print("\n" + "-" * 60)
    print("CONNECTIVITY TEST RESULTS")
    print("-" * 60)
    
    successful = [(ip, vrf) for ip, success, vrf in results if success]
    failed = [ip for ip, success, vrf in results if not success]
    
    print(f"\nSuccessful connections: {len(successful)}/{len(switch_ips)}")
    if failed:
        print(f"Failed connections: {len(failed)}")
        for ip in failed:
            print(f"  - {ip}")
    
    if not successful:
        print("\n✗ No successful connections. Please check network and credentials.")
        return None
    
    # Analyze VRF consistency
    detected_vrfs = {vrf for ip, vrf in successful if vrf and vrf != "unknown"}
    unknown_count = sum(1 for ip, vrf in successful if vrf == "unknown")
    
    print(f"\nVRF Detection:")
    for vrf, count in sorted(vrf_counts.items(), key=lambda x: -x[1]):
        if vrf != "unknown":
            print(f"  - '{vrf}': {count} switch(es)")
    if unknown_count:
        print(f"  - Could not detect: {unknown_count} switch(es)")
    
    # Handle different scenarios
    if len(detected_vrfs) == 0:
        # Couldn't detect any VRF
        print("\n⚠ Could not automatically detect VRF on any switch.")
        vrf_input = input("Enter VRF to use [mgmt]: ").strip()
        return vrf_input if vrf_input else "mgmt"
    
    elif len(detected_vrfs) == 1:
        # All switches have the same VRF - perfect!
        detected_vrf = list(detected_vrfs)[0]
        print(f"\n✓ All switches are using VRF: '{detected_vrf}'")
        response = input(f"Press Enter to confirm using '{detected_vrf}', or type a different VRF: ").strip()
        return response if response else detected_vrf
    
    else:
        # VRF mismatch detected!
        print("\n" + "!" * 60)
        print("⚠ VRF MISMATCH DETECTED!")
        print("!" * 60)
        print("\nDifferent VRFs were detected across your switches:")
        for vrf in detected_vrfs:
            ips_with_vrf = [ip for ip, v in successful if v == vrf]
            print(f"\n  VRF '{vrf}':")
            for ip in ips_with_vrf:
                print(f"    - {ip}")
        
        print("\nThis may indicate a configuration issue that should be addressed")
        print("before proceeding with deployment.")
        print("\nOptions:")
        print("  1) Stop now and address the VRF mismatch")
        print("  2) Proceed anyway with a VRF I'll enter manually")
        
        while True:
            choice = input("\nSelect option (1 or 2): ").strip()
            if choice == "1":
                print("\nExiting. Please ensure all switches use the same VRF.")
                return None
            elif choice == "2":
                vrf_input = input("Enter VRF to use: ").strip()
                if vrf_input:
                    return vrf_input
                print("VRF cannot be empty.")
            else:
                print("Please enter 1 or 2.")


def run_auth_check(username: str, password: str, devices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Verify we can SSH to each device using the provided username/password.

    This catches password drift early (e.g., defaults were changed but deploy is running
    with an old password), before rsync/install steps.
    """
    discovery = SwitchDiscovery(username, password)
    reachable: List[Dict] = []
    unreachable: List[Dict] = []

    for device in devices:
        ip = device.get("ip")
        if not ip:
            unreachable.append(device)
            continue
        try:
            ok = discovery.check_connectivity(ip)
        except Exception:
            ok = False
        (reachable if ok else unreachable).append(device)

    return reachable, unreachable


class NetworkDetector:
    """Detect BCM networks and match switch IPs."""
    
    def __init__(self):
        self.networks = []
    
    def detect_networks(self) -> List[Dict]:
        """Get available BCM networks using cmsh."""
        try:
            result = subprocess.run(
                ["cmsh", "-c", "network; list"],
                capture_output=True, text=True, check=True
            )
            self.networks = self._parse_network_list(result.stdout)
            
            if self.networks:
                print(f"  Found {len(self.networks)} network(s):")
                for net in self.networks:
                    print(f"    - {net['name']}: {net['base_address']}/{net['netmask_bits']}")
            else:
                # Debug: show raw output if parsing failed
                print("  Warning: Could not parse network list")
                if result.stdout.strip():
                    print("  Raw output from cmsh:")
                    for line in result.stdout.strip().split('\n')[:10]:
                        print(f"    {line}")
            
            return self.networks
        except subprocess.CalledProcessError as e:
            print(f"Error detecting networks: {e}")
            if e.stderr:
                print(f"  stderr: {e.stderr}")
            return []
        except FileNotFoundError:
            print("Error: cmsh command not found. Make sure you're running on a BCM system.")
            return []
    
    def _parse_network_list(self, output: str) -> List[Dict]:
        """Parse cmsh network list output.
        
        Note: When cmsh runs with -c flag, it outputs data without headers.
        Format: Name Type NetmaskBits BaseAddress DomainName IPv6
        """
        networks = []
        lines = output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip header lines if present (interactive mode)
            if line.startswith('Name') or '--' in line and line.count('-') > 10:
                continue
            
            # Parse the line - columns are: Name, Type, Netmask bits, Base address, Domain name, IPv6
            # Use split() which handles multiple whitespace
            parts = line.split()
            if len(parts) >= 4:
                try:
                    name = parts[0]
                    net_type = parts[1]
                    netmask_bits = int(parts[2])
                    base_address = parts[3]
                    
                    # Validate that base_address looks like an IP
                    if '.' in base_address and netmask_bits >= 0 and netmask_bits <= 32:
                        networks.append({
                            'name': name,
                            'type': net_type,
                            'netmask_bits': netmask_bits,
                            'base_address': base_address
                        })
                except (ValueError, IndexError):
                    continue
        
        return networks
    
    def match_ip_to_network(self, ip: str) -> Optional[str]:
        """Find which network an IP belongs to.
        
        Returns the most specific matching network (highest netmask bits).
        """
        try:
            ip_obj = ipaddress.ip_address(ip)
            
            matches = []
            for network in self.networks:
                try:
                    # Create network object from base address and netmask
                    net = ipaddress.ip_network(
                        f"{network['base_address']}/{network['netmask_bits']}",
                        strict=False
                    )
                    if ip_obj in net:
                        matches.append((network['netmask_bits'], network['name']))
                except ValueError:
                    continue
            
            if matches:
                # Sort by netmask bits descending (most specific first)
                matches.sort(reverse=True)
                return matches[0][1]
                
        except ValueError:
            pass
        
        return None
    
    def detect_network_for_ips(self, ips: List[str]) -> Optional[str]:
        """Detect which network matches the given IPs."""
        if not self.networks:
            self.detect_networks()
        
        # Try to match the first IP
        for ip in ips:
            network = self.match_ip_to_network(ip)
            if network:
                return network
        
        return None
    
    def prompt_for_network(self, suggested_network: Optional[str], ips: List[str]) -> str:
        """Prompt user to confirm or select network."""
        if suggested_network:
            print(f"\nBased on the IP addresses entered, the correct network appears to be: {suggested_network}")
            response = input("Is this correct? (y/n): ").strip().lower()
            if response in ['y', 'yes', '']:
                return suggested_network
        else:
            print("\nCould not automatically determine the correct network for the switch IPs.")
        
        # Show available networks
        print("\nAvailable BCM networks:")
        for i, network in enumerate(self.networks, 1):
            print(f"  {i}) {network['name']} ({network['type']}) - {network['base_address']}/{network['netmask_bits']}")
        
        while True:
            try:
                choice = input("\nSelect network by number: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(self.networks):
                    return self.networks[idx]['name']
                print("Invalid selection. Please try again.")
            except ValueError:
                print("Please enter a number.")


class SwitchDiscovery:
    """Discover switch information via SSH."""
    
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
    
    def _run_ssh_command(self, host: str, command: str) -> Optional[str]:
        """Run a command on a remote host via SSH.
        
        Tries SSH key auth first, falls back to password auth via sshpass.
        """
        ssh_opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=yes"  # Fail fast if key auth doesn't work
        ]
        
        # First try without password (SSH key auth)
        ssh_cmd = ["ssh"] + ssh_opts + [f"{self.username}@{host}", command]
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        
        # Fall back to password auth via sshpass
        if self.password:
            ssh_opts_pwd = [
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=15",
                "-o", "PubkeyAuthentication=no"  # Force password auth
            ]
            ssh_cmd = ["sshpass", "-p", self.password, "ssh"] + ssh_opts_pwd + [
                f"{self.username}@{host}", command
            ]
            try:
                result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return result.stdout.strip()
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
        
        return None
    
    def get_hostname(self, ip: str) -> Optional[str]:
        """Get hostname from a switch."""
        hostname = self._run_ssh_command(ip, "hostname")
        if hostname:
            return hostname.strip()
        
        # Fallback: try reading /etc/hostname
        hostname = self._run_ssh_command(ip, "cat /etc/hostname")
        if hostname:
            return hostname.strip()
        
        return None
    
    def get_mac_address(self, ip: str) -> Optional[str]:
        """Get MAC address from a switch."""
        # Try to get the MAC of the management interface
        # First, find the interface with our IP
        result = self._run_ssh_command(ip, f"ip -o addr show | grep '{ip}' | awk '{{print $2}}'")
        if result:
            interface = result.strip()
            # Get MAC of that interface
            mac_result = self._run_ssh_command(ip, f"cat /sys/class/net/{interface}/address")
            if mac_result:
                return mac_result.strip().upper()
        
        # Fallback: try eth0
        mac_result = self._run_ssh_command(ip, "cat /sys/class/net/eth0/address")
        if mac_result:
            return mac_result.strip().upper()
        
        # Fallback: use ip link
        result = self._run_ssh_command(ip, "ip link show eth0 | grep ether | awk '{print $2}'")
        if result:
            return result.strip().upper()
        
        return None
    
    def check_connectivity(self, ip: str) -> bool:
        """Check if switch is reachable via SSH."""
        # Use SSH instead of ping since ICMP may be blocked
        result = self._run_ssh_command(ip, "echo ok")
        return result == "ok"
    
    def check_ztp_status(self, ip: str) -> Optional[str]:
        """Check ZTP status on a switch.
        
        Returns:
            'enabled', 'disabled', or None if unable to check
        """
        # Run: sudo ztp -s | grep -i service
        # Need to use sudo, so we need to handle password
        ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"
        cmd = f"sshpass -p '{self.password}' ssh {ssh_opts} {self.username}@{ip} " \
              f"'echo {self.password} | sudo -S ztp -s 2>/dev/null | grep -i service'"
        
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout.lower()
            
            if 'disabled' in output:
                return 'disabled'
            elif 'enabled' in output:
                return 'enabled'
            else:
                return None
        except:
            return None
    
    def get_vrf_for_ip(self, host: str, target_ip: str) -> Optional[str]:
        """Detect which VRF an IP address belongs to on a switch.
        
        Args:
            host: The switch to SSH into
            target_ip: The IP address to find the VRF for
        
        Returns:
            VRF name, empty string if no VRF (default), or None if detection failed
        """
        # First, find which interface has this IP
        result = self._run_ssh_command(host, f"ip -o addr show | grep '{target_ip}/'")
        if not result:
            return None
        
        # Parse out the interface name (second field)
        parts = result.split()
        if len(parts) < 2:
            return None
        interface = parts[1]
        
        # Now check if this interface is in a VRF
        # Method 1: Check ip link for "master" (VRF membership)
        link_result = self._run_ssh_command(host, f"ip link show {interface}")
        if link_result:
            # Look for "master <vrf_name>" in output
            import re
            match = re.search(r'master\s+(\S+)', link_result)
            if match:
                vrf_name = match.group(1)
                # Verify it's actually a VRF (not a bridge or bond)
                vrf_check = self._run_ssh_command(host, f"ip link show {vrf_name} type vrf")
                if vrf_check is not None:
                    return vrf_name
        
        # Method 2: Try NVUE command (Cumulus Linux 5.x+)
        nv_result = self._run_ssh_command(host, f"nv show interface {interface} | grep -E '^vrf\\s'")
        if nv_result:
            parts = nv_result.split()
            if len(parts) >= 2 and parts[1]:
                return parts[1]
        
        # Method 3: Check /sys/class/net for VRF
        vrf_result = self._run_ssh_command(host, f"cat /sys/class/net/{interface}/master/uevent 2>/dev/null | grep INTERFACE")
        if vrf_result and 'INTERFACE=' in vrf_result:
            vrf_name = vrf_result.split('=')[1].strip()
            return vrf_name
        
        # No VRF found - interface is in default VRF
        return "default"
    
    def test_connectivity_and_vrf(self, ip: str) -> Tuple[bool, Optional[str]]:
        """Test SSH connectivity and detect VRF for the given IP.
        
        Returns:
            Tuple of (connection_success, vrf_name)
        """
        # First check ping
        if not self.check_connectivity(ip):
            return (False, None)
        
        # Try SSH and get VRF
        vrf = self.get_vrf_for_ip(ip, ip)
        if vrf is None:
            # SSH might have failed even if ping worked
            # Try a simple SSH test
            test = self._run_ssh_command(ip, "echo ok")
            if test != "ok":
                return (False, None)
            # SSH works but couldn't determine VRF
            return (True, None)
        
        return (True, vrf)
    
    def discover_switch(self, ip: str) -> Optional[Dict]:
        """Discover all information about a switch."""
        print(f"  Connecting to {ip}...")
        
        # Check connectivity first
        if not self.check_connectivity(ip):
            print(f"    ✗ Switch not reachable")
            return None
        
        hostname = self.get_hostname(ip)
        if not hostname:
            print(f"    ✗ Could not get hostname")
            return None
        print(f"    - Hostname: {hostname}")
        
        mac = self.get_mac_address(ip)
        if not mac:
            print(f"    ✗ Could not get MAC address")
            return None
        print(f"    - MAC: {mac}")
        
        return {
            'hostname': hostname,
            'ip': ip,
            'mac': mac
        }


class BCMChecker:
    """Check BCM for existing devices and verify consistency."""
    
    def __init__(self):
        self.bcm_devices = {}  # Cache of BCM devices by IP
    
    def get_bcm_devices(self) -> Dict[str, Dict]:
        """Get all devices from BCM, indexed by IP."""
        if self.bcm_devices:
            return self.bcm_devices
        
        try:
            result = subprocess.run(
                ["cmsh", "-c", "device; list"],
                capture_output=True, text=True, check=True
            )
            self.bcm_devices = self._parse_device_list(result.stdout)
            return self.bcm_devices
        except subprocess.CalledProcessError as e:
            print(f"Warning: Could not query BCM devices: {e}")
            return {}
    
    def _parse_device_list(self, output: str) -> Dict[str, Dict]:
        """Parse cmsh device list output into a dict indexed by IP."""
        devices = {}
        lines = output.strip().split('\n')
        
        for line in lines:
            # Skip header and separator lines
            if not line.strip() or '--' in line or line.startswith('Type'):
                continue
            
            parts = line.split()
            if len(parts) >= 6:
                try:
                    dev_type = parts[0]
                    hostname = parts[1]
                    mac = parts[2].upper() if parts[2] != '00:00:00:00:00:00' else ''
                    # Category might be empty, so IP position varies
                    # Find the IP by looking for something that looks like an IP
                    ip = ''
                    network = ''
                    for i, part in enumerate(parts[3:], 3):
                        if '.' in part and part.count('.') == 3:
                            # Check if it looks like an IP
                            try:
                                ipaddress.ip_address(part)
                                ip = part
                                if i + 1 < len(parts):
                                    network = parts[i + 1]
                                break
                            except ValueError:
                                continue
                    
                    if ip and ip != '0.0.0.0':
                        devices[ip] = {
                            'type': dev_type,
                            'hostname': hostname,
                            'mac': mac,
                            'ip': ip,
                            'network': network
                        }
                except (ValueError, IndexError):
                    continue
        
        return devices
    
    def find_device_by_ip(self, ip: str) -> Optional[Dict]:
        """Check if a device with this IP exists in BCM."""
        devices = self.get_bcm_devices()
        return devices.get(ip)
    
    def find_device_by_mac(self, mac: str) -> Optional[Dict]:
        """Check if a device with this MAC exists in BCM."""
        devices = self.get_bcm_devices()
        mac_upper = mac.upper()
        for device in devices.values():
            if device.get('mac', '').upper() == mac_upper:
                return device
        return None
    
    def check_consistency(self, switch_data: Dict, bcm_data: Dict) -> Dict:
        """Compare switch data with BCM data and return differences."""
        differences = {}
        
        # Compare hostname
        if switch_data.get('hostname') != bcm_data.get('hostname'):
            differences['hostname'] = {
                'switch': switch_data.get('hostname'),
                'bcm': bcm_data.get('hostname')
            }
        
        # Compare MAC (case-insensitive)
        switch_mac = switch_data.get('mac', '').upper()
        bcm_mac = bcm_data.get('mac', '').upper()
        if switch_mac and bcm_mac and switch_mac != bcm_mac:
            differences['mac'] = {
                'switch': switch_mac,
                'bcm': bcm_mac
            }
        
        # Compare IP
        if switch_data.get('ip') != bcm_data.get('ip'):
            differences['ip'] = {
                'switch': switch_data.get('ip'),
                'bcm': bcm_data.get('ip')
            }
        
        return differences
    
    def refresh(self):
        """Clear cache and re-fetch BCM devices."""
        self.bcm_devices = {}
        self.get_bcm_devices()


class BCMDeployer:
    """Handle BCM deployment operations."""
    
    def __init__(self, username: str, password: str, vrf: str = "mgmt", 
                 dry_run: bool = False):
        self.username = username
        self.password = password
        self.vrf = vrf
        self.dry_run = dry_run
        self.bcm_master_ip = None
        self.ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    
    def get_bcm_master_ip(self) -> str:
        """Get BCM master IP."""
        if self.bcm_master_ip:
            return self.bcm_master_ip
        
        try:
            result = subprocess.run(
                ["cmsh", "-c", "device; use master; get ip"],
                capture_output=True, text=True, check=True
            )
            self.bcm_master_ip = result.stdout.strip()
            return self.bcm_master_ip
        except Exception as e:
            raise RuntimeError(f"Failed to get BCM master IP: {e}")
    
    def _run_ssh_command(self, host: str, command: str) -> Optional[str]:
        """Run SSH command on remote host."""
        ssh_cmd = ["sshpass", "-p", self.password, "ssh"] + self.ssh_opts + [
            f"{self.username}@{host}", command
        ]
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return None
    
    def check_daemon_installed(self, device: Dict) -> bool:
        """Check if cm-lite-daemon is already installed on the device."""
        result = self._run_ssh_command(device['ip'], "test -d /opt/cm-lite-daemon && echo yes")
        return result == "yes"
    
    def check_transfer_complete(self, device: Dict) -> bool:
        """Check if all required files have been transferred to the device."""
        # Check for both cm-lite-daemon.zip and pip_packages_dep directory
        result = self._run_ssh_command(device['ip'], 
            f"test -d /home/{self.username}/pip_packages_dep && "
            f"test -f /home/{self.username}/cm-lite-daemon.zip && echo yes")
        return result == "yes"
    
    def check_daemon_registered(self, device: Dict) -> bool:
        """Check if device is already registered with BCM (has certificates)."""
        result = self._run_ssh_command(device['ip'], 
            "test -f /opt/cm-lite-daemon/etc/bootstrap.key && echo yes")
        return result == "yes"
    
    def check_device_in_bcm(self, hostname: str) -> bool:
        """Check if device already exists in BCM by hostname."""
        try:
            result = subprocess.run(
                f"cmsh -c 'device; use {hostname}; get hostname'",
                shell=True, capture_output=True, text=True
            )
            return result.returncode == 0 and hostname in result.stdout
        except:
            return False
    
    def add_device_to_bcm(self, device: Dict, network: str, skip_if_exists: bool = True) -> bool:
        """Add a device to BCM.
        
        Args:
            device: Device info dict with hostname, ip, mac
            network: BCM network name
            skip_if_exists: If True, skip adding if device already configured correctly
        
        Returns:
            True if successful or already exists, False on error
        """
        hostname = device['hostname']
        
        if self.dry_run:
            print(f"    [DRY RUN] Would add {hostname} to BCM")
            return True
        
        # Check if device already exists in BCM
        if skip_if_exists and self.check_device_in_bcm(hostname):
            print(f"    ✓ Device {hostname} already exists in BCM, skipping add")
            return True
        
        # Try to add/update the device
        commands = [
            (f"cmsh -c 'device; add switch {hostname}; commit'", "Adding device"),
            (f"cmsh -c 'device; use {hostname}; set ip {device['ip']}; set mac {device['mac']}; "
             f"set network {network}; set hasclientdaemon yes; commit'", "Setting properties"),
            (f"cmsh -c 'device; use {hostname}; accesssettings; set username {self.username}; "
             f"set password {self.password}; set -e force true; commit'", "Setting access"),
            (f"cmsh -c 'device; use {hostname}; ztpsettings; set enableapi yes; commit'", "Setting ZTP"),
            (f"cmsh -c 'device; use {hostname}; initialize'", "Initializing")
        ]
        
        for cmd, description in commands:
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                # Some commands may "fail" if device already exists, that's OK
                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    # Ignore "already exists" type errors
                    if "already" in stderr.lower() or "exists" in stderr.lower():
                        continue
                    # Ignore uncommitted changes warning if command still worked
                    if "uncommitted" in stderr.lower():
                        continue
                    print(f"    ✗ {description} failed: {stderr}")
                    return False
            except Exception as e:
                print(f"    ✗ {description} error: {e}")
                return False
        
        return True
    
    def transfer_daemon(self, device: Dict, skip_if_exists: bool = True) -> bool:
        """Transfer cm-lite-daemon to a device."""
        if self.dry_run:
            print(f"    [DRY RUN] Would transfer daemon to {device['hostname']}")
            return True
        
        # Check if all required files are already on the device
        if skip_if_exists and self.check_transfer_complete(device):
            print(f"    ✓ Files already present on {device['hostname']}, skipping transfer")
            return True
        
        # Always use local files from .files/ directory (ensure_local_files() prepares them)
        local_zip = FILES_DIR / "cm-lite-daemon.zip"
        pip_packages_dir = FILES_DIR / "pip_packages_dep"
        
        if not local_zip.exists():
            print(f"    ✗ cm-lite-daemon.zip not found in {FILES_DIR}")
            print(f"      Run ensure_local_files() first or check your setup")
            return False
        
        if not pip_packages_dir.exists() or not list(pip_packages_dir.glob("*")):
            print(f"    ✗ pip_packages_dep not found or empty in {FILES_DIR}")
            print(f"      Run ensure_local_files() first or check your setup")
            return False
        
        try:
            
            # Transfer files via rsync
            ssh_opts = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            target = f"{self.username}@{device['ip']}:/home/{self.username}/"
            
            # Try rsync with SSH key first, fall back to sshpass
            def run_rsync(source: str, dest: str, description: str) -> bool:
                import time
                start_time = time.time()
                
                # rsync options: -a (archive), -v (verbose), -z (compress), 
                # --info=progress2 shows overall progress percentage
                rsync_opts = ["-avz", "--info=progress2", "--human-readable"]
                
                # First try without password (SSH key auth)
                rsync_cmd = ["rsync"] + rsync_opts + ["-e", f"{ssh_opts} -o BatchMode=yes",
                            source, dest]
                print(f"      {description}...")
                result = subprocess.run(rsync_cmd, text=True)
                
                if result.returncode == 0:
                    elapsed = time.time() - start_time
                    print(f"      Completed in {elapsed:.1f}s")
                    return True
                
                # Fall back to password auth
                if self.password:
                    rsync_cmd = ["sshpass", "-p", self.password, "rsync"] + rsync_opts + [
                                "-e", ssh_opts, source, dest]
                    result = subprocess.run(rsync_cmd, text=True)
                    if result.returncode == 0:
                        elapsed = time.time() - start_time
                        print(f"      Completed in {elapsed:.1f}s")
                        return True
                return False
            
            if not run_rsync(str(local_zip), target, "Transferring cm-lite-daemon.zip"):
                print(f"    ✗ Failed to transfer cm-lite-daemon.zip")
                return False
            
            # pip packages need to go to pip_packages_dep/ subdirectory
            pip_target = f"{self.username}@{device['ip']}:/home/{self.username}/pip_packages_dep/"
            if not run_rsync(str(pip_packages_dir) + "/", pip_target, "Transferring pip packages"):
                print(f"    ✗ Failed to transfer pip_packages_dep")
                return False
            
            return True
            
        except Exception as e:
            print(f"    ✗ Transfer failed: {e}")
            return False
    
    def install_daemon(self, device: Dict, skip_if_exists: bool = True) -> bool:
        """Install cm-lite-daemon on a device."""
        import time
        
        if self.dry_run:
            print(f"    [DRY RUN] Would install daemon on {device['hostname']}")
            return True
        
        # Check if already installed and has required Python packages
        if skip_if_exists and self.check_daemon_installed(device):
            # Verify Python deps are installed by checking if we can import a key module
            check_result = self._run_ssh_command(device['ip'], 
                "python3 -c 'import websocket' 2>/dev/null && echo ok")
            if check_result == "ok":
                print(f"    ✓ cm-lite-daemon already installed on {device['hostname']}, skipping")
                return True
            else:
                print(f"    Daemon directory exists but deps may be missing, re-installing deps...")
        
        ssh_base = ["sshpass", "-p", self.password, "ssh",
                   "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   f"{self.username}@{device['ip']}"]
        
        # Commands with descriptions for progress feedback
        # Note: Using apt-get instead of apt for stable CLI interface in scripts
        steps = [
            ("Killing stale apt processes...", "sudo killall apt apt-get 2>/dev/null; sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock 2>/dev/null; sleep 2; echo done"),
            ("Updating package lists...", "sudo apt-get update -q"),
            ("Installing build dependencies...", "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q build-essential python3-dev python3-pip unzip"),
            ("Extracting cm-lite-daemon...", f"cd /home/{self.username} && unzip -o cm-lite-daemon.zip"),
            ("Copying to /opt/...", f"sudo cp -r /home/{self.username}/cm-lite-daemon /opt/"),
            ("Installing Python packages (this may take a while)...", 
             f"cd /opt/cm-lite-daemon && sudo pip3 install --break-system-packages --no-index "
             f"--find-links /home/{self.username}/pip_packages_dep -r requirements.txt"),
            ("Cleaning up...", f"rm -f /home/{self.username}/cm-lite-daemon.zip"),
        ]
        
        total_steps = len(steps)
        overall_start = time.time()
        
        for i, (description, cmd) in enumerate(steps, 1):
            print(f"      [{i}/{total_steps}] {description}", end="", flush=True)
            step_start = time.time()
            
            try:
                # Handle sudo password
                if "sudo " in cmd:
                    full_cmd = ssh_base + [cmd.replace("sudo ", "sudo -S ", 1)]
                    result = subprocess.run(full_cmd, input=f"{self.password}\n",
                                          capture_output=True, text=True, timeout=600)
                else:
                    result = subprocess.run(ssh_base + [cmd], capture_output=True, text=True, timeout=300)
                
                step_elapsed = time.time() - step_start
                
                if result.returncode != 0 and "already" not in result.stderr.lower():
                    # Some non-critical errors are OK
                    if "unzip" in cmd or "rm " in cmd:
                        print(f" skipped ({step_elapsed:.1f}s)")
                        continue
                    print(f" FAILED ({step_elapsed:.1f}s)")
                    # Surface useful error output. SSH banners/warnings can pollute stderr/stdout,
                    # so print the tail of both to help pinpoint the real failure (pip, sudo, etc.).
                    stderr_tail = (result.stderr or "").strip()[-1200:]
                    stdout_tail = (result.stdout or "").strip()[-1200:]
                    if stderr_tail:
                        print("        stderr (tail):")
                        for line in stderr_tail.splitlines()[-20:]:
                            print(f"          {line}")
                    if stdout_tail:
                        print("        stdout (tail):")
                        for line in stdout_tail.splitlines()[-20:]:
                            print(f"          {line}")
                    return False
                
                print(f" done ({step_elapsed:.1f}s)")
                
            except subprocess.TimeoutExpired:
                print(f" TIMEOUT")
                return False
            except Exception as e:
                print(f" ERROR: {e}")
                return False
        
        total_elapsed = time.time() - overall_start
        print(f"      Total install time: {total_elapsed:.1f}s")
        
        return True
    
    def register_device(self, device: Dict, skip_if_exists: bool = True) -> bool:
        """Register device with BCM and transfer bootstrap certs."""
        if self.dry_run:
            print(f"    [DRY RUN] Would register {device['hostname']} with BCM")
            return True
        
        # Check if already registered
        if skip_if_exists and self.check_daemon_registered(device):
            print(f"    ✓ {device['hostname']} already registered with BCM, skipping")
            return True
        
        hostname = device['hostname']
        bcm_master_ip = self.get_bcm_master_ip()
        
        # Wait for bootstrap files
        bootstrap_dir = Path(f"/cm/local/apps/cmd/etc/htdocs/switch/{hostname}")
        bootstrap_key = bootstrap_dir / "bootstrap.key"
        bootstrap_pem = bootstrap_dir / "bootstrap.pem"
        
        print(f"    Waiting for bootstrap certificates...")
        max_wait = 120
        waited = 0
        while waited < max_wait:
            if bootstrap_key.exists() and bootstrap_pem.exists():
                break
            time.sleep(5)
            waited += 5
        
        if not bootstrap_key.exists() or not bootstrap_pem.exists():
            print(f"    ✗ Bootstrap files not generated after {max_wait}s")
            return False
        
        # Transfer bootstrap files
        scp_base = ["sshpass", "-p", self.password, "scp",
                   "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        ssh_base = ["sshpass", "-p", self.password, "ssh",
                   "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   f"{self.username}@{device['ip']}"]
        
        target = f"{self.username}@{device['ip']}:/home/{self.username}/"
        
        try:
            subprocess.run(scp_base + [str(bootstrap_key), target], check=True, capture_output=True)
            subprocess.run(scp_base + [str(bootstrap_pem), target], check=True, capture_output=True)
            
            # Move to correct location
            cmds = [
                "sudo mkdir -p /opt/cm-lite-daemon/etc",
                f"sudo mv /home/{self.username}/bootstrap.key /opt/cm-lite-daemon/etc/",
                f"sudo mv /home/{self.username}/bootstrap.pem /opt/cm-lite-daemon/etc/",
                "sudo chown root:root /opt/cm-lite-daemon/etc/bootstrap.*",
                "sudo chmod 600 /opt/cm-lite-daemon/etc/bootstrap.key",
                "sudo chmod 644 /opt/cm-lite-daemon/etc/bootstrap.pem"
            ]
            
            for cmd in cmds:
                full_cmd = ssh_base + [cmd.replace("sudo ", "sudo -S ", 1)]
                subprocess.run(full_cmd, input=f"{self.password}\n",
                             capture_output=True, text=True, timeout=60)
            
            # Register with BCM
            print(f"    Registering with BCM...")
            register_cmd = (f"cd /opt/cm-lite-daemon && sudo ./register_node "
                          f"--host {bcm_master_ip} --disable-cert-check --vrf {self.vrf}")
            full_cmd = ssh_base + [register_cmd.replace("sudo ", "sudo -S ", 1)]
            result = subprocess.run(full_cmd, input=f"{self.password}\n",
                         capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                print(f"    ⚠ Registration command returned non-zero, checking service...")
            
            # Start the service
            print(f"    Starting cm-lite-daemon service...")
            start_cmd = "sudo systemctl start cm-lite-daemon"
            full_cmd = ssh_base + [start_cmd.replace("sudo ", "sudo -S ", 1)]
            subprocess.run(full_cmd, input=f"{self.password}\n",
                         capture_output=True, text=True, timeout=60)
            
            # Wait a moment for service to start
            time.sleep(3)
            
            # Verify service is running
            print(f"    Verifying service status...")
            verify_cmd = "systemctl is-active cm-lite-daemon"
            full_cmd = ssh_base + [verify_cmd]
            result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=30)
            
            if result.stdout.strip() == "active":
                print(f"    ✓ cm-lite-daemon service is running")
                return True
            else:
                print(f"    ✗ Service not running: {result.stdout.strip()}")
                # Try to get more info
                status_cmd = "sudo systemctl status cm-lite-daemon --no-pager -l"
                full_cmd = ssh_base + [status_cmd.replace("sudo ", "sudo -S ", 1)]
                status_result = subprocess.run(full_cmd, input=f"{self.password}\n",
                                              capture_output=True, text=True, timeout=30)
                if status_result.stdout:
                    print(f"    Service status:\n{status_result.stdout[:500]}")
                return False
            
        except Exception as e:
            print(f"    ✗ Registration failed: {e}")
            return False


def configure_monitoring_only_mode(devices: List[Dict], dry_run: bool = False) -> bool:
    """Configure switches for monitoring-only mode in BCM.
    
    This sets:
    1. cumulusmode = manual (BCM won't push config to switches)
    2. runztponeachboot = no (ZTP won't run on every boot)
    
    The result is non-disruptive monitoring:
    - cm-lite-daemon provides metrics to BCM
    - BCM doesn't change anything on the switches
    - ZTP config remains in BCM for future disaster recovery use
    """
    if not devices:
        return True
    
    print("\nConfiguring switches for monitoring-only mode...")
    print("  - Setting cumulusmode to 'manual' (no auto-config push)")
    print("  - Disabling 'run ZTP on each boot' (no boot-time provisioning)")
    
    if dry_run:
        print("\n  [DRY RUN] Would configure all switches for monitoring-only mode")
        return True
    
    hostnames = [d.get('hostname', d.get('ip')) for d in devices]
    
    success_count = 0
    for hostname in hostnames:
        try:
            # Set cumulusmode to manual AND disable ZTP run on each boot
            # Using a single cmsh command for efficiency
            cmd = (f"cmsh -c \"device; use {hostname}; "
                   f"set cumulusmode manual; "
                   f"ztpsettings; set runztponeachboot no; "
                   f"exit; commit\"")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                print(f"  ✓ {hostname}: monitoring-only mode configured")
                success_count += 1
            else:
                print(f"  ✗ {hostname}: failed - {result.stderr.strip()}")
        except Exception as e:
            print(f"  ✗ {hostname}: error - {e}")
    
    print(f"\nConfigured {success_count}/{len(hostnames)} devices for monitoring-only mode")
    if success_count == len(hostnames):
        print("  ✓ All switches set to monitoring-only (no config changes on boot)")
    
    return success_count == len(hostnames)


def write_csv(devices: List[Dict], network: str):
    """Write devices to CSV file."""
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['Hostname', 'IP', 'MAC', 'Network'])
        writer.writeheader()
        for device in devices:
            writer.writerow({
                'Hostname': device['hostname'],
                'IP': device['ip'],
                'MAC': device['mac'],
                'Network': network
            })
    print(f"\nGenerated {CSV_FILE} with {len(devices)} devices.")

def read_devices_from_csv(csv_path: Path) -> List[Dict]:
    """Read devices from a CSV file.
    
    Expected columns: Hostname, IP, MAC, Network
    """
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)
    
    devices = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize field names
            device = {}
            for key, value in row.items():
                device[key.lower()] = value
            
            # Require IP
            if not device.get('ip'):
                continue
            
            devices.append({
                'hostname': device.get('hostname', ''),
                'ip': device['ip'],
                'mac': device.get('mac', '').upper() if device.get('mac') else '',
                'network': device.get('network', '')
            })
    
    return devices


def check_csv_conflicts_with_bcm(devices: List[Dict], bcm_checker: 'BCMChecker') -> List[Dict]:
    """Check for conflicts between CSV devices and existing BCM devices.
    
    Returns list of conflict dicts with details.
    """
    conflicts = []
    bcm_devices = bcm_checker.get_bcm_devices()
    
    for device in devices:
        ip = device['ip']
        mac = device.get('mac', '')
        hostname = device.get('hostname', '')
        
        # Check by IP
        if ip in bcm_devices:
            bcm_dev = bcm_devices[ip]
            conflict = {'device': device, 'bcm_device': bcm_dev, 'type': 'ip'}
            
            # Check if data matches
            if mac and bcm_dev.get('mac') and mac.upper() != bcm_dev['mac'].upper():
                conflict['mac_mismatch'] = True
            if hostname and bcm_dev.get('hostname') and hostname != bcm_dev['hostname']:
                conflict['hostname_mismatch'] = True
            
            if conflict.get('mac_mismatch') or conflict.get('hostname_mismatch'):
                conflicts.append(conflict)
        
        # Check by MAC (might be at different IP)
        if mac:
            for bcm_ip, bcm_dev in bcm_devices.items():
                if bcm_dev.get('mac') and bcm_dev['mac'].upper() == mac.upper():
                    if bcm_ip != ip:
                        conflicts.append({
                            'device': device,
                            'bcm_device': bcm_dev,
                            'type': 'mac',
                            'ip_mismatch': True
                        })
                        break
    
    return conflicts




def check_prerequisites():
    """Check that all prerequisites are met."""
    # Check for sshpass
    if not shutil.which("sshpass"):
        print("Error: sshpass is required. Install with: apt-get install sshpass")
        sys.exit(1)
    
    # Check for rsync
    if not shutil.which("rsync"):
        print("Error: rsync is required. Install with: apt-get install rsync")
        sys.exit(1)
    
    # Check for cmsh
    if not shutil.which("cmsh"):
        print("Error: cmsh not found. This script must run on a BCM system.")
        sys.exit(1)
    
    # Check for cm-lite-daemon.zip source
    if not CM_LITE_ZIP_PATH.exists() and not (FILES_DIR / "cm-lite-daemon.zip").exists():
        print(f"Error: cm-lite-daemon.zip not found at {CM_LITE_ZIP_PATH}")
        print("       and not found in {FILES_DIR}")
        sys.exit(1)


def _python_version_to_tags(py_version: str) -> Tuple[str, str]:
    """
    Convert '3.11' -> ('3.11', 'cp311'), '3.9' -> ('3.9', 'cp39'), '3.10' -> ('3.10', 'cp310').
    """
    parts = (py_version or "").strip().split(".")
    if len(parts) < 2:
        raise ValueError(f"Invalid python version '{py_version}' (expected MAJOR.MINOR)")
    major = int(parts[0])
    minor = int(parts[1])
    if major != 3:
        raise ValueError(f"Unsupported python major version '{major}' (expected 3.x)")
    abi = f"cp{major}{minor}" if minor < 10 else f"cp{major}{minor}"
    # For 3.9 this yields cp39, for 3.10 cp310, for 3.11 cp311.
    return f"{major}.{minor}", abi


def detect_switch_python_versions(username: str, password: str, devices: List[Dict]) -> List[str]:
    """
    Detect python3 MAJOR.MINOR versions on target switches.

    Returns a sorted list of unique versions (e.g. ['3.10', '3.11']).
    If detection fails for all devices, returns ['3.11'] as a conservative default.
    """
    discovery = SwitchDiscovery(username, password)
    found: set = set()
    for dev in devices:
        ip = dev.get("ip")
        if not ip:
            continue
        out = discovery._run_ssh_command(ip, "python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")' 2>/dev/null")
        if out:
            v = out.strip()
            if v and v[0].isdigit() and "." in v:
                found.add(v)
    if not found:
        return ["3.11"]
    return sorted(found)


def ensure_local_files(python_versions: Optional[List[str]] = None):
    """Ensure all required files are present in .files/ directory.
    
    Downloads files if not already present. This enables a 'partially airgapped'
    approach where BCM downloads once and distributes to switches locally.
    """
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    
    cm_lite_zip = FILES_DIR / "cm-lite-daemon.zip"
    pip_packages = FILES_DIR / "pip_packages_dep"
    
    # Default target if caller didn't detect versions.
    python_versions = python_versions or ["3.11"]

    # Check if files already exist (and are plausibly complete for the detected python versions).
    # A non-empty directory is not sufficient: we need wheels for each target ABI.
    needs_pip_rebuild = False
    if cm_lite_zip.exists() and pip_packages.exists():
        existing = list(pip_packages.glob("*"))
        if existing:
            missing = []
            for v in python_versions:
                try:
                    _, abi = _python_version_to_tags(v)
                except Exception:
                    missing.append(v)
                    continue
                if not list(pip_packages.glob(f"*{abi}*.whl")):
                    missing.append(v)
            if not missing:
                print(f"✓ Using cached files from {FILES_DIR}")
                return True
            print(f"⚠ Cached wheelhouse missing wheels for python version(s): {', '.join(missing)}; re-downloading")
            needs_pip_rebuild = True
    
    print(f"\nPreparing deployment files in {FILES_DIR}...")
    
    # Copy cm-lite-daemon.zip if not present
    if not cm_lite_zip.exists():
        if CM_LITE_ZIP_PATH.exists():
            print(f"  Copying cm-lite-daemon.zip from BCM...")
            shutil.copy2(CM_LITE_ZIP_PATH, cm_lite_zip)
            print(f"  ✓ Copied cm-lite-daemon.zip")
        else:
            print(f"  ✗ cm-lite-daemon.zip not found at {CM_LITE_ZIP_PATH}")
            return False
    
    # Extract requirements and download pip packages if not present OR cache is incomplete.
    if needs_pip_rebuild:
        # Clear out stale/incomplete wheelhouse contents so we don't accidentally re-use it.
        try:
            for p in pip_packages.glob("*"):
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
        except Exception:
            # Best-effort; we'll still attempt to download into the directory.
            pass

    if needs_pip_rebuild or (not pip_packages.exists()) or (not list(pip_packages.glob("*"))):
        pip_packages.mkdir(parents=True, exist_ok=True)
        
        # Extract requirements.txt from zip
        print(f"  Extracting requirements.txt...")
        requirements = None
        with zipfile.ZipFile(cm_lite_zip, 'r') as zip_ref:
            for filename in zip_ref.namelist():
                if filename.endswith('requirements.txt'):
                    with zip_ref.open(filename) as req_file:
                        requirements = req_file.read().decode('utf-8')
                        break
        
        if not requirements:
            print(f"  ✗ requirements.txt not found in cm-lite-daemon.zip")
            return False
        
        # Write temp requirements file
        temp_req = FILES_DIR / "requirements.txt"
        temp_req.write_text(requirements)
        
        try:
            # Download packages for Python 3.11 (Cumulus Linux default).
            #
            # We prefer wheels for offline installation, but a small number of requirements
            # (e.g. `uptime`) may only be available as sdists. To keep the wheelhouse usable,
            # we first download wheels-only for the target platform, then separately fetch
            # sdists for a small allowlist.
            print(f"  Downloading pip packages for python version(s): {', '.join(python_versions)} (wheels preferred)...")

            # Split requirements: download wheels for everything except packages we will allow as sdists.
            # Start with a small baseline allowlist; we will expand it automatically if pip reports
            # "no matching distribution found" for a package under the wheel constraints.
            sdist_allowlist = {"uptime"}

            def _pkg_name_from_req_line(line: str) -> Optional[str]:
                s = (line or "").strip()
                if not s or s.startswith("#"):
                    return None
                # Basic normalization: strip extras and version pins.
                return s.split("==", 1)[0].split("[", 1)[0].strip()

            def _write_filtered_requirements() -> Path:
                filtered_lines = []
                for line in requirements.splitlines():
                    pkg = _pkg_name_from_req_line(line)
                    if pkg and pkg in sdist_allowlist:
                        continue
                    filtered_lines.append(line)
                p = FILES_DIR / "requirements.filtered.txt"
                p.write_text("\n".join(filtered_lines).strip() + "\n")
                return p

            def _extract_missing_pkgs(stderr_text: str) -> List[str]:
                """
                Parse pip stderr for missing distribution messages and return package names.
                Examples:
                  - "No matching distribution found for netifaces"
                  - "Could not find a version that satisfies the requirement netifaces (from versions: none)"
                """
                missing: List[str] = []
                if not stderr_text:
                    return missing
                low = stderr_text.lower()
                # Pattern 1: explicit "No matching distribution found for X"
                for m in re.finditer(r"no matching distribution found for ([a-z0-9_.-]+)", low):
                    missing.append(m.group(1))
                # Pattern 2: "satisfies the requirement X"
                for m in re.finditer(r"satisfies the requirement ([a-z0-9_.-]+)", low):
                    missing.append(m.group(1))
                # De-dupe while preserving order
                out: List[str] = []
                for x in missing:
                    if x not in out:
                        out.append(x)
                return out

            results = []
            for v in python_versions:
                v_norm, abi = _python_version_to_tags(v)

                # Retry loop: expand sdist_allowlist based on pip errors, then retry wheel download.
                attempt = 0
                last = None
                while attempt < 3:
                    attempt += 1
                    filtered_req = _write_filtered_requirements()
                    base_cmd = [
                        "pip", "download",
                        "-r", str(filtered_req),
                        "--dest", str(pip_packages),
                        "--python-version", v_norm,
                        "--implementation", "cp",
                        "--abi", abi,
                        "--platform", "manylinux2014_x86_64",
                        "--only-binary", ":all:",
                        "--no-binary", ":none:",
                    ]
                    r = subprocess.run(base_cmd, capture_output=True, text=True, timeout=600)
                    last = r
                    if r.returncode == 0:
                        break

                    missing_pkgs = _extract_missing_pkgs(r.stderr or "")
                    # Add newly discovered missing packages to sdist allowlist and retry.
                    added = False
                    for pkg in missing_pkgs:
                        if pkg and pkg not in sdist_allowlist:
                            sdist_allowlist.add(pkg)
                            added = True
                    if not added:
                        break

                results.append((v_norm, abi, last))

            # Always fetch sdists for allowlisted packages (they were excluded from wheel download).
            for pkg in sorted(sdist_allowlist):
                print(f"  Downloading sdist for '{pkg}'...")
                sdist_cmd = ["pip", "download", "--no-binary", ":all:", "--no-deps", "--dest", str(pip_packages), pkg]
                subprocess.run(sdist_cmd, capture_output=True, text=True, timeout=300)

            # Count downloaded packages
            package_count = len(list(pip_packages.glob("*")))
            wheel_count = len(list(pip_packages.glob("*.whl")))

            if package_count == 0 or wheel_count == 0:
                print("  ✗ Failed to download required pip packages for offline install")
                print(f"    Downloaded files: {package_count} (wheels: {wheel_count})")
                for v, abi, r in results:
                    if r is not None and r.returncode != 0:
                        print(f"    pip download failed for python {v} ({abi})")
                        if r.stderr:
                            print(f"      stderr (first 500 chars): {r.stderr[:500]}")
                        if r.stdout:
                            print(f"      stdout (first 500 chars): {r.stdout[:500]}")
                return False

            # If the wheel download failed for reasons other than known sdists, still fail fast.
            if any((r is not None and r.returncode != 0) for _, _, r in results):
                print("  ✗ Failed to download required pip packages for offline install")
                for v, abi, r in results:
                    if r is not None and r.returncode != 0:
                        print(f"    pip download failed for python {v} ({abi})")
                        if r.stderr:
                            print(f"      stderr (first 500 chars): {r.stderr[:500]}")
                        if r.stdout:
                            print(f"      stdout (first 500 chars): {r.stdout[:500]}")
                return False

            print(f"  ✓ Downloaded {package_count} package files")

        finally:
            if 'filtered_req' in locals() and filtered_req.exists():
                filtered_req.unlink()
            # Clean up temp requirements
            if temp_req.exists():
                temp_req.unlink()
    
    return True


def get_bcm_switches() -> List[Dict]:
    """Get all switches currently in BCM.
    
    Returns list of dicts with: hostname, ip, mac, network, status
    """
    try:
        result = subprocess.run(
            ["cmsh", "-c", "device;list -t switch"],
            capture_output=True, text=True, check=True
        )
        return parse_bcm_switch_list(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error querying BCM switches: {e}")
        return []
    except FileNotFoundError:
        print("Error: cmsh command not found.")
        return []


def parse_bcm_switch_list(output: str) -> List[Dict]:
    """Parse 'cmsh device;list -t switch' output.
    
    Format:
    Type       Hostname (key)   MAC                Category  IP              Network        Status
    ---------- ---------------- ------------------ --------- --------------- -------------- --------
    Switch     leaf-01          48:B0:2D:3B:C8:E6            192.168.200.166 internalnet    [   UP   ]
    """
    switches = []
    lines = output.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip header lines
        if line.startswith('Type') or '--' in line and line.count('-') > 10:
            continue
        
        parts = line.split()
        if len(parts) < 5:
            continue
        
        # First part should be "Switch"
        if parts[0] != 'Switch':
            continue
        
        try:
            hostname = parts[1]
            mac = parts[2].upper() if ':' in parts[2] else ''
            
            # Find IP address (looks like x.x.x.x)
            ip = ''
            network = ''
            status = ''
            
            for i, part in enumerate(parts[3:], 3):
                if '.' in part and part.count('.') == 3:
                    try:
                        ipaddress.ip_address(part)
                        ip = part
                        # Network is usually the next field
                        if i + 1 < len(parts):
                            network = parts[i + 1]
                        break
                    except ValueError:
                        continue
            
            # Extract status (text between [ and ])
            status_match = re.search(r'\[\s*(\w+)\s*\]', line)
            if status_match:
                status = status_match.group(1)
            
            if ip:
                switches.append({
                    'hostname': hostname,
                    'ip': ip,
                    'mac': mac,
                    'network': network,
                    'status': status
                })
        except (ValueError, IndexError):
            continue
    
    return switches


def display_initial_menu() -> int:
    """Display the initial menu and return user's choice (1, 2, or 3)."""
    print("\n" + "=" * 70)
    print("What switches do you want to add?")
    print("=" * 70)
    print("""
  1) I want to provide the IP Addresses and let the script figure out
     the other info.

  2) I have prepared a CSV file, and I want to use that.

  3) I have already added the switches to BCM, and I just want the
     script to install cm-lite-daemon.
""")
    
    while True:
        try:
            choice = input("Select an option (1/2/3): ").strip()
            if choice in ['1', '2', '3']:
                return int(choice)
            print("Please enter 1, 2, or 3.")
        except (ValueError, EOFError):
            print("Please enter 1, 2, or 3.")


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Cumulus switches to BCM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      # Interactive mode (shows menu)
  %(prog)s --csv .configs/from-dhcp.csv  # Deploy from CSV file
  %(prog)s --from-bcm           # Install cm-lite-daemon on switches already in BCM
  %(prog)s --resume             # Resume from previous progress
  %(prog)s --retry-failed       # Retry only the previously failed devices
  %(prog)s --dry-run            # Show what would be done
  %(prog)s --connectivity-test  # Run connectivity test and VRF detection only
  
Non-interactive mode (for automation):
  %(prog)s --csv FILE --non-interactive --username cumulus --password PWD
  %(prog)s --from-bcm --non-interactive --username cumulus --password PWD
  %(prog)s --from-bcm --non-interactive --exclude-ips 192.168.1.1,192.168.1.2

Notes:
  Files are automatically downloaded to .files/ if not already present.
  For fully airgapped deployments, use scripts/prep-airgapped.py to create
  an archive that includes all dependencies.
        """
    )
    
    parser.add_argument("--resume", action="store_true",
                       help="Resume from previous progress")
    parser.add_argument("--retry-failed", action="store_true",
                       help="Retry only the previously failed devices")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without executing")
    parser.add_argument("--connectivity-test", action="store_true",
                       help="Run connectivity test and VRF auto-detection only")
    parser.add_argument("--csv", type=Path, metavar="FILE",
                       help="Use CSV file as source of truth for switch information")
    parser.add_argument("--from-bcm", action="store_true",
                       help="Install cm-lite-daemon on switches already added to BCM")
    parser.add_argument("--non-interactive", action="store_true",
                       help="Run without user prompts (uses defaults/config values)")
    parser.add_argument("--username", type=str, default=None,
                       help="SSH username for switches (for non-interactive mode)")
    parser.add_argument("--password", type=str, default=None,
                       help="SSH password for switches (for non-interactive mode)")
    parser.add_argument("--exclude-ips", type=str, default=None,
                       help="IPs to exclude, comma-separated (for --from-bcm)")
    
    args = parser.parse_args()
    
    print("=" * 70)
    print("BCM Switch Deployment Tool")
    print("=" * 70)
    
    # Check prerequisites
    check_prerequisites()
    
    # Initialize config manager
    config = ConfigManager()
    
    # Show initial menu if no specific mode selected
    # Skip menu for: --csv, --from-bcm, --resume, --retry-failed, --connectivity-test
    if not (args.csv or args.from_bcm or args.resume or args.retry_failed or args.connectivity_test):
        menu_choice = display_initial_menu()
        
        if menu_choice == 2:
            # User chose CSV mode - ask for path
            csv_path = input("\nEnter path to CSV file: ").strip()
            if not csv_path:
                print("Error: CSV path is required.")
                sys.exit(1)
            args.csv = Path(csv_path)
            if not args.csv.exists():
                print(f"Error: CSV file not found: {args.csv}")
                sys.exit(1)
        
        elif menu_choice == 3:
            # User chose from-BCM mode
            args.from_bcm = True
        
        # menu_choice == 1 continues with default flow

    # Handle --from-bcm mode (install on switches already in BCM)
    if args.from_bcm:
        print("\n" + "=" * 70)
        print("FROM-BCM Mode: Installing on switches already in BCM")
        print("=" * 70)
        
        # Get switches from BCM
        print("\nRetrieving switches from BCM...")
        bcm_switches = get_bcm_switches()
        
        if not bcm_switches:
            print("No switches found in BCM. Add switches first or use a different mode.")
            sys.exit(1)
        
        print(f"\nFound {len(bcm_switches)} switch(es) in BCM:")
        print("-" * 70)
        print(f"{'#':<4} {'Hostname':<16} {'IP':<16} {'Network':<15} {'Status':<10}")
        print("-" * 70)
        for i, sw in enumerate(bcm_switches, 1):
            print(f"{i:<4} {sw['hostname']:<16} {sw['ip']:<16} {sw['network']:<15} {sw['status']:<10}")
        print("-" * 70)
        
        # Handle exclusions
        if args.non_interactive:
            # Non-interactive: use --exclude-ips if provided
            if args.exclude_ips:
                exclude_set = set()
                parts = [p.strip() for p in args.exclude_ips.replace(' ', '').split(',')]
                
                for part in parts:
                    if not part:
                        continue
                    if '-' in part and '.' in part:
                        exclude_ips = IPAddressParser.parse(part)
                        exclude_set.update(exclude_ips)
                    elif '.' in part:
                        exclude_set.add(part)
                    else:
                        for sw in bcm_switches:
                            if sw['hostname'].lower() == part.lower():
                                exclude_set.add(sw['ip'])
                                break
                
                original_count = len(bcm_switches)
                bcm_switches = [sw for sw in bcm_switches if sw['ip'] not in exclude_set]
                excluded_count = original_count - len(bcm_switches)
                print(f"\nExcluded {excluded_count} switch(es). Proceeding with {len(bcm_switches)}.")
                
                if not bcm_switches:
                    print("No switches remaining after exclusions. Exiting.")
                    sys.exit(0)
        else:
            # Interactive: ask user
            response = input("\nWould you like to exclude any switches? (y/n) [n]: ").strip().lower()
            if response in ['y', 'yes']:
                print("\nEnter IP addresses or hostnames to exclude.")
                print("  - Comma-separated: 192.168.200.161, 192.168.200.162")
                print("  - IP range: 192.168.200.161-165")
                print("  - Hostnames: spine-01, spine-02")
                exclude_input = input("\nExclude: ").strip()
                
                if exclude_input:
                    # Parse exclusions
                    exclude_set = set()
                    parts = [p.strip() for p in exclude_input.replace(' ', '').split(',')]
                    
                    for part in parts:
                        if not part:
                            continue
                        
                        # Check if it's an IP range
                        if '-' in part and '.' in part:
                            exclude_ips = IPAddressParser.parse(part)
                            exclude_set.update(exclude_ips)
                        elif '.' in part:
                            # Single IP
                            exclude_set.add(part)
                        else:
                            # Hostname - find corresponding IP
                            for sw in bcm_switches:
                                if sw['hostname'].lower() == part.lower():
                                    exclude_set.add(sw['ip'])
                                    break
                    
                    # Filter switches
                    original_count = len(bcm_switches)
                    bcm_switches = [sw for sw in bcm_switches if sw['ip'] not in exclude_set]
                    excluded_count = original_count - len(bcm_switches)
                    print(f"\nExcluded {excluded_count} switch(es). Proceeding with {len(bcm_switches)}.")
                    
                    if not bcm_switches:
                        print("No switches remaining after exclusions. Exiting.")
                        sys.exit(0)
        
        # Get credentials
        print("\n" + "-" * 60)
        print("CREDENTIALS")
        print("-" * 60)
        
        if args.non_interactive:
            # Non-interactive: use command-line args or config
            config.load()
            username = args.username or config.get('username', 'cumulus')
            password = args.password or config.get('password', '')
            
            if not password:
                print("Error: Password required. Use --password or set in config.")
                sys.exit(1)
            
            print(f"\nUsing credentials: username={username}")
        else:
            # Interactive: prompt for credentials
            if config.load():
                current_user = config.get('username', 'cumulus')
                current_pass = config.get('password', '')
                print(f"\nExisting credentials found (username: {current_user})")
                response = input("Use existing credentials? (y/n) [y]: ").strip().lower()
                if response not in ['n', 'no']:
                    username = current_user
                    password = current_pass
                    print("Using existing credentials.")
                else:
                    username = input(f"Enter SSH username [{current_user}]: ").strip() or current_user
                    password = getpass.getpass("Enter SSH password: ")
            else:
                username = input("Enter SSH username [cumulus]: ").strip() or "cumulus"
                password = getpass.getpass("Enter SSH password: ")
        
        # Get VRF - use default or prompt
        vrf = config.get('vrf', 'default')
        if not vrf:
            vrf = 'default'
        print(f"\nUsing VRF: {vrf}")
        
        # Save config
        config.set('username', username)
        config.set('password', password)
        config.set('vrf', vrf)
        config.set('switch_ips', [sw['ip'] for sw in bcm_switches])
        config.save()
        
        # Convert bcm_switches to devices format
        devices = bcm_switches
        
        # Initialize deployer
        deployer = BCMDeployer(username, password, vrf, args.dry_run)
        
        # Detect switch python versions and ensure local files are ready for those versions.
        py_versions = detect_switch_python_versions(username, password, devices)
        if not ensure_local_files(py_versions):
            print("\nError: Failed to prepare deployment files. Exiting.")
            sys.exit(1)
        
        # Phase 3: Transfer daemon (skip phases 1 and 2)
        print("\n" + "=" * 70)
        print("PHASE 3: Transferring cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        failed_devices = []
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Transferring to {device['hostname']} ({device['ip']})...")
            if deployer.transfer_daemon(device):
                print(f"    ✓ Transfer complete")
                success_count += 1
            else:
                print(f"    ✗ Transfer failed")
                failed_count += 1
                failed_devices.append(device['hostname'])
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed transfer: {', '.join(failed_devices)}")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        # Phase 4: Install daemon
        print("\n" + "=" * 70)
        print("PHASE 4: Installing cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        failed_devices = []
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Installing on {device['hostname']} ({device['ip']})...")
            if deployer.install_daemon(device):
                print(f"    ✓ Installation complete")
                success_count += 1
            else:
                print(f"    ✗ Installation failed")
                failed_count += 1
                failed_devices.append(device['hostname'])
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed installation: {', '.join(failed_devices)}")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        # Phase 5: Register with BCM
        print("\n" + "=" * 70)
        print("PHASE 5: Registering devices with BCM")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        failed_devices = []
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Registering {device['hostname']} ({device['ip']})...")
            if deployer.register_device(device):
                print(f"    ✓ Registration complete")
                success_count += 1
            else:
                print(f"    ✗ Registration failed")
                failed_count += 1
                failed_devices.append(device['hostname'])
        
        # Phase 6: Configure monitoring-only mode
        print("\n" + "=" * 70)
        print("PHASE 6: Configuring for monitoring-only mode")
        print("=" * 70)
        
        configure_monitoring_only_mode(devices, dry_run=args.dry_run)
        
        # Summary
        print("\n" + "=" * 70)
        print("DEPLOYMENT SUMMARY (from-BCM mode)")
        print("=" * 70)
        
        print(f"\nTotal devices processed: {len(devices)}")
        if failed_devices:
            print(f"Failed devices: {', '.join(failed_devices)}")
        else:
            print("All devices completed successfully!")
        
        if not args.dry_run:
            print("\nNext steps:")
            print("1. Verify cm-lite-daemon service status on devices")
            print("2. Check BCM for device connectivity")
            print("3. Monitor logs for any issues")
        
        # Exit non-zero if any devices failed. We may still proceed through later phases
        # (especially in non-interactive mode), but callers (e.g., test-loop) need an
        # accurate success/failure signal.
        sys.exit(0 if failed_count == 0 else 1)

    # Handle CSV mode
    if args.csv:
        print(f"\nCSV Mode: Using {args.csv} as source of truth")
        
        # Read devices from CSV
        csv_devices = read_devices_from_csv(args.csv)
        if not csv_devices:
            print("Error: No valid devices found in CSV file.")
            sys.exit(1)
        
        print(f"  Found {len(csv_devices)} device(s) in CSV")
        for dev in csv_devices:
            hostname = dev['hostname'] if dev['hostname'] else "(no hostname)"
            print(f"    - {dev['ip']:16} {hostname}")
        
        # Check for conflicts with BCM
        print("\nChecking for conflicts with existing BCM devices...")
        bcm_checker = BCMChecker()
        conflicts = check_csv_conflicts_with_bcm(csv_devices, bcm_checker)
        
        if conflicts:
            print("\n" + "!" * 60)
            print("CONFLICTS DETECTED WITH EXISTING BCM DEVICES")
            print("!" * 60)
            
            for conflict in conflicts:
                csv_dev = conflict['device']
                bcm_dev = conflict['bcm_device']
                print(f"\n  Device: {csv_dev.get('hostname') or csv_dev['ip']}")
                print(f"    CSV:  IP={csv_dev['ip']}, MAC={csv_dev.get('mac', 'N/A')}")
                print(f"    BCM:  IP={bcm_dev['ip']}, MAC={bcm_dev.get('mac', 'N/A')}, " +
                      f"Hostname={bcm_dev.get('hostname', 'N/A')}")
                
                if conflict.get('mac_mismatch'):
                    print(f"    !! MAC address mismatch!")
                if conflict.get('hostname_mismatch'):
                    print(f"    !! Hostname mismatch!")
                if conflict.get('ip_mismatch'):
                    print(f"    !! Same MAC exists with different IP in BCM!")
            
            print("\n" + "!" * 60)
            print("Please resolve these conflicts before proceeding:")
            print("  1. Update the CSV file to match BCM")
            print("  2. Update BCM to match the CSV")
            print("  3. Remove conflicting devices from CSV")
            print("!" * 60)
            sys.exit(1)
        
        print("  No conflicts found with BCM")
        
        # Get credentials
        print("\n" + "-" * 60)
        print("CREDENTIALS")
        print("-" * 60)
        
        if args.non_interactive:
            # Non-interactive: use command-line args or config
            config.load()
            username = args.username or config.get('username', 'cumulus')
            password = args.password or config.get('password', '')
            
            if not password:
                print("Error: Password required. Use --password or set in config.")
                sys.exit(1)
            
            print(f"\nUsing credentials: username={username}")
        else:
            # Interactive: prompt for credentials
            if config.load():
                current_user = config.get('username', 'cumulus')
                current_pass = config.get('password', '')
                print(f"\nExisting credentials found (username: {current_user})")
                response = input("Use existing credentials? (y/n) [y]: ").strip().lower()
                if response not in ['n', 'no']:
                    username = current_user
                    password = current_pass
                    print("Using existing credentials.")
                else:
                    username = input(f"Enter SSH username [{current_user}]: ").strip() or current_user
                    password = getpass.getpass("Enter SSH password: ")
            else:
                username = input("Enter SSH username [cumulus]: ").strip() or "cumulus"
                password = getpass.getpass("Enter SSH password: ")
        
        # Determine network from CSV or detect
        networks_in_csv = set(d['network'] for d in csv_devices if d.get('network'))
        if len(networks_in_csv) == 1:
            network = list(networks_in_csv)[0]
            print(f"\nUsing network from CSV: {network}")
        elif len(networks_in_csv) > 1:
            print(f"\nMultiple networks found in CSV: {', '.join(networks_in_csv)}")
            print("Devices will be added to their respective networks.")
            network = None  # Will use per-device network
        else:
            # No network in CSV, detect it
            print("\nNo network specified in CSV. Detecting...")
            network_detector = NetworkDetector()
            network_detector.detect_networks()
            ips = [d['ip'] for d in csv_devices]
            suggested = network_detector.detect_network_for_ips(ips)
            
            if args.non_interactive:
                # Non-interactive: use the suggested network or first available
                network = suggested or (network_detector.networks[0]['name'] if network_detector.networks else 'internalnet')
                print(f"  Auto-selected network: {network}")
            else:
                network = network_detector.prompt_for_network(suggested, ips)
            
            # Apply to all devices
            for d in csv_devices:
                d['network'] = network
        
        # Connectivity/auth test
        if args.non_interactive:
            # In non-interactive mode we still MUST validate SSH auth to avoid late rsync/install failures.
            print("\nTesting SSH authentication to devices...")
            reachable, unreachable = run_auth_check(username, password, csv_devices)
            if unreachable:
                print(f"\n✗ {len(unreachable)} device(s) failed SSH authentication or are unreachable:")
                for dev in unreachable:
                    hn = dev.get("hostname") or dev.get("ip")
                    print(f"  - {hn} ({dev.get('ip')})")
                print("\nFix credentials/state and re-run. (Tip: ensure your setup step changed passwords consistently.)")
                sys.exit(1)
            print(f"✓ SSH authentication OK for {len(reachable)}/{len(csv_devices)} devices")
        else:
            print("\n" + "-" * 60)
            response = input("Would you like to test connectivity to the devices? (y/n) [y]: ").strip().lower()
            if response not in ['n', 'no']:
                print("\nTesting connectivity...")
                discovery = SwitchDiscovery(username, password)
                reachable = []
                unreachable = []
                
                for i, device in enumerate(csv_devices, 1):
                    ip = device['ip']
                    hostname = device['hostname'] if device['hostname'] else ip
                    print(f"  [{i}/{len(csv_devices)}] {hostname} ({ip})...", end=" ", flush=True)
                    
                    if discovery.check_connectivity(ip):
                        print("reachable")
                        reachable.append(device)
                    else:
                        print("UNREACHABLE")
                        unreachable.append(device)
                
                if unreachable:
                    print(f"\n{len(unreachable)} device(s) are unreachable:")
                    for dev in unreachable:
                        print(f"    - {dev['ip']} ({dev.get('hostname', '')})")
                    response = input("\nContinue with reachable devices only? (y/n) [n]: ").strip().lower()
                    if response not in ['y', 'yes']:
                        print("Exiting.")
                        sys.exit(1)
                    csv_devices = reachable
                    if not csv_devices:
                        print("No reachable devices. Exiting.")
                        sys.exit(1)
        
        # Save config
        config.set('username', username)
        config.set('password', password)
        config.set('switch_ips', [d['ip'] for d in csv_devices])
        if network:
            config.set('network', network)
        
        # VRF - use default if not set
        vrf = config.get('vrf', 'default')
        if not vrf:
            vrf = 'default'
        config.set('vrf', vrf)
        
        # Set devices directly (skip discovery phase)
        config.progress['devices'] = csv_devices
        config.progress['phase'] = 'bcm_add'
        config.save()
        
        print("\nConfiguration saved. Proceeding to deployment...")
        print(f"Using VRF: {vrf}")
        if network:
            print(f"Using network: {network}")
        
        # Set variables for rest of script
        switch_ips = [d['ip'] for d in csv_devices]
        devices = csv_devices
        
        # Jump to phase 2 (bcm_add)
        deployer = BCMDeployer(username, password, vrf, args.dry_run)
        
        # Phase 2: Add to BCM
        print("\n" + "=" * 70)
        print("PHASE 2: Adding devices to BCM")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            dev_network = device.get('network') or network
            print(f"\n[{i}/{len(devices)}] Adding {device.get('hostname') or device['ip']} to BCM...")
            if deployer.add_device_to_bcm(device, dev_network):
                print(f"    Added to BCM")
                success_count += 1
            else:
                print(f"    Failed to add to BCM")
                failed_count += 1
                config.mark_ip_failed(device['ip'])
        
        if failed_count > 0:
            print(f"\n{failed_count} device(s) failed to add to BCM.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('transfer')
        
        # Continue to phase 3 - ensure local files are ready for detected switch python version(s)
        py_versions = detect_switch_python_versions(username, password, devices)
        if not ensure_local_files(py_versions):
            print("\nError: Failed to prepare deployment files. Exiting.")
            sys.exit(1)
        
        print("\n" + "=" * 70)
        print("PHASE 3: Transferring cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Transferring to {device.get('hostname') or device['ip']}...")
            if deployer.transfer_daemon(device):
                print(f"    Transfer complete")
                success_count += 1
            else:
                print(f"    Transfer failed")
                failed_count += 1
                config.mark_ip_failed(device['ip'])
        
        if failed_count > 0:
            print(f"\n{failed_count} device(s) failed transfer.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('install')
        
        # Phase 4: Install daemon
        print("\n" + "=" * 70)
        print("PHASE 4: Installing cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Installing on {device.get('hostname') or device['ip']}...")
            if deployer.install_daemon(device):
                print(f"    Installation complete")
                success_count += 1
            else:
                print(f"    Installation failed")
                failed_count += 1
                config.mark_ip_failed(device['ip'])
        
        if failed_count > 0:
            print(f"\n{failed_count} device(s) failed installation.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('register')
        
        # Phase 5: Register with BCM
        print("\n" + "=" * 70)
        print("PHASE 5: Registering devices with BCM")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Registering {device.get('hostname') or device['ip']}...")
            if deployer.register_device(device):
                print(f"    Registration complete")
                success_count += 1
                config.mark_ip_completed(device['ip'])
            else:
                print(f"    Registration failed")
                failed_count += 1
                config.mark_ip_failed(device['ip'])
        
        config.set_phase('finalize')
        
        # Phase 6: Disable ZTP for monitoring-only mode
        print("\n" + "=" * 70)
        print("PHASE 6: Configuring for monitoring-only mode")
        print("=" * 70)
        
        # Configure for monitoring-only (no config push, no ZTP on boot)
        configure_monitoring_only_mode(devices, dry_run=args.dry_run)
        
        config.set_phase('complete')
        
        # Summary
        print("\n" + "=" * 70)
        print("DEPLOYMENT SUMMARY")
        print("=" * 70)
        
        print(f"\nTotal devices processed: {len(devices)}")
        print(f"Completed IPs: {len(config.progress['completed_ips'])}")
        print(f"Failed IPs: {len(config.progress['failed_ips'])}")
        
        if config.progress['failed_ips']:
            print(f"\nFailed IPs:")
            for ip in config.progress['failed_ips']:
                print(f"  - {ip}")
        
        # Note: Not writing bcm_switches.csv since we're using --csv input as source of truth
        
        if not args.dry_run:
            print("\nNext steps:")
            print("1. Verify devices appear in BCM device management")
            print("2. Check cm-lite-daemon service status on devices")
            print("3. Monitor logs for connectivity issues")
        
        if config.progress['phase'] == 'complete' and not config.progress['failed_ips']:
            config.clear_progress()
            print("\nDeployment completed successfully!")
        
        # Exit non-zero if any devices failed.
        sys.exit(0 if not config.progress.get('failed_ips') else 1)


    # Handle retry-failed mode
    if args.retry_failed:
        if not config.load():
            print("Error: No configuration found. Cannot retry failed devices.")
            sys.exit(1)
        
        failed_ips = config.progress.get('failed_ips', [])
        if not failed_ips:
            print("No failed devices to retry.")
            sys.exit(0)
        
        print(f"\nRetrying {len(failed_ips)} previously failed device(s):")
        for ip in failed_ips:
            print(f"  - {ip}")
        
        # Clear failed IPs so they will be retried
        config.progress['failed_ips'] = []
        
        # Remove failed IPs from completed_ips so they get rediscovered
        config.progress['completed_ips'] = [ip for ip in config.progress.get('completed_ips', [])
                                            if ip not in failed_ips]
        
        # Keep only successful devices, remove failed ones from devices list
        config.progress['devices'] = [d for d in config.progress.get('devices', []) 
                                      if d['ip'] not in failed_ips]
        
        # Reset phase to discovery for these IPs
        config.progress['phase'] = 'discovery'
        config.save()
        
        # Store failed IPs for filtering - these are the ONLY ones we'll process
        config.set('_retry_ips', failed_ips)
        config.set('switch_ips', failed_ips)
        print("\nUsing existing configuration for retry.")
    
    # Handle resume mode
    elif args.resume and config.load() and config.has_progress():
        print(f"\nResuming from previous progress (phase: {config.progress['phase']})")
        print(f"  Completed IPs: {len(config.progress['completed_ips'])}")
        print(f"  Failed IPs: {len(config.progress['failed_ips'])}")
        print(f"  Discovered devices: {len(config.progress['devices'])}")
    else:
        # Load or create configuration
        if config.load():
            # Clear any stale progress if not resuming
            if not args.resume:
                config.clear_progress()
            print("\nExisting configuration found.")
            response = input("Would you like to use the existing configuration? (y/n): ").strip().lower()
            if response in ['y', 'yes', '']:
                print("Using existing configuration.")
            else:
                config.prompt_for_config(use_existing=True)
                config.save()
        else:
            config.prompt_for_config(use_existing=False)
            config.save()
    
    print("\nConfiguration saved to .configs/config.json")
    
    # Get configuration values
    switch_ips = config.get('switch_ips', [])
    username = config.get('username', 'cumulus')
    password = config.get('password', '')
    
    if not switch_ips:
        print("Error: No switch IPs configured.")
        sys.exit(1)
    
    # Handle VRF detection
    vrf = config.get('vrf')
    
    # Run connectivity test if:
    # 1. --connectivity-test flag is specified, OR
    # 2. VRF is not yet configured and not resuming
    if args.connectivity_test or (not vrf and not args.resume):
        if args.connectivity_test:
            # Forced connectivity test
            print("\nRunning connectivity test...")
        else:
            # Ask user if they want to test
            print("\n" + "-" * 60)
            print("VRF CONFIGURATION")
            print("-" * 60)
            print("\nThe VRF setting determines which routing table the switches")
            print("use to communicate with BCM. This can be auto-detected.")
            response = input("\nWould you like to test connectivity and auto-detect VRF? (y/n) [y]: ").strip().lower()
            
            if response in ['n', 'no']:
                # Manual VRF entry
                vrf_input = input("Enter VRF to use [mgmt]: ").strip()
                vrf = vrf_input if vrf_input else "mgmt"
                config.set('vrf', vrf)
                config.save()
            else:
                # Run connectivity test
                pass
        
        # Run the test if we didn't skip it
        if not vrf:
            detected_vrf = run_connectivity_test(config)
            if detected_vrf is None:
                # User cancelled or test failed
                if args.connectivity_test:
                    print("\nConnectivity test completed.")
                    sys.exit(0)
                else:
                    print("\nExiting due to connectivity test failure.")
                    sys.exit(1)
            
            vrf = detected_vrf
            config.set('vrf', vrf)
            config.save()
            print(f"\nVRF '{vrf}' saved to configuration.")
        
        # If --connectivity-test only, exit now
        if args.connectivity_test:
            print("\n" + "=" * 60)
            print("Connectivity test completed successfully!")
            print(f"VRF configured: {vrf}")
            print("=" * 60)
            print("\nRun without --connectivity-test to proceed with deployment.")
            sys.exit(0)
    
    # Use default if still not set
    if not vrf:
        vrf = "mgmt"
        config.set('vrf', vrf)
        config.save()
    
    print(f"\nUsing VRF: {vrf}")
    
    # Detect network
    network = config.get('network')
    
    if not network:
        print("\nDetecting BCM networks...")
        network_detector = NetworkDetector()
        network_detector.detect_networks()
        
        suggested_network = network_detector.detect_network_for_ips(switch_ips)
        network = network_detector.prompt_for_network(suggested_network, switch_ips)
        config.set('network', network)
        config.save()
    
    print(f"\nUsing network: {network}")
    
    # Initialize components
    discovery = SwitchDiscovery(username, password)
    deployer = BCMDeployer(username, password, vrf, args.dry_run)
    bcm_checker = BCMChecker()
    
    # Pre-check: Look for existing devices in BCM
    if config.progress['phase'] == 'discovery' and not args.resume:
        print("\n" + "=" * 70)
        print("PRE-CHECK: Checking for existing devices in BCM")
        print("=" * 70)
        
        bcm_checker.refresh()
        existing_in_bcm = []
        
        for ip in switch_ips:
            bcm_device = bcm_checker.find_device_by_ip(ip)
            if bcm_device:
                existing_in_bcm.append((ip, bcm_device))
        
        if existing_in_bcm:
            print(f"\nFound {len(existing_in_bcm)} switch(es) already in BCM:")
            for ip, bcm_dev in existing_in_bcm:
                print(f"  - {bcm_dev['hostname']} ({ip}) - MAC: {bcm_dev.get('mac', 'N/A')}")
            
            response = input("\nWould you like to run a consistency check? (y/n) [y]: ").strip().lower()
            if response in ['n', 'no']:
                # Skip consistency check - use BCM data for all existing devices
                print("\nUsing existing BCM data for these devices...")
                for ip, bcm_dev in existing_in_bcm:
                    bcm_dev['network'] = network
                    config.add_device(bcm_dev)
                    config.mark_ip_completed(ip)
                    print(f"  ✓ Using BCM data for {bcm_dev['hostname']} ({ip})")
            else:
                print("\nRunning consistency check...")
                all_consistent = True
                
                for ip, bcm_dev in existing_in_bcm:
                    print(f"\n  Checking {bcm_dev['hostname']} ({ip})...")
                    switch_data = discovery.discover_switch(ip)
                    
                    if not switch_data:
                        print(f"    ⚠ Could not connect to switch to verify")
                        continue
                    
                    switch_data['ip'] = ip
                    differences = bcm_checker.check_consistency(switch_data, bcm_dev)
                    
                    if not differences:
                        print(f"    ✓ Consistency Confirmed!")
                        # Add to progress as already discovered
                        switch_data['network'] = network
                        config.add_device(switch_data)
                        config.mark_ip_completed(ip)
                    else:
                        all_consistent = False
                        print(f"    ⚠ MISMATCH DETECTED:")
                        for field, values in differences.items():
                            print(f"      {field}: Switch='{values['switch']}' vs BCM='{values['bcm']}'")
                        
                        print(f"\n    How would you like to handle this?")
                        print(f"      1) Use data from BCM (hostname: {bcm_dev['hostname']})")
                        print(f"      2) Use data from switch (hostname: {switch_data['hostname']})")
                        print(f"      3) Abort and fix manually")
                        
                        while True:
                            choice = input("    Select option (1/2/3): ").strip()
                            if choice == "1":
                                # Use BCM data
                                bcm_dev['network'] = network
                                config.add_device(bcm_dev)
                                config.mark_ip_completed(ip)
                                print(f"    Using BCM data for {bcm_dev['hostname']}")
                                break
                            elif choice == "2":
                                # Use switch data - will need to update BCM
                                switch_data['network'] = network
                                switch_data['_needs_bcm_update'] = True
                                config.add_device(switch_data)
                                config.mark_ip_completed(ip)
                                print(f"    Using switch data, will update BCM")
                                break
                            elif choice == "3":
                                print("\nAborting. Please fix the inconsistency manually.")
                                sys.exit(1)
                            else:
                                print("    Please enter 1, 2, or 3")
                
                if all_consistent:
                    print("\n✓ All existing devices passed consistency check!")
    
    # Phase 1: Discovery
    if config.progress['phase'] == 'discovery':
        print("\n" + "=" * 70)
        print("PHASE 1: Discovering switches")
        print("=" * 70)
        
        remaining_ips = config.get_remaining_ips(switch_ips)
        total = len(switch_ips)
        failed_count = 0
        
        if not remaining_ips:
            print("\nAll switches already discovered or checked.")
        else:
            for i, ip in enumerate(remaining_ips, len(config.progress['completed_ips']) + 1):
                print(f"\n[{i}/{total}] Discovering {ip}...")
                
                device = discovery.discover_switch(ip)
                if device:
                    device['network'] = network
                    config.add_device(device)
                    config.mark_ip_completed(ip)
                    print(f"    ✓ Progress saved")
                else:
                    config.mark_ip_failed(ip)
                    failed_count += 1
                    print(f"    ✗ Failed, marked for retry")
        
        # Check for failures and prompt
        if failed_count > 0:
            print(f"\n⚠ {failed_count} switch(es) failed discovery.")
            if not args.non_interactive:
                response = input("Do you want to proceed with the successful switches? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Run with --resume to retry failed switches.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding with successful switches)")
        
        # Generate CSV
        devices = config.progress['devices']
        if devices:
            write_csv(devices, network)
            
            # Check ZTP status on discovered switches
            print("\n" + "-" * 70)
            print("Checking ZTP status on switches...")
            ztp_enabled = []
            for d in devices:
                status = discovery.check_ztp_status(d['ip'])
                print(f"  {d['hostname']}: ZTP {status or 'unknown'}")
                if status == 'enabled':
                    ztp_enabled.append(d['hostname'])
            
            if ztp_enabled:
                print("\n" + "!" * 70)
                print("WARNING: ZTP IS ENABLED ON THESE SWITCHES:")
                for name in ztp_enabled:
                    print(f"  - {name}")
                print("\n⚠ This script has NOT been tested with ZTP enabled.")
                print("  BCM integration may affect existing ZTP configuration.")
                print("\n  To disable ZTP first, run:")
                print("    ./scripts/change-switch-defaults.py --disable-ztp --csv <file>")
                print("!" * 70)
                if not args.non_interactive:
                    resp = input("\nContinue anyway? (yes/no) [no]: ").strip().lower()
                    if resp != 'yes':
                        print("\nExiting. Disable ZTP first, then run again.")
                        sys.exit(1)
                else:
                    print("\n  (non-interactive: proceeding despite ZTP warning)")
            else:
                print("✓ ZTP disabled on all switches")
            
            config.set_phase('bcm_add')
        else:
            print("\nNo devices discovered. Exiting.")
            sys.exit(1)
    
    devices = config.progress['devices']
    
    # Filter devices to only those we're currently processing (for --retry-failed)
    if args.retry_failed:
        target_ips = set(switch_ips)
        devices = [d for d in devices if d['ip'] in target_ips]
    
    # Phase 2: Add to BCM
    if config.progress['phase'] == 'bcm_add':
        print("\n" + "=" * 70)
        print("PHASE 2: Adding devices to BCM")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Adding {device['hostname']} to BCM...")
            if deployer.add_device_to_bcm(device, network):
                print(f"    ✓ Added to BCM")
                success_count += 1
            else:
                print(f"    ✗ Failed to add to BCM")
                failed_count += 1
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed to add to BCM.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('transfer')
        
        # Phase 3: Transfer daemon
    if config.progress['phase'] == 'transfer':
        # Ensure local files are ready before transfer
        if not ensure_local_files():
            print("\nError: Failed to prepare deployment files. Exiting.")
            sys.exit(1)
        
        print("\n" + "=" * 70)
        print("PHASE 3: Transferring cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Transferring to {device['hostname']}...")
            if deployer.transfer_daemon(device):
                print(f"    ✓ Transfer complete")
                success_count += 1
            else:
                print(f"    ✗ Transfer failed")
                failed_count += 1
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed transfer.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('install')
    
    # Phase 4: Install daemon
    if config.progress['phase'] == 'install':
        print("\n" + "=" * 70)
        print("PHASE 4: Installing cm-lite-daemon")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Installing on {device['hostname']}...")
            if deployer.install_daemon(device):
                print(f"    ✓ Installation complete")
                success_count += 1
            else:
                print(f"    ✗ Installation failed")
                failed_count += 1
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed installation.")
            if not args.non_interactive:
                response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("\nExiting. Fix the issues and run with --resume to continue.")
                    sys.exit(1)
            else:
                print("  (non-interactive: proceeding anyway)")
        
        config.set_phase('register')
    
    # Phase 5: Register with BCM
    if config.progress['phase'] == 'register':
        print("\n" + "=" * 70)
        print("PHASE 5: Registering devices with BCM")
        print("=" * 70)
        
        success_count = 0
        failed_count = 0
        
        for i, device in enumerate(devices, 1):
            print(f"\n[{i}/{len(devices)}] Registering {device['hostname']}...")
            if deployer.register_device(device):
                print(f"    ✓ Registration complete")
                success_count += 1
            else:
                print(f"    ✗ Registration failed")
                failed_count += 1
        
        if failed_count > 0:
            print(f"\n⚠ {failed_count} device(s) failed registration.")
        
        config.set_phase('finalize')
    
    # Phase 6: Disable ZTP for monitoring-only mode
    if config.progress['phase'] == 'finalize':
        print("\n" + "=" * 70)
        print("PHASE 6: Configuring for monitoring-only mode")
        print("=" * 70)
        
        # Configure for monitoring-only (no config push, no ZTP on boot)
        configure_monitoring_only_mode(devices, dry_run=args.dry_run)
        
        config.set_phase('complete')
    
    # Summary
    print("\n" + "=" * 70)
    print("DEPLOYMENT SUMMARY")
    print("=" * 70)
    
    print(f"\nTotal devices processed: {len(devices)}")
    print(f"Completed IPs: {len(config.progress['completed_ips'])}")
    print(f"Failed IPs: {len(config.progress['failed_ips'])}")
    
    if config.progress['failed_ips']:
        print(f"\nFailed IPs:")
        for ip in config.progress['failed_ips']:
            print(f"  - {ip}")
    
    print(f"\nCSV file: {CSV_FILE}")
    
    if not args.dry_run:
        print("\nNext steps:")
        print("1. Verify devices appear in BCM device management")
        print("2. Start cm-lite-daemon service on devices if needed")
        print("3. Monitor logs for connectivity issues")
    
    # Clear progress on success
    if config.progress['phase'] == 'complete' and not config.progress['failed_ips']:
        config.clear_progress()
        print("\nDeployment completed successfully!")
    else:
        print(f"\nProgress saved. Run with --resume to continue.")


if __name__ == "__main__":
    main()

