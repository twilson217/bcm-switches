#!/usr/bin/env python3
"""
BCM Switch Deployment Script

A comprehensive tool for deploying Cumulus switches to BCM (Base Command Manager).
This script automates the entire process from switch discovery to full deployment.

Usage:
    python3 deploy_bcm_switches.py              # Normal mode
    python3 deploy_bcm_switches.py --airgapped  # Airgapped mode (uses local files)
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
PROGRESS_FILE = CONFIG_DIR / "progress.json"
CSV_FILE = CONFIG_DIR / "bcm_switches.csv"
FILES_DIR = SCRIPT_DIR / ".files"
CM_LITE_ZIP_PATH = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")


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
    """Manage configuration file operations."""
    
    def __init__(self):
        self.config = {}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> bool:
        """Load configuration from file. Returns True if config exists."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.config = json.load(f)
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
        
        ip_input = input("Enter switch IP addresses (formats: 192.168.0.1-100, 192.168.0.1-192.168.0.100,\n"
                        "  or comma-separated, or combinations) [Enter to keep]: ").strip()
        
        if ip_input:
            self.config['switch_ips'] = IPAddressParser.parse(ip_input)
            print(f"  Parsed {len(self.config['switch_ips'])} IP address(es)")
            self.save()  # Save after IP addresses
        elif not use_existing or not current_ips:
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
        if use_existing:
            print("\nCurrent password: *******")
        password = getpass.getpass("Enter SSH password for switches [Enter to keep]: ")
        if password:
            self.config['password'] = password
            self.save()  # Save after password
        elif not use_existing or not self.get('password'):
            self.config['password'] = getpass.getpass("Enter SSH password for switches: ")
            self.save()  # Save after password


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


class ProgressTracker:
    """Track deployment progress for resume capability."""
    
    PHASES = ['discovery', 'bcm_add', 'transfer', 'install', 'register', 'complete']
    
    def __init__(self):
        self.progress = {
            'phase': 'discovery',
            'completed_ips': [],
            'failed_ips': [],
            'devices': []
        }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> bool:
        """Load progress from file. Returns True if progress exists."""
        if PROGRESS_FILE.exists():
            try:
                with open(PROGRESS_FILE, 'r') as f:
                    self.progress = json.load(f)
                return True
            except (json.JSONDecodeError, IOError):
                pass
        return False
    
    def save(self):
        """Save progress to file."""
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(self.progress, f, indent=2)
    
    def clear(self):
        """Clear progress file."""
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
        self.progress = {
            'phase': 'discovery',
            'completed_ips': [],
            'failed_ips': [],
            'devices': []
        }
    
    def set_phase(self, phase: str):
        """Set current phase."""
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
        """Run a command on a remote host via SSH."""
        ssh_cmd = ["sshpass", "-p", self.password, "ssh",
                   "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "ConnectTimeout=10",
                   f"{self.username}@{host}", command]
        
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
        """Check if switch is reachable via ping."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "3", ip],
                capture_output=True, timeout=10
            )
            return result.returncode == 0
        except:
            return False
    
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
                 airgapped: bool = False, dry_run: bool = False):
        self.username = username
        self.password = password
        self.vrf = vrf
        self.airgapped = airgapped
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
        
        # Check if already installed
        if skip_if_exists and self.check_daemon_installed(device):
            print(f"    ✓ cm-lite-daemon already installed on {device['hostname']}, skipping transfer")
            return True
        
        work_dir = Path(tempfile.mkdtemp(prefix="cm_lite_daemon_"))
        
        try:
            # Get cm-lite-daemon.zip
            if self.airgapped:
                zip_path = FILES_DIR / "cm-lite-daemon.zip"
                if not zip_path.exists():
                    print(f"    ✗ cm-lite-daemon.zip not found in {FILES_DIR}")
                    return False
                local_zip = work_dir / "cm-lite-daemon.zip"
                shutil.copy2(zip_path, local_zip)
            else:
                if not CM_LITE_ZIP_PATH.exists():
                    print(f"    ✗ cm-lite-daemon.zip not found at {CM_LITE_ZIP_PATH}")
                    return False
                local_zip = work_dir / "cm-lite-daemon.zip"
                shutil.copy2(CM_LITE_ZIP_PATH, local_zip)
            
            # Get pip packages
            pip_packages_dir = work_dir / "pip_packages_dep"
            
            if self.airgapped:
                src_packages = FILES_DIR / "pip_packages_dep"
                if src_packages.exists():
                    shutil.copytree(src_packages, pip_packages_dir)
                else:
                    print(f"    ✗ pip_packages_dep not found in {FILES_DIR}")
                    return False
            else:
                pip_packages_dir.mkdir(exist_ok=True)
                # Extract requirements.txt and download packages
                with zipfile.ZipFile(local_zip, 'r') as zip_ref:
                    for filename in zip_ref.namelist():
                        if filename.endswith('requirements.txt'):
                            with zip_ref.open(filename) as req_file:
                                requirements = req_file.read().decode('utf-8')
                                break
                    else:
                        print("    ✗ requirements.txt not found in zip")
                        return False
                
                temp_req = work_dir / "requirements.txt"
                temp_req.write_text(requirements)
                
                cmd = ["pip", "download", "--python-version", "3.11",
                       "-r", str(temp_req), "--dest", str(pip_packages_dir), "--no-deps"]
                subprocess.run(cmd, capture_output=True, check=True)
            
            # Transfer files via SCP
            scp_base = ["sshpass", "-p", self.password, "scp", "-r",
                       "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
            
            target = f"{self.username}@{device['ip']}:/home/{self.username}/"
            
            subprocess.run(scp_base + [str(local_zip), target], check=True, capture_output=True)
            subprocess.run(scp_base + [str(pip_packages_dir), target], check=True, capture_output=True)
            
            return True
            
        except Exception as e:
            print(f"    ✗ Transfer failed: {e}")
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    
    def install_daemon(self, device: Dict, skip_if_exists: bool = True) -> bool:
        """Install cm-lite-daemon on a device."""
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
        
        commands = [
            "sudo apt update",
            "sudo apt install -y build-essential python3-dev python3-pip unzip",
            f"cd /home/{self.username} && unzip -o cm-lite-daemon.zip",
            f"sudo cp -r /home/{self.username}/cm-lite-daemon /opt/",
            f"cd /opt/cm-lite-daemon && sudo pip3 install --break-system-packages --no-index "
            f"--find-links /home/{self.username}/pip_packages_dep -r requirements.txt",
            f"rm -f /home/{self.username}/cm-lite-daemon.zip"
        ]
        
        for cmd in commands:
            try:
                # Handle sudo password
                if "sudo " in cmd:
                    full_cmd = ssh_base + [cmd.replace("sudo ", "sudo -S ", 1)]
                    result = subprocess.run(full_cmd, input=f"{self.password}\n",
                                          capture_output=True, text=True, timeout=300)
                else:
                    result = subprocess.run(ssh_base + [cmd], capture_output=True, text=True, timeout=300)
                
                if result.returncode != 0 and "already" not in result.stderr.lower():
                    # Some non-critical errors are OK
                    if "unzip" in cmd or "rm " in cmd:
                        continue
            except Exception as e:
                print(f"    ✗ Command failed: {e}")
                return False
        
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
            register_cmd = (f"cd /opt/cm-lite-daemon && sudo ./register_node "
                          f"--host {bcm_master_ip} --disable-cert-check --vrf {self.vrf}")
            full_cmd = ssh_base + [register_cmd.replace("sudo ", "sudo -S ", 1)]
            subprocess.run(full_cmd, input=f"{self.password}\n",
                         capture_output=True, text=True, timeout=120)
            
            return True
            
        except Exception as e:
            print(f"    ✗ Registration failed: {e}")
            return False


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


def check_prerequisites(airgapped: bool = False):
    """Check that all prerequisites are met."""
    # Check for sshpass
    if not shutil.which("sshpass"):
        print("Error: sshpass is required. Install with: apt install sshpass")
        sys.exit(1)
    
    # Check for cmsh
    if not shutil.which("cmsh"):
        print("Error: cmsh not found. This script must run on a BCM system.")
        sys.exit(1)
    
    # Check for airgapped files if needed
    if airgapped:
        if not (FILES_DIR / "cm-lite-daemon.zip").exists():
            print(f"Error: cm-lite-daemon.zip not found in {FILES_DIR}")
            print("Run scripts/prep-airgapped.py first to prepare airgapped files.")
            sys.exit(1)
        if not (FILES_DIR / "pip_packages_dep").exists():
            print(f"Error: pip_packages_dep not found in {FILES_DIR}")
            print("Run scripts/prep-airgapped.py first to prepare airgapped files.")
            sys.exit(1)
    else:
        if not CM_LITE_ZIP_PATH.exists():
            print(f"Error: cm-lite-daemon.zip not found at {CM_LITE_ZIP_PATH}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Cumulus switches to BCM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      # Normal interactive mode
  %(prog)s --airgapped          # Use pre-downloaded files from ./files/
  %(prog)s --resume             # Resume from previous progress
  %(prog)s --dry-run            # Show what would be done
  %(prog)s --connectivity-test  # Run connectivity test and VRF detection only
        """
    )
    
    parser.add_argument("--airgapped", action="store_true",
                       help="Use airgapped mode (files from ./files/ directory)")
    parser.add_argument("--resume", action="store_true",
                       help="Resume from previous progress")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without executing")
    parser.add_argument("--connectivity-test", action="store_true",
                       help="Run connectivity test and VRF auto-detection only")
    
    args = parser.parse_args()
    
    print("=" * 70)
    print("BCM Switch Deployment Tool")
    print("=" * 70)
    
    # Check prerequisites
    check_prerequisites(args.airgapped)
    
    # Initialize managers
    config = ConfigManager()
    progress = ProgressTracker()
    
    # Handle resume mode
    if args.resume and progress.load():
        print(f"\nResuming from previous progress (phase: {progress.progress['phase']})")
        print(f"  Completed IPs: {len(progress.progress['completed_ips'])}")
        print(f"  Failed IPs: {len(progress.progress['failed_ips'])}")
        print(f"  Discovered devices: {len(progress.progress['devices'])}")
        
        if not config.load():
            print("Error: No configuration found. Cannot resume.")
            sys.exit(1)
    else:
        progress.clear()
        
        # Load or create configuration
        if config.load():
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
    print("\nDetecting BCM networks...")
    network_detector = NetworkDetector()
    network_detector.detect_networks()
    
    suggested_network = network_detector.detect_network_for_ips(switch_ips)
    network = config.get('network')
    
    if not network or not args.resume:
        network = network_detector.prompt_for_network(suggested_network, switch_ips)
        config.set('network', network)
        config.save()
    
    print(f"\nUsing network: {network}")
    
    # Initialize components
    discovery = SwitchDiscovery(username, password)
    deployer = BCMDeployer(username, password, vrf, args.airgapped, args.dry_run)
    bcm_checker = BCMChecker()
    
    # Pre-check: Look for existing devices in BCM
    if progress.progress['phase'] == 'discovery' and not args.resume:
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
            if response not in ['n', 'no']:
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
                        progress.add_device(switch_data)
                        progress.mark_ip_completed(ip)
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
                                progress.add_device(bcm_dev)
                                progress.mark_ip_completed(ip)
                                print(f"    Using BCM data for {bcm_dev['hostname']}")
                                break
                            elif choice == "2":
                                # Use switch data - will need to update BCM
                                switch_data['network'] = network
                                switch_data['_needs_bcm_update'] = True
                                progress.add_device(switch_data)
                                progress.mark_ip_completed(ip)
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
    if progress.progress['phase'] == 'discovery':
        print("\n" + "=" * 70)
        print("PHASE 1: Discovering switches")
        print("=" * 70)
        
        remaining_ips = progress.get_remaining_ips(switch_ips)
        total = len(switch_ips)
        failed_count = 0
        
        if not remaining_ips:
            print("\nAll switches already discovered or checked.")
        else:
            for i, ip in enumerate(remaining_ips, len(progress.progress['completed_ips']) + 1):
                print(f"\n[{i}/{total}] Discovering {ip}...")
                
                device = discovery.discover_switch(ip)
                if device:
                    device['network'] = network
                    progress.add_device(device)
                    progress.mark_ip_completed(ip)
                    print(f"    ✓ Progress saved")
                else:
                    progress.mark_ip_failed(ip)
                    failed_count += 1
                    print(f"    ✗ Failed, marked for retry")
        
        # Check for failures and prompt
        if failed_count > 0:
            print(f"\n⚠ {failed_count} switch(es) failed discovery.")
            response = input("Do you want to proceed with the successful switches? (y/n) [n]: ").strip().lower()
            if response not in ['y', 'yes']:
                print("\nExiting. Run with --resume to retry failed switches.")
                sys.exit(1)
        
        # Generate CSV
        devices = progress.progress['devices']
        if devices:
            write_csv(devices, network)
            progress.set_phase('bcm_add')
        else:
            print("\nNo devices discovered. Exiting.")
            sys.exit(1)
    
    devices = progress.progress['devices']
    
    # Phase 2: Add to BCM
    if progress.progress['phase'] == 'bcm_add':
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
            response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
            if response not in ['y', 'yes']:
                print("\nExiting. Fix the issues and run with --resume to continue.")
                sys.exit(1)
        
        if not args.dry_run and success_count > 0:
            print("\nWaiting 15 seconds for BCM initialization...")
            time.sleep(15)
        
        progress.set_phase('transfer')
    
    # Phase 3: Transfer daemon
    if progress.progress['phase'] == 'transfer':
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
            response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
            if response not in ['y', 'yes']:
                print("\nExiting. Fix the issues and run with --resume to continue.")
                sys.exit(1)
        
        progress.set_phase('install')
    
    # Phase 4: Install daemon
    if progress.progress['phase'] == 'install':
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
            response = input("Do you want to proceed to the next phase? (y/n) [n]: ").strip().lower()
            if response not in ['y', 'yes']:
                print("\nExiting. Fix the issues and run with --resume to continue.")
                sys.exit(1)
        
        progress.set_phase('register')
    
    # Phase 5: Register with BCM
    if progress.progress['phase'] == 'register':
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
        
        progress.set_phase('complete')
    
    # Summary
    print("\n" + "=" * 70)
    print("DEPLOYMENT SUMMARY")
    print("=" * 70)
    
    print(f"\nTotal devices processed: {len(devices)}")
    print(f"Completed IPs: {len(progress.progress['completed_ips'])}")
    print(f"Failed IPs: {len(progress.progress['failed_ips'])}")
    
    if progress.progress['failed_ips']:
        print(f"\nFailed IPs:")
        for ip in progress.progress['failed_ips']:
            print(f"  - {ip}")
    
    print(f"\nCSV file: {CSV_FILE}")
    
    if not args.dry_run:
        print("\nNext steps:")
        print("1. Verify devices appear in BCM device management")
        print("2. Start cm-lite-daemon service on devices if needed")
        print("3. Monitor logs for connectivity issues")
    
    # Clear progress on success
    if progress.progress['phase'] == 'complete' and not progress.progress['failed_ips']:
        progress.clear()
        print("\nDeployment completed successfully!")
    else:
        print(f"\nProgress saved. Run with --resume to continue.")


if __name__ == "__main__":
    main()

