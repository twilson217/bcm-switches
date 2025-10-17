#!/usr/bin/env python3
"""
Script to add Cumulus switches to BCM from a CSV file with complete configuration.
Reads device information from cumulus.csv and performs:
1. Device addition (IP, MAC, network, client daemon)
2. Access settings configuration (SSH credentials)
3. ZTP settings configuration (API enablement)
4. Device initialization
All using cmsh -c command format for automation.
"""

import csv
import subprocess
import sys
import argparse
from pathlib import Path


def read_devices_from_csv(csv_file):
    """Read device information from CSV file."""
    devices = []
    
    try:
        with open(csv_file, 'r', newline='') as file:
            # Detect delimiter (tab or comma)
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


def generate_cmsh_add_command(device):
    """Generate cmsh command for adding a device to BCM."""
    command = f"cmsh -c 'device; add switch {device['hostname']}; commit'"
    return command


def generate_cmsh_update_command(device):
    """Generate cmsh command for updating device properties in BCM."""
    command = (
        f"cmsh -c 'device; use {device['hostname']}; "
        f"set ip {device['ip']}; set mac {device['mac']}; "
        f"set network {device['network']}; set hasclientdaemon yes; commit'"
    )
    return command


def generate_accesssettings_command(device, username="cumulus", password="1234"):
    """Generate cmsh command for setting access credentials."""
    command = (
        f"cmsh -c 'device; use {device['hostname']}; "
        f"accesssettings; set username {username}; set password {password}; "
        f"set -e force true; commit'"
    )
    return command


def generate_ztpsettings_command(device):
    """Generate cmsh command for enabling API and ZTP settings."""
    command = (
        f"cmsh -c 'device; use {device['hostname']}; "
        f"ztpsettings; set enableapi yes; commit'"
    )
    return command


def generate_initialize_command(device):
    """Generate cmsh command for initializing the device."""
    command = f"cmsh -c 'device; use {device['hostname']}; initialize'"
    return command


def check_device_exists(hostname):
    """Check if a device already exists in BCM."""
    command = f"cmsh -c 'device; use {hostname}; show'"
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        # If the command succeeds, the device exists
        return result.returncode == 0
    except Exception:
        return False


def execute_command(command, dry_run=False):
    """Execute the cmsh command or just print it if dry_run is True."""
    if dry_run:
        print(f"[DRY RUN] {command}")
        return True
    else:
        print(f"Executing: {command}")
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"✓ Success: Device added successfully")
                if result.stdout:
                    print(f"  Output: {result.stdout.strip()}")
                return True
            else:
                print(f"✗ Error: Command failed with return code {result.returncode}")
                if result.stderr:
                    print(f"  Error: {result.stderr.strip()}")
                return False
        except Exception as e:
            print(f"✗ Exception executing command: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Add Cumulus switches to BCM from CSV file with full configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script performs the complete BCM configuration for Cumulus switches:
1. Adds the device to BCM with IP, MAC, and network settings
2. Configures SSH access credentials (username/password)
3. Enables API and ZTP settings
4. Initializes the device

Examples:
  %(prog)s                          # Configure devices from cumulus.csv (dry run)
  %(prog)s --execute               # Actually execute all configuration commands
  %(prog)s --csv devices.csv       # Use different CSV file
  %(prog)s --execute --force       # Execute without prompting for existing devices
  %(prog)s --execute --username admin --password secret  # Custom credentials
        """
    )
    
    parser.add_argument(
        '--csv', 
        default='cumulus.csv',
        help='CSV file containing device information (default: cumulus.csv)'
    )
    
    parser.add_argument(
        '--execute', 
        action='store_true',
        help='Actually execute the cmsh commands (default: dry run mode)'
    )
    
    parser.add_argument(
        '--username',
        default='cumulus',
        help='Username for SSH access to switches (default: cumulus)'
    )
    
    parser.add_argument(
        '--password',
        default='1234',
        help='Password for SSH access to switches (default: 1234)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite existing devices without prompting'
    )
    
    args = parser.parse_args()
    
    # Check if CSV file exists
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV file '{args.csv}' not found.")
        sys.exit(1)
    
    # Read devices from CSV
    print(f"Reading devices from: {args.csv}")
    devices = read_devices_from_csv(args.csv)
    
    if not devices:
        print("No valid devices found in CSV file.")
        sys.exit(1)
    
    print(f"Found {len(devices)} devices to configure:")
    for device in devices:
        print(f"  - {device['hostname']} ({device['ip']}) - MAC: {device['mac']}")
    
    # Check for existing devices and prompt if needed
    if not args.force and args.execute:
        existing_devices = []
        for device in devices:
            if check_device_exists(device['hostname']):
                existing_devices.append(device['hostname'])
        
        if existing_devices:
            print(f"\nWarning: The following devices already exist in BCM:")
            for hostname in existing_devices:
                print(f"  - {hostname}")
            print(f"\nThis will update their configuration (IP, MAC, network, access settings, ZTP settings).")
            
            response = input("Do you want to continue and update existing devices? (y/N): ").strip().lower()
            if response not in ['y', 'yes']:
                print("Operation cancelled.")
                sys.exit(0)
    
    if not args.execute:
        print("\n" + "="*60)
        print("DRY RUN MODE - Commands will be printed but not executed")
        print("Use --execute flag to actually run the commands")
        print("="*60)
    
    print(f"\nProcessing devices...")
    
    success_count = 0
    failed_count = 0
    
    for i, device in enumerate(devices, 1):
        print(f"\n[{i}/{len(devices)}] Processing {device['hostname']}...")
        
        device_success = True
        device_exists = False
        
        # Check if device already exists (only in execute mode)
        if args.execute:
            if check_device_exists(device['hostname']):
                print(f"  Device {device['hostname']} already exists in BCM. Updating properties.")
                device_exists = True
            else:
                print(f"  Device {device['hostname']} does not exist in BCM. Adding it.")
                device_exists = False
        else:
            # In dry run mode, assume device doesn't exist for demonstration
            print(f"  Checking if device exists (dry run - assuming new device)...")
        
        # Step 1: Add device to BCM (if it doesn't exist)
        if not device_exists:
            print(f"  Step 1a/4: Adding device to BCM...")
            command = generate_cmsh_add_command(device)
            if not execute_command(command, dry_run=not args.execute):
                device_success = False
        
        # Step 1b: Update device properties
        if device_success:
            action = "Updating" if device_exists else "Setting"
            print(f"  Step 1b/4: {action} device properties...")
            command = generate_cmsh_update_command(device)
            if not execute_command(command, dry_run=not args.execute):
                device_success = False
        
        # Step 2: Configure access settings
        if device_success:
            print(f"  Step 2/4: Configuring access settings...")
            command = generate_accesssettings_command(device, args.username, args.password)
            if not execute_command(command, dry_run=not args.execute):
                device_success = False
        
        # Step 3: Configure ZTP settings
        if device_success:
            print(f"  Step 3/4: Configuring ZTP settings...")
            command = generate_ztpsettings_command(device)
            if not execute_command(command, dry_run=not args.execute):
                device_success = False
        
        # Step 4: Initialize device
        if device_success:
            print(f"  Step 4/4: Initializing device...")
            command = generate_initialize_command(device)
            if not execute_command(command, dry_run=not args.execute):
                device_success = False
        
        if device_success:
            success_count += 1
            print(f"✓ Device {device['hostname']} configured successfully")
        else:
            failed_count += 1
            print(f"✗ Device {device['hostname']} configuration failed")
    
    # Summary
    print(f"\n" + "="*60)
    print(f"SUMMARY:")
    print(f"Total devices: {len(devices)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {failed_count}")
    
    if not args.execute:
        print(f"\nNote: This was a dry run. Use --execute to actually add devices to BCM.")
    
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main() 