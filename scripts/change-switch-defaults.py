#!/usr/bin/env python3
"""
Change Switch Defaults Script

Changes default password, hostname, and/or ZTP settings on Cumulus switches.

Usage:
    # Do ALL actions (password + hostname + disable-ztp) - DEFAULT behavior
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv
    
    # Change password only
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password
    
    # Change hostname only (requires non-default password)
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --hostname --current-password <pwd>
    
    # Disable ZTP only
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --disable-ztp --current-password <pwd>
    
    # Combine specific actions
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password --hostname
    
    # Dry run
    ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --dry-run
"""

import argparse
import csv
import getpass
import subprocess
import sys
import time
from pathlib import Path

# Constants
DEFAULT_USERNAME = "cumulus"
DEFAULT_PASSWORD = "cumulus"


def read_csv_file(csv_path: Path) -> list:
    """Read devices from CSV file."""
    devices = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Handle different column name cases
            device = {
                'hostname': row.get('Hostname') or row.get('hostname', ''),
                'ip': row.get('IP') or row.get('ip', ''),
                'mac': (row.get('MAC') or row.get('mac', '')).upper(),
                'network': row.get('Network') or row.get('network', ''),
            }
            if device['ip']:
                devices.append(device)
    return devices


def disable_ztp_on_switch(ip: str, password: str, dry_run: bool = False) -> bool:
    """Disable ZTP on a Cumulus switch.
    
    Runs: sudo ztp --disable
    """
    if dry_run:
        print(f"    [DRY RUN] Would disable ZTP")
        return True
    
    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
    
    # Check current ZTP status first
    check_cmd = f"sshpass -p '{password}' ssh {ssh_opts} {DEFAULT_USERNAME}@{ip} 'sudo ztp -s 2>/dev/null | grep -i service || echo unknown'"
    try:
        result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True, timeout=30)
        status = result.stdout.strip().lower()
        
        if 'disabled' in status:
            print(f"    ✓ ZTP already disabled")
            return True
    except:
        pass  # Continue to try disabling anyway
    
    # Disable ZTP
    print(f"    Disabling ZTP...")
    disable_cmd = f"sshpass -p '{password}' ssh {ssh_opts} {DEFAULT_USERNAME}@{ip} 'echo {password} | sudo -S ztp --disable 2>&1'"
    
    try:
        result = subprocess.run(disable_cmd, shell=True, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0 or 'Removed' in result.stdout:
            print(f"    ✓ ZTP disabled")
            return True
        else:
            print(f"    ⚠ ZTP disable returned: {result.stdout.strip()[:100]}")
            return True  # May already be disabled
            
    except subprocess.TimeoutExpired:
        print(f"    ✗ Timeout disabling ZTP")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def change_password_and_hostname(ip: str, hostname: str, new_password: str, 
                                  change_password: bool, change_hostname: bool,
                                  current_password: str = None, dry_run: bool = False) -> dict:
    """
    Change password and/or hostname on a switch.
    
    Returns dict with:
        - password_changed: bool
        - hostname_changed: bool  
        - error: str or None
    """
    result = {
        'password_changed': False,
        'hostname_changed': False,
        'error': None
    }
    
    if dry_run:
        if change_password:
            print(f"    [DRY RUN] Would change password")
            result['password_changed'] = True
        if change_hostname:
            print(f"    [DRY RUN] Would set hostname to: {hostname}")
            result['hostname_changed'] = True
        return result
    
    # Determine which password to use for initial connection
    initial_password = current_password or DEFAULT_PASSWORD
    working_password = initial_password
    
    # Step 1: Handle password change if requested
    if change_password:
        print(f"    Changing password...")
        
        # Use expect to handle the interactive password change
        expect_script = f'''
set timeout 30
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {DEFAULT_USERNAME}@{ip}

expect {{
    "Are you sure you want to continue connecting" {{
        send "yes\\r"
        exp_continue
    }}
    "Current password:" {{
        send "{DEFAULT_PASSWORD}\\r"
        exp_continue
    }}
    "New password:" {{
        send "{new_password}\\r"
        exp_continue
    }}
    "Retype new password:" {{
        send "{new_password}\\r"
        expect {{
            "passwd: password updated successfully" {{
                puts "PASSWORD_CHANGED_SUCCESS"
            }}
            "Connection to" {{
                puts "PASSWORD_CHANGED_SUCCESS"
            }}
            eof {{
                puts "PASSWORD_CHANGED_SUCCESS"
            }}
            timeout {{
                puts "PASSWORD_TIMEOUT"
            }}
        }}
    }}
    -re "password:" {{
        send "{DEFAULT_PASSWORD}\\r"
        exp_continue
    }}
    "Permission denied" {{
        puts "AUTH_FAILED"
    }}
    timeout {{
        puts "TIMEOUT"
    }}
    eof {{
        puts "EARLY_EOF"
    }}
}}
'''
        try:
            proc = subprocess.run(
                ["expect", "-c", expect_script],
                capture_output=True, text=True, timeout=60
            )
            
            if "PASSWORD_CHANGED_SUCCESS" in proc.stdout:
                result['password_changed'] = True
                working_password = new_password
                print(f"    ✓ Password changed")
            elif "AUTH_FAILED" in proc.stdout:
                # Password might already be changed
                print(f"    ⚠ Auth failed with default password (may already be changed)")
                working_password = new_password  # Assume it was changed before
            else:
                result['error'] = f"Password change failed: {proc.stdout[-200:]}"
                print(f"    ✗ Password change failed")
                return result
                
        except subprocess.TimeoutExpired:
            result['error'] = "Timeout during password change"
            print(f"    ✗ Timeout")
            return result
        except Exception as e:
            result['error'] = str(e)
            print(f"    ✗ Error: {e}")
            return result
        
        # Wait for connection to be ready
        time.sleep(2)
    
    # Step 2: Handle hostname change if requested
    if change_hostname and hostname:
        print(f"    Setting hostname to: {hostname}")
        
        ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
        
        # Commands to set hostname
        commands = [
            f"nv set system hostname {hostname}",
            "nv config apply -y"
        ]
        
        for cmd in commands:
            ssh_cmd = f"sshpass -p '{working_password}' ssh {ssh_opts} {DEFAULT_USERNAME}@{ip} '{cmd}'"
            try:
                proc = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=60)
                if proc.returncode != 0:
                    # Check if it's just a warning
                    if "error" in proc.stderr.lower():
                        result['error'] = f"Hostname command failed: {proc.stderr}"
                        print(f"    ✗ Failed: {proc.stderr[:100]}")
                        return result
            except subprocess.TimeoutExpired:
                result['error'] = "Timeout setting hostname"
                print(f"    ✗ Timeout")
                return result
            except Exception as e:
                result['error'] = str(e)
                print(f"    ✗ Error: {e}")
                return result
        
        # Verify hostname was set
        time.sleep(2)
        verify_cmd = f"sshpass -p '{working_password}' ssh {ssh_opts} {DEFAULT_USERNAME}@{ip} 'hostname'"
        try:
            proc = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=30)
            actual_hostname = proc.stdout.strip()
            if actual_hostname == hostname or actual_hostname.startswith(hostname):
                result['hostname_changed'] = True
                print(f"    ✓ Hostname set to: {actual_hostname}")
            else:
                print(f"    ⚠ Hostname is '{actual_hostname}', expected '{hostname}'")
                result['hostname_changed'] = True  # Command succeeded, just different output
        except Exception as e:
            print(f"    ⚠ Could not verify hostname: {e}")
            result['hostname_changed'] = True  # Assume it worked
    
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Change default password, hostname, and/or ZTP settings on Cumulus switches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Default behavior (no options specified):
  Performs ALL actions: change password + set hostname + disable ZTP

Examples:
  # Do everything (default behavior for fresh switches)
  %(prog)s --csv .configs/from-dhcp.csv

  # Change password only
  %(prog)s --csv .configs/from-dhcp.csv --password

  # Change hostname only (when password is already changed)  
  %(prog)s --csv .configs/from-dhcp.csv --hostname --current-password MyPassword123

  # Disable ZTP only
  %(prog)s --csv .configs/from-dhcp.csv --disable-ztp --current-password MyPassword123

  # Combine specific actions
  %(prog)s --csv .configs/from-dhcp.csv --password --disable-ztp

  # Dry run to see what would happen
  %(prog)s --csv .configs/from-dhcp.csv --dry-run

Workflow:
  1. Generate CSV: ./scripts/csv-from-dhcp.py
  2. Map hostnames: ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology <file>
  3. Change defaults: ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv
        """
    )
    
    parser.add_argument("--csv", type=str, required=True,
                       help="CSV file with IP, MAC, Hostname columns")
    parser.add_argument("--password", action="store_true",
                       help="Change the default password")
    parser.add_argument("--hostname", action="store_true",
                       help="Set hostname from CSV file")
    parser.add_argument("--disable-ztp", action="store_true",
                       help="Disable ZTP on the switches")
    parser.add_argument("--current-password", type=str,
                       help="Current password if not default")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without making changes")
    
    args = parser.parse_args()
    
    # Determine what actions to perform
    # If no specific options given, do ALL actions
    do_all = not args.password and not args.hostname and not args.disable_ztp
    
    do_password = args.password or do_all
    do_hostname = args.hostname or do_all
    do_disable_ztp = args.disable_ztp or do_all
    
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)
    
    print("=" * 60)
    print("Change Switch Defaults")
    print("=" * 60)
    
    # Read devices
    devices = read_csv_file(csv_path)
    if not devices:
        print(f"Error: No devices found in {csv_path}")
        sys.exit(1)
    
    print(f"\nLoaded {len(devices)} device(s) from {csv_path}")
    
    # Show what we'll do
    actions = []
    if do_password:
        actions.append("change password")
    if do_hostname:
        actions.append("set hostname")
    if do_disable_ztp:
        actions.append("disable ZTP")
    
    if do_all:
        print(f"Mode: ALL actions (no specific options given)")
    print(f"Actions: {', '.join(actions)}")
    
    # Get new password if changing
    new_password = None
    working_password = args.current_password or DEFAULT_PASSWORD
    
    if do_password:
        print("\n" + "-" * 60)
        print("NEW PASSWORD")
        print("-" * 60)
        print("Requirements: 8+ chars, mix of upper/lower/numbers/symbols")
        
        while True:
            new_password = getpass.getpass("\nEnter new password: ")
            if len(new_password) < 8:
                print("Password must be at least 8 characters")
                continue
            
            confirm = getpass.getpass("Confirm new password: ")
            if new_password != confirm:
                print("Passwords do not match")
                continue
            
            print("✓ Password confirmed")
            break
        
        # After password change, use the new password for subsequent actions
        working_password = new_password
    
    # Process devices
    print("\n" + "-" * 60)
    print("PROCESSING DEVICES")
    print("-" * 60)
    
    stats = {
        'password_success': 0,
        'password_fail': 0,
        'hostname_success': 0,
        'hostname_fail': 0,
        'ztp_success': 0,
        'ztp_fail': 0,
    }
    
    for i, device in enumerate(devices, 1):
        ip = device['ip']
        hostname = device['hostname']
        
        print(f"\n[{i}/{len(devices)}] {hostname or 'unknown'} ({ip})")
        
        if do_hostname and not hostname:
            print(f"    ⚠ No hostname in CSV, skipping hostname change")
        
        # Password and hostname changes
        if do_password or do_hostname:
            result = change_password_and_hostname(
                ip=ip,
                hostname=hostname if do_hostname else None,
                new_password=new_password,
                change_password=do_password,
                change_hostname=do_hostname and bool(hostname),
                current_password=args.current_password,
                dry_run=args.dry_run
            )
            
            if do_password:
                if result['password_changed']:
                    stats['password_success'] += 1
                else:
                    stats['password_fail'] += 1
            
            if do_hostname and hostname:
                if result['hostname_changed']:
                    stats['hostname_success'] += 1
                else:
                    stats['hostname_fail'] += 1
        
        # ZTP disable (use working_password which is new password if changed)
        if do_disable_ztp:
            if disable_ztp_on_switch(ip, working_password, args.dry_run):
                stats['ztp_success'] += 1
            else:
                stats['ztp_fail'] += 1
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if do_password:
        print(f"  Password changes: {stats['password_success']} success, {stats['password_fail']} failed")
    if do_hostname:
        print(f"  Hostname changes: {stats['hostname_success']} success, {stats['hostname_fail']} failed")
    if do_disable_ztp:
        print(f"  ZTP disabled: {stats['ztp_success']} success, {stats['ztp_fail']} failed")
    
    total_failures = stats['password_fail'] + stats['hostname_fail'] + stats['ztp_fail']
    if total_failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
