#!/usr/bin/env python3
"""
Complete Cumulus Setup Script

This script provides end-to-end automation for adding Cumulus devices to BCM (Base Command Manager)
management with cm-lite-daemon. It orchestrates all existing scripts to provide a single-command solution.

Usage examples:
    # Single device setup
    python3 complete_cumulus_setup.py --host 10.141.1.1 --hostname spine01 --mac 44:38:39:00:01:01 --network internalnet
    
    # CSV file setup  
    python3 complete_cumulus_setup.py --csv cumulus.csv
    
    # With explicit BCM master IP
    python3 complete_cumulus_setup.py --host 10.141.1.1 --hostname spine01 --mac 44:38:39:00:01:01 --network internalnet --bcm-master-ip 10.141.255.254
    
    # Dry run
    python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run
"""

import argparse
import csv
import getpass
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional


class CumulusDeviceManager:
    def __init__(self, bcm_master_ip=None, bcm_master_name="master", 
                 username="cumulus", password=None, ssh_key=None, 
                 install_dir="/opt", dry_run=False, skip_ping=False, vrf="mgmt"):
        self.bcm_master_ip = bcm_master_ip
        self.bcm_master_name = bcm_master_name
        self.username = username
        self.password = password
        self.ssh_key = ssh_key
        self.install_dir = install_dir
        self.dry_run = dry_run
        self.skip_ping = skip_ping
        self.vrf = vrf
        
        # Paths to existing scripts
        self.script_dir = Path(__file__).parent
        self.add_devices_script = self.script_dir / "add_cumulus_devices_to_bcm.py"
        self.transfer_script = self.script_dir / "transfer_cm_lite_daemon.py"
        self.install_script = self.script_dir / "remote_install_cm_lite.py"
        
        # Validate scripts exist
        self._validate_scripts()
        
    def get_bcm_master_ip(self):
        """Automatically determine BCM master IP using cmsh command"""
        if self.bcm_master_ip:
            # IP already provided, no need to detect
            return self.bcm_master_ip
            
        print("Auto-detecting BCM master IP...")
        try:
            result = subprocess.run(
                ["cmsh", "-c", "device; use master; get ip"], 
                check=True, 
                capture_output=True, 
                text=True
            )
            detected_ip = result.stdout.strip()
            if detected_ip:
                print(f"✓ Detected BCM master IP: {detected_ip}")
                self.bcm_master_ip = detected_ip
                return detected_ip
            else:
                raise ValueError("Empty IP address returned from cmsh command")
                
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to detect BCM master IP using cmsh command: {e}"
            if e.stderr:
                error_msg += f"\nError output: {e.stderr.strip()}"
            raise RuntimeError(error_msg)
        except FileNotFoundError:
            raise RuntimeError("cmsh command not found. Make sure you're running this script on a BCM system.")

    def _validate_scripts(self):
        """Validate that all required scripts exist"""
        missing_scripts = []
        for script_name, script_path in [
            ("add_cumulus_devices_to_bcm.py", self.add_devices_script),
            ("transfer_cm_lite_daemon.py", self.transfer_script),
            ("remote_install_cm_lite.py", self.install_script)
        ]:
            if not script_path.exists():
                missing_scripts.append(script_name)
        
        if missing_scripts:
            raise FileNotFoundError(f"Missing required scripts: {', '.join(missing_scripts)}")

    def check_device_connectivity(self, device: Dict[str, str]) -> bool:
        """Check if device is reachable via ping"""
        if self.skip_ping:
            return True
            
        if self.dry_run:
            print(f"[DRY RUN] Would ping {device['hostname']} ({device['ip']})")
            return True
        
        print(f"🔍 Checking connectivity to {device['hostname']} ({device['ip']})...")
        
        try:
            # Use ping command with timeout
            # -c 3: send 3 packets, -W 3: wait 3 seconds for response
            cmd = ["ping", "-c", "3", "-W", "3", device['ip']]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            
            if result.returncode == 0:
                print(f"✅ {device['hostname']} is reachable")
                return True
            else:
                print(f"❌ {device['hostname']} is not reachable via ping")
                return False
                
        except subprocess.TimeoutExpired:
            print(f"❌ {device['hostname']} ping timeout after 15 seconds")
            return False
        except Exception as e:
            print(f"❌ {device['hostname']} ping check failed: {e}")
            return False
    
    def _run_script(self, script_path: Path, args: List[str], description: str) -> bool:
        """Run a script with the given arguments"""
        cmd = ["python3", str(script_path)] + args
        
        if self.dry_run:
            print(f"[DRY RUN] {description}")
            print(f"[DRY RUN] Command: {' '.join(cmd)}")
            return True
        
        print(f"\n{'='*60}")
        print(f"STEP: {description}")
        print(f"{'='*60}")
        print(f"Executing: {' '.join(cmd)}")
        print()
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=False, text=True)
            print(f"✓ {description} completed successfully")
            return True
        except subprocess.CalledProcessError as e:
            print(f"✗ {description} failed with return code {e.returncode}")
            return False
        except Exception as e:
            print(f"✗ {description} failed: {e}")
            return False
    
    def create_temp_csv(self, devices: List[Dict[str, str]]) -> Path:
        """Create a temporary CSV file with device information"""
        temp_file = Path(tempfile.mktemp(suffix=".csv"))
        
        with open(temp_file, 'w', newline='') as csvfile:
            fieldnames = ['Hostname', 'IP', 'MAC', 'Network']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for device in devices:
                writer.writerow({
                    'Hostname': device['hostname'],
                    'IP': device['ip'],
                    'MAC': device['mac'],
                    'Network': device['network']
                })
        
        return temp_file
    
    def add_devices_to_bcm(self, devices: List[Dict[str, str]]) -> bool:
        """Add devices to BCM using the add_cumulus_devices_to_bcm.py script"""
        temp_csv = self.create_temp_csv(devices)
        
        try:
            args = ["--csv", str(temp_csv)]
            if not self.dry_run:
                args.append("--execute")
            
            # Add credentials if available
            if self.username != "cumulus":
                args.extend(["--username", self.username])
            if self.password:
                args.extend(["--password", self.password])
            
            success = self._run_script(
                self.add_devices_script,
                args,
                f"Adding {len(devices)} device(s) to BCM"
            )
            
            if success and not self.dry_run:
                print("Waiting 15 seconds for BCM initialize process to begin generating bootstrap certificates...")
                print("(Individual device setup will wait for actual file generation if needed)")
                time.sleep(15)
            
            return success
            
        finally:
            # Clean up temp file
            if temp_csv.exists():
                temp_csv.unlink()
    
    def transfer_daemon_to_device(self, device: Dict[str, str]) -> bool:
        """Transfer cm-lite-daemon to a device using transfer_cm_lite_daemon.py"""
        args = ["--host", device['ip'], "--username", self.username]
        
        # Add authentication
        if self.ssh_key:
            args.extend(["--ssh-key", self.ssh_key])
        elif self.password:
            args.extend(["--password", self.password])
        
        return self._run_script(
            self.transfer_script,
            args,
            f"Transferring cm-lite-daemon to {device['hostname']} ({device['ip']})"
        )
    
    def install_and_register_device(self, device: Dict[str, str]) -> bool:
        """Install cm-lite-daemon and register with BCM using remote_install_cm_lite.py"""
        args = [
            "--host", device['ip'],
            "--username", self.username,
            "--switch-name", device['hostname'],
            "--transfer-bootstrap",  # Add this flag to transfer bootstrap certificates
            "--register-node"
        ]
        
        # Add authentication
        if self.ssh_key:
            args.extend(["--ssh-key", self.ssh_key])
        elif self.password:
            args.extend(["--password", self.password])
        
        # Add BCM master information
        if self.bcm_master_ip:
            args.extend(["--bcm-master-ip", self.bcm_master_ip])
        if self.bcm_master_name != "master":
            args.extend(["--bcm-master-name", self.bcm_master_name])
        
        # Add installation directory if custom
        if self.install_dir != "/opt":
            args.extend(["--install-dir", self.install_dir])
        
        # Add VRF parameter
        args.extend(["--vrf", self.vrf])
        
        return self._run_script(
            self.install_script,
            args,
            f"Installing and registering {device['hostname']} ({device['ip']})"
        )
    
    def setup_device(self, device: Dict[str, str]) -> str:
        """Complete setup for a single device"""
        print(f"\n🚀 Starting complete setup for {device['hostname']} ({device['ip']})")
        
        # Check connectivity first
        if not self.check_device_connectivity(device):
            print(f"⏭️ Skipping {device['hostname']} due to connectivity issues")
            return "skipped"
        
        # Step 1: Add device to BCM (if not already done in batch)
        # This is handled separately for efficiency
        
        # Step 2: Transfer daemon files
        if not self.transfer_daemon_to_device(device):
            print(f"❌ Failed to transfer daemon to {device['hostname']}")
            return "failed"
        
        # Step 3: Install daemon and register with BCM
        if not self.install_and_register_device(device):
            print(f"❌ Failed to install/register {device['hostname']}")
            return "failed"
        
        print(f"✅ Successfully completed setup for {device['hostname']}")
        return "success"
    
    def setup_devices(self, devices: List[Dict[str, str]]) -> Dict[str, str]:
        """Setup multiple devices with complete workflow"""
        if not devices:
            print("No devices to setup")
            return {}
        
        print(f"\n🎯 Starting complete Cumulus device setup for {len(devices)} device(s)")
        print("="*80)
        
        # Step 1: Add all devices to BCM in batch
        print(f"\n📋 PHASE 1: Adding all devices to BCM")
        if not self.add_devices_to_bcm(devices):
            print("❌ Failed to add devices to BCM. Aborting.")
            return {device['hostname']: "failed" for device in devices}
        
        # Step 2: Process each device individually
        print(f"\n🔧 PHASE 2: Setting up individual devices")
        results = {}
        
        for i, device in enumerate(devices, 1):
            print(f"\n--- Device {i}/{len(devices)} ---")
            results[device['hostname']] = self.setup_device(device)
        
        return results
    
    def print_summary(self, results: Dict[str, str]):
        """Print setup summary"""
        print("\n" + "="*80)
        print("🏁 SETUP SUMMARY")
        print("="*80)
        
        successful = [host for host, status in results.items() if status == "success"]
        failed = [host for host, status in results.items() if status == "failed"]
        skipped = [host for host, status in results.items() if status == "skipped"]
        
        print(f"✅ Successful: {len(successful)}")
        for host in successful:
            print(f"   - {host}")
        
        if skipped:
            print(f"⏭️ Skipped (connectivity issues): {len(skipped)}")
            for host in skipped:
                print(f"   - {host}")
        
        if failed:
            print(f"❌ Failed: {len(failed)}")
            for host in failed:
                print(f"   - {host}")
        
        print(f"\nTotal devices processed: {len(results)}")
        
        if len(results) > 0:
            success_rate = len(successful) / len(results) * 100
            print(f"Success rate: {len(successful)}/{len(results)} ({success_rate:.1f}%)")
            
            if skipped:
                accessible_devices = len(results) - len(skipped)
                if accessible_devices > 0:
                    accessible_success_rate = len(successful) / accessible_devices * 100
                    print(f"Success rate (accessible devices only): {len(successful)}/{accessible_devices} ({accessible_success_rate:.1f}%)")
        
        if not self.dry_run and successful:
            print("\n🎉 Next steps for successful devices:")
            print("1. Verify devices appear in BCM device management")
            print("2. Start cm-lite-daemon service on devices if needed")
            print("3. Monitor logs for connectivity issues")
            print("4. Configure any device-specific settings in BCM")
            
        if skipped:
            print("\n⚠️ For skipped devices:")
            print("1. Check network connectivity and routing")
            print("2. Verify device IP addresses are correct")
            print("3. Ensure devices are powered on and accessible")
            print("4. Re-run setup once connectivity is restored")


def read_devices_from_csv(csv_file: str) -> List[Dict[str, str]]:
    """Read device information from CSV file"""
    devices = []
    
    try:
        with open(csv_file, 'r', newline='') as file:
            # Detect delimiter
            sample = file.read(1024)
            file.seek(0)
            delimiter = '\t' if '\t' in sample else ','
            
            reader = csv.DictReader(file, delimiter=delimiter)
            
            for row in reader:
                # Handle different possible column names
                hostname = row.get('Hostname') or row.get('hostname') or row.get('HOSTNAME')
                ip = row.get('IP') or row.get('ip') or row.get('IP_Address')
                mac = row.get('MAC') or row.get('mac') or row.get('MAC_Address')
                network = row.get('Network') or row.get('network') or row.get('NETWORK')
                
                if hostname and ip and mac and network:
                    devices.append({
                        'hostname': hostname.strip(),
                        'ip': ip.strip(),
                        'mac': mac.strip(),
                        'network': network.strip()
                    })
                else:
                    print(f"Warning: Skipping incomplete row: {row}")
                    
    except FileNotFoundError:
        print(f"Error: CSV file '{csv_file}' not found.")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        sys.exit(1)
        
    return devices


def validate_device_info(hostname: str, ip: str, mac: str, network: str) -> bool:
    """Validate required device information"""
    if not all([hostname, ip, mac, network]):
        print("Error: All device information is required (hostname, ip, mac, network)")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Complete Cumulus device setup for BCM management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single device setup (auto-detects BCM master IP)
  %(prog)s --host 10.141.1.1 --hostname spine01 --mac 44:38:39:00:01:01 --network internalnet

  # From CSV file (auto-detects BCM master IP)
  %(prog)s --csv cumulus.csv

  # With explicit BCM master IP
  %(prog)s --host 10.141.1.1 --hostname spine01 --mac 44:38:39:00:01:01 --network internalnet --bcm-master-ip 10.141.255.254

  # Skip ping connectivity checks
  %(prog)s --csv cumulus.csv --skip-ping

  # Dry run mode
  %(prog)s --csv cumulus.csv --dry-run

  # With SSH key authentication
  %(prog)s --csv cumulus.csv --ssh-key ~/.ssh/cumulus_key

  # With custom VRF
  %(prog)s --csv cumulus.csv --vrf production
        """
    )
    
    # Device specification (choose one)
    device_group = parser.add_mutually_exclusive_group(required=True)
    device_group.add_argument("--csv", help="CSV file containing device information")
    device_group.add_argument("--host", help="Single device IP address")
    
    # Single device parameters (required when using --host)
    parser.add_argument("--hostname", help="Device hostname (required with --host)")
    parser.add_argument("--mac", help="Device MAC address (required with --host)")
    parser.add_argument("--network", help="Device network/role (required with --host)")
    
    # Authentication options
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument("--password", help="SSH password")
    auth_group.add_argument("--ssh-key", help="Path to SSH private key")
    
    # Configuration options
    parser.add_argument("--username", default="cumulus", help="SSH username (default: cumulus)")
    parser.add_argument("--bcm-master-ip", help="BCM master IP address (optional - will auto-detect using cmsh if not provided)")
    parser.add_argument("--bcm-master-name", default="master", help="BCM master hostname (default: master)")
    parser.add_argument("--install-dir", default="/opt", help="Installation directory (default: /opt)")
    parser.add_argument("--skip-ping", action="store_true", help="Skip ping connectivity checks")
    parser.add_argument("--vrf", default="mgmt", help="VRF to use for installation (default: mgmt)")
    
    # Operation mode
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    
    args = parser.parse_args()
    
    # Validate single device parameters
    if args.host:
        if not validate_device_info(args.hostname, args.host, args.mac, args.network):
            sys.exit(1)
        devices = [{
            'hostname': args.hostname,
            'ip': args.host,
            'mac': args.mac,
            'network': args.network
        }]
    else:
        devices = read_devices_from_csv(args.csv)
        if not devices:
            print("No valid devices found in CSV file.")
            sys.exit(1)
    
    # Get password if needed
    if not args.password and not args.ssh_key and not args.dry_run:
        args.password = getpass.getpass(f"Password for {args.username}: ")
    
    # Get BCM master IP - auto-detect first, then prompt if needed
    if not args.bcm_master_ip and not args.dry_run:
        # Create a temporary manager instance just for auto-detection
        temp_manager = CumulusDeviceManager()
        
        try:
            args.bcm_master_ip = temp_manager.get_bcm_master_ip()
        except (RuntimeError, ValueError) as e:
            print(f"\n⚠️ Auto-detection failed: {e}")
            print(f"\nBCM master IP is required for device registration.")
            try:
                args.bcm_master_ip = input(f"Enter BCM master IP address manually: ").strip()
                if not args.bcm_master_ip:
                    print("Error: BCM master IP cannot be empty")
                    sys.exit(1)
            except (KeyboardInterrupt, EOFError):
                print("\nOperation cancelled by user")
                sys.exit(1)
    
    # Create device manager
    manager = CumulusDeviceManager(
        bcm_master_ip=args.bcm_master_ip,
        bcm_master_name=args.bcm_master_name,
        username=args.username,
        password=args.password,
        ssh_key=args.ssh_key,
        install_dir=args.install_dir,
        dry_run=args.dry_run,
        skip_ping=args.skip_ping,
        vrf=args.vrf
    )
    
    # Show devices to be processed
    print(f"\n📝 Devices to be configured:")
    for device in devices:
        print(f"   - {device['hostname']} ({device['ip']}) - MAC: {device['mac']} - Network: {device['network']}")
    
    if not args.dry_run:
        response = input(f"\nProceed with setup of {len(devices)} device(s)? (y/N): ").strip().lower()
        if response not in ['y', 'yes']:
            print("Operation cancelled.")
            sys.exit(0)
    
    try:
        # Execute complete setup
        results = manager.setup_devices(devices)
        
        # Print summary
        manager.print_summary(results)
        
        # Exit with error code if any devices failed
        failed_count = len([status for status in results.values() if status == "failed"])
        if failed_count > 0:
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 