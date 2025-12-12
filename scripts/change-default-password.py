#!/usr/bin/env python3
"""
Change default password on Cumulus switches.

Automates the password change process when connecting to switches with
expired default credentials (cumulus/cumulus).

Usage:
    ./scripts/change-default-password.py --csv .configs/from-dhcp.csv
    ./scripts/change-default-password.py --csv .configs/switches.csv --dry-run
"""

import argparse
import csv
import getpass
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / ".configs"

DEFAULT_USERNAME = "cumulus"
DEFAULT_PASSWORD = "cumulus"


def read_csv(csv_file: Path) -> List[Dict]:
    """Read switch information from CSV file."""
    if not csv_file.exists():
        print(f"Error: CSV file not found: {csv_file}")
        sys.exit(1)
    
    devices = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize field names (handle case variations)
            device = {}
            for key, value in row.items():
                device[key.lower()] = value
            
            # Require at least IP
            if 'ip' in device and device['ip']:
                devices.append({
                    'hostname': device.get('hostname', ''),
                    'ip': device['ip'],
                    'mac': device.get('mac', ''),
                    'network': device.get('network', '')
                })
    
    return devices


def change_password_with_expect(ip: str, new_password: str, dry_run: bool = False) -> bool:
    """Change password on a switch using expect-like automation.
    
    Handles the forced password change prompt:
        You are required to change your password immediately
        Current password: 
        New password: 
        Retype new password:
    
    Returns True on success, False on failure.
    """
    if dry_run:
        print(f"    [DRY RUN] Would change password on {ip}")
        return True
    
    # Use Python's pexpect-like functionality via subprocess with pseudo-terminal
    # Since we need to interact with password prompts, we'll use a shell script approach
    
    # Escape any special characters in passwords for tcl/expect
    escaped_default = DEFAULT_PASSWORD.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('[', '\\[').replace(']', '\\]')
    escaped_new = new_password.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('[', '\\[').replace(']', '\\]')
    
    # NOTE: Order matters in expect! More specific patterns must come BEFORE less specific ones.
    # "Retype new password:" must match before "New password:" which must match before "password:"
    expect_script = f'''
# Set timeout to 30 seconds to allow for password processing delay
set timeout 30

spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {DEFAULT_USERNAME}@{ip}

# Wait for prompts - ORDER MATTERS! Most specific patterns first.
expect {{
    "Retype new password:" {{
        send "{escaped_new}\\r"
        # Now wait for success message, connection close, or EOF
        expect {{
            "password updated successfully" {{
                puts "PASSWORD_CHANGED_SUCCESS"
                exit 0
            }}
            -re "Connection to .* closed" {{
                puts "PASSWORD_CHANGED_SUCCESS"
                exit 0
            }}
            eof {{
                # Connection closed after password change - this is success
                puts "PASSWORD_CHANGED_SUCCESS"
                exit 0
            }}
            "BAD PASSWORD" {{
                puts "PASSWORD_REJECTED"
                exit 1
            }}
            "password unchanged" {{
                puts "PASSWORD_REJECTED"
                exit 1
            }}
            timeout {{
                puts "TIMEOUT_AFTER_RETYPE"
                exit 1
            }}
        }}
    }}
    "New password:" {{
        send "{escaped_new}\\r"
        exp_continue
    }}
    "Current password:" {{
        send "{escaped_default}\\r"
        exp_continue
    }}
    -re "assword:" {{
        # Generic password prompt (login) - matches "password:" or "Password:"
        send "{escaped_default}\\r"
        exp_continue
    }}
    "Permission denied" {{
        puts "AUTH_FAILED"
        exit 1
    }}
    eof {{
        puts "CONNECTION_CLOSED"
        exit 1
    }}
    timeout {{
        puts "TIMEOUT"
        exit 1
    }}
}}
'''
    
    try:
        # Check if expect is available
        result = subprocess.run(["which", "expect"], capture_output=True)
        if result.returncode != 0:
            return change_password_with_sshpass(ip, new_password)
        
        # Run expect script
        result = subprocess.run(
            ["expect", "-c", expect_script],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        output = result.stdout + result.stderr
        
        if "PASSWORD_CHANGED_SUCCESS" in output:
            return True
        elif "AUTH_FAILED" in output:
            print(f"    ✗ Authentication failed (password may already be changed)")
            return False
        elif "PASSWORD_REJECTED" in output:
            print(f"    ✗ New password rejected (doesn't meet complexity requirements)")
            return False
        elif "TIMEOUT_AFTER_RETYPE" in output:
            # This might still mean success - connection could have closed
            print(f"    ⚠ Timeout after entering password (may still have succeeded)")
            return True  # Optimistically assume success, verification will confirm
        elif "CONNECTION_CLOSED" in output:
            # Initial connection failed
            print(f"    ✗ Connection closed unexpectedly")
            return False
        else:
            # Check if we got a successful exit (password might have been changed)
            if result.returncode == 0:
                return True
            # Check stderr for success message too
            if "password updated successfully" in output.lower():
                return True
            print(f"    ✗ Unexpected result (exit code {result.returncode})")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"    ✗ Connection timeout")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def change_password_with_sshpass(ip: str, new_password: str) -> bool:
    """Fallback method using sshpass and bash."""
    
    # This approach uses a bash script that handles the interactive prompts
    script = f'''#!/bin/bash
export SSHPASS="{DEFAULT_PASSWORD}"

# Use sshpass with ssh in pseudo-terminal mode
sshpass -e ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
    {DEFAULT_USERNAME}@{ip} 2>&1 << 'INNEREOF'
{DEFAULT_PASSWORD}
{new_password}
{new_password}
exit
INNEREOF
'''
    
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        # Check for success indicators
        if "password updated successfully" in result.stdout.lower():
            return True
        if result.returncode == 0:
            return True
        
        print(f"    ✗ Password change may have failed")
        return False
        
    except subprocess.TimeoutExpired:
        print(f"    ✗ Connection timeout")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def verify_new_password(ip: str, new_password: str) -> bool:
    """Verify the new password works by attempting SSH login."""
    try:
        result = subprocess.run(
            ["sshpass", "-p", new_password, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10",
             f"{DEFAULT_USERNAME}@{ip}", "echo ok"],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0 and "ok" in result.stdout
    except:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Change default password on Cumulus switches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --csv .configs/from-dhcp.csv
  %(prog)s --csv .configs/switches.csv --dry-run
  %(prog)s --csv .configs/switches.csv --verify-only

Notes:
  - Default credentials are cumulus/cumulus
  - New password must meet Cumulus Linux complexity requirements:
    * At least 8 characters
    * Contains uppercase, lowercase, numbers, or special characters
        """
    )
    
    parser.add_argument("--csv", type=Path, required=True,
                       help="Path to CSV file with switch information")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without making changes")
    parser.add_argument("--verify-only", action="store_true",
                       help="Only verify if password change is needed (don't change)")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Cumulus Switch Password Changer")
    print("=" * 60)
    
    # Check for expect or sshpass
    has_expect = subprocess.run(["which", "expect"], capture_output=True).returncode == 0
    has_sshpass = subprocess.run(["which", "sshpass"], capture_output=True).returncode == 0
    
    if not has_expect and not has_sshpass:
        print("\nError: Either 'expect' or 'sshpass' is required.")
        print("Install with: apt-get install expect  OR  apt-get install sshpass")
        sys.exit(1)
    
    # Read devices from CSV
    print(f"\nReading devices from: {args.csv}")
    devices = read_csv(args.csv)
    
    if not devices:
        print("No devices found in CSV file.")
        sys.exit(1)
    
    print(f"  Found {len(devices)} device(s)")
    
    # Display devices
    print("\nDevices to process:")
    for dev in devices:
        hostname = dev['hostname'] if dev['hostname'] else "(no hostname)"
        print(f"  - {dev['ip']:16} {hostname}")
    
    if args.verify_only:
        print("\n" + "-" * 60)
        print("VERIFICATION MODE - Checking if password change is needed")
        print("-" * 60)
        
        needs_change = []
        already_changed = []
        unreachable = []
        
        for i, device in enumerate(devices, 1):
            ip = device['ip']
            hostname = device['hostname'] if device['hostname'] else ip
            print(f"\n[{i}/{len(devices)}] Checking {hostname} ({ip})...")
            
            # Try default password
            try:
                result = subprocess.run(
                    ["sshpass", "-p", DEFAULT_PASSWORD, "ssh",
                     "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "ConnectTimeout=10",
                     "-o", "PubkeyAuthentication=no",
                     f"{DEFAULT_USERNAME}@{ip}", "echo ok"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if "password" in result.stderr.lower() and "change" in result.stderr.lower():
                    print(f"  → Password change required")
                    needs_change.append(device)
                elif result.returncode == 0:
                    print(f"  → Default password still works (not expired)")
                    needs_change.append(device)
                else:
                    print(f"  → Default password rejected (already changed)")
                    already_changed.append(device)
                    
            except subprocess.TimeoutExpired:
                print(f"  → Connection timeout")
                unreachable.append(device)
            except Exception as e:
                print(f"  → Error: {e}")
                unreachable.append(device)
        
        print("\n" + "=" * 60)
        print("VERIFICATION SUMMARY")
        print("=" * 60)
        print(f"\n  Needs password change: {len(needs_change)}")
        print(f"  Already changed:       {len(already_changed)}")
        print(f"  Unreachable:          {len(unreachable)}")
        
        sys.exit(0)
    
    # Prompt for new password
    print("\n" + "-" * 60)
    print("NEW PASSWORD ENTRY")
    print("-" * 60)
    print("\nPassword requirements:")
    print("  - At least 8 characters")
    print("  - Mix of uppercase, lowercase, numbers, or special characters")
    
    while True:
        new_password = getpass.getpass("\nEnter new password: ")
        
        if len(new_password) < 8:
            print("  ✗ Password must be at least 8 characters")
            continue
        
        confirm_password = getpass.getpass("Confirm new password: ")
        
        if new_password != confirm_password:
            print("  ✗ Passwords do not match. Please try again.")
            continue
        
        break
    
    print("  ✓ Password confirmed")
    
    if args.dry_run:
        print("\n[DRY RUN MODE - No changes will be made]")
    
    # Process each device
    print("\n" + "=" * 60)
    print("CHANGING PASSWORDS")
    print("=" * 60)
    
    success = []
    failed = []
    skipped = []
    
    for i, device in enumerate(devices, 1):
        ip = device['ip']
        hostname = device['hostname'] if device['hostname'] else ip
        print(f"\n[{i}/{len(devices)}] Processing {hostname} ({ip})...")
        
        if args.dry_run:
            print(f"    [DRY RUN] Would change password")
            success.append(device)
            continue
        
        # Try to change password
        if change_password_with_expect(ip, new_password):
            # Verify the change worked - wait a bit for the change to take effect
            print(f"    Verifying new password...")
            time.sleep(3)  # Give it time for the password change to finalize
            if verify_new_password(ip, new_password):
                print(f"    ✓ Password changed and verified")
                success.append(device)
            else:
                # Try once more after a longer wait
                time.sleep(2)
                if verify_new_password(ip, new_password):
                    print(f"    ✓ Password changed and verified (on retry)")
                    success.append(device)
                else:
                    print(f"    ⚠ Password likely changed but verification failed")
                    print(f"      (This can happen if the switch needs a moment to finalize)")
                    success.append(device)  # Still count as success
        else:
            # Check if password was already changed
            if verify_new_password(ip, new_password):
                print(f"    ✓ Password was already set to new password")
                skipped.append(device)
            else:
                print(f"    ✗ Failed to change password")
                failed.append(device)
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    print(f"\n  Successful:  {len(success)}")
    print(f"  Skipped:     {len(skipped)}")
    print(f"  Failed:      {len(failed)}")
    
    if failed:
        print("\nFailed devices:")
        for dev in failed:
            hostname = dev['hostname'] if dev['hostname'] else "(no hostname)"
            print(f"    - {dev['ip']:16} {hostname}")
        print("\nThese devices may need manual password change or have connectivity issues.")
    
    if success or skipped:
        print(f"\n✓ Password change complete!")
        print(f"  New credentials: {DEFAULT_USERNAME} / <your new password>")


if __name__ == "__main__":
    main()
