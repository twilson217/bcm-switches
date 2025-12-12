#!/usr/bin/env python3
"""
Change Default Hostname Script

Sets hostnames on Cumulus switches based on MAC address to hostname mapping.

This script:
1. Gets MAC-to-hostname mapping (from DHCP leases + topology, or a mapping file)
2. SSHs to each switch IP
3. Discovers the MAC address
4. Looks up the correct hostname
5. Sets the hostname using NVUE commands

Usage:
    # Auto-discover from DHCP leases and topology file
    python3 scripts/change-default-hostname.py --topology scripts/tests/sample-configs/test-topology.json
    
    # Use a specific mapping file (JSON format: {"MAC": "hostname", ...})
    python3 scripts/change-default-hostname.py --mapping mac-to-hostname.json
    
    # Specify IPs directly
    python3 scripts/change-default-hostname.py --topology <file> --ips 192.168.200.161-166
    
    # Dry run (show what would be done)
    python3 scripts/change-default-hostname.py --topology <file> --dry-run
"""

import argparse
import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIGS_DIR = REPO_DIR / ".configs"
DHCP_LEASES_FILE = Path("/var/lib/dhcpd/dhcpd.leases")

DEFAULT_USERNAME = "cumulus"
DEFAULT_PASSWORD = "cumulus"


def parse_ip_range(ip_input: str) -> list:
    """Parse IP address input in various formats."""
    ips = []
    
    # Split by comma
    parts = [p.strip() for p in ip_input.split(',')]
    
    for part in parts:
        if '-' in part:
            # Check if it's a full range (192.168.0.1-192.168.0.100) or short (192.168.0.1-100)
            if part.count('.') > 3:
                # Full range
                start, end = part.split('-')
                start_parts = start.split('.')
                end_parts = end.split('.')
                start_num = int(start_parts[-1])
                end_num = int(end_parts[-1])
                base = '.'.join(start_parts[:-1])
                for i in range(start_num, end_num + 1):
                    ips.append(f"{base}.{i}")
            else:
                # Short range (192.168.0.1-100)
                base_end = part.split('-')
                base_parts = base_end[0].split('.')
                start_num = int(base_parts[-1])
                end_num = int(base_end[1])
                base = '.'.join(base_parts[:-1])
                for i in range(start_num, end_num + 1):
                    ips.append(f"{base}.{i}")
        else:
            # Single IP
            ips.append(part)
    
    return ips


def get_hostnames_from_topology(topology_file: Path) -> list:
    """Extract switch hostnames from topology file."""
    if not topology_file.exists():
        print(f"Error: Topology file not found: {topology_file}")
        return []
    
    try:
        with open(topology_file, 'r') as f:
            topology = json.load(f)
        
        nodes = topology.get('content', {}).get('nodes', {})
        # Get switch names (spine-*, leaf-*)
        switches = [name for name in nodes.keys() 
                   if name.startswith('spine-') or name.startswith('leaf-')]
        return sorted(switches)
    except Exception as e:
        print(f"Error reading topology file: {e}")
        return []


def parse_dhcp_leases() -> dict:
    """Parse DHCP leases file to get IP -> MAC mapping."""
    if not DHCP_LEASES_FILE.exists():
        print(f"Warning: DHCP leases file not found: {DHCP_LEASES_FILE}")
        return {}
    
    ip_to_mac = {}
    lease_pattern = re.compile(r'lease\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+\{([^}]+)\}')
    mac_pattern = re.compile(r'hardware ethernet\s+([0-9a-fA-F:]{17});')
    
    content = DHCP_LEASES_FILE.read_text()
    for match in lease_pattern.finditer(content):
        ip = match.group(1)
        lease_block = match.group(2)
        
        if "binding state active;" not in lease_block:
            continue
        
        mac_match = mac_pattern.search(lease_block)
        if mac_match:
            mac = mac_match.group(1).upper()
            ip_to_mac[ip] = mac
    
    return ip_to_mac


def run_ssh_command(ip: str, command: str, username: str, password: str, timeout: int = 30) -> tuple:
    """Run SSH command and return (success, output)."""
    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
    
    # Try SSH key first
    ssh_cmd = f"ssh {ssh_opts} -o BatchMode=yes {username}@{ip} '{command}'"
    result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    
    if result.returncode == 0:
        return True, result.stdout.strip()
    
    # Fall back to password auth
    if password:
        ssh_cmd = f"sshpass -p '{password}' ssh {ssh_opts} {username}@{ip} '{command}'"
        try:
            result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "Timeout"
    
    return False, result.stderr.strip()


def get_switch_mac(ip: str, username: str, password: str) -> str:
    """Get MAC address of eth0 interface on a switch."""
    command = "nv show interface eth0 | grep -i mac"
    success, output = run_ssh_command(ip, command, username, password)
    
    if success and output:
        # Parse: "  mac-address              48:b0:2d:3b:c8:e6"
        match = re.search(r'([0-9a-fA-F:]{17})', output)
        if match:
            return match.group(1).upper()
    
    return None


def get_current_hostname(ip: str, username: str, password: str) -> str:
    """Get current hostname of a switch."""
    command = "hostname"
    success, output = run_ssh_command(ip, command, username, password)
    return output if success else None


def set_hostname(ip: str, hostname: str, username: str, password: str, dry_run: bool = False) -> bool:
    """Set hostname on a switch using NVUE."""
    if dry_run:
        print(f"    [DRY RUN] Would set hostname to: {hostname}")
        return True
    
    # Set hostname
    cmd1 = f"nv set system hostname {hostname}"
    success1, output1 = run_ssh_command(ip, cmd1, username, password)
    if not success1:
        print(f"    ✗ Failed to set hostname: {output1}")
        return False
    
    # Apply configuration
    cmd2 = "nv config apply -y"
    success2, output2 = run_ssh_command(ip, cmd2, username, password, timeout=60)
    if not success2:
        print(f"    ✗ Failed to apply config: {output2}")
        return False
    
    return True


def verify_hostname(ip: str, expected: str, username: str, password: str) -> bool:
    """Verify hostname was set correctly."""
    import time
    time.sleep(2)  # Wait for config to apply
    
    current = get_current_hostname(ip, username, password)
    if current == expected:
        return True
    
    # Sometimes hostname command returns FQDN
    if current and current.startswith(expected):
        return True
    
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Set hostnames on Cumulus switches based on MAC address",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discover from DHCP and set hostnames based on topology
  %(prog)s --topology scripts/tests/sample-configs/test-topology.json

  # Use specific IPs
  %(prog)s --topology scripts/tests/sample-configs/test-topology.json --ips 192.168.200.161-166

  # Use a pre-defined MAC-to-hostname mapping file
  %(prog)s --mapping mac-hostnames.json --ips 192.168.200.161-166

  # Dry run to see what would happen
  %(prog)s --topology <file> --dry-run
        """
    )
    
    parser.add_argument("--topology", type=str,
                       help="Topology file to get valid hostnames from")
    parser.add_argument("--mapping", type=str,
                       help="JSON file with MAC-to-hostname mapping")
    parser.add_argument("--ips", type=str,
                       help="IP addresses (e.g., 192.168.200.161-166)")
    parser.add_argument("--username", type=str, default=DEFAULT_USERNAME,
                       help=f"SSH username (default: {DEFAULT_USERNAME})")
    parser.add_argument("--password", type=str,
                       help="SSH password (will prompt if not provided)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without making changes")
    parser.add_argument("--create-mapping", type=str,
                       help="Create a MAC-to-hostname mapping file from current DHCP leases and save to this path")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Change Default Hostname")
    print("=" * 60)
    
    # Handle --create-mapping option
    if args.create_mapping:
        print("\nCreating MAC-to-hostname mapping from DHCP leases...")
        
        if not args.topology:
            print("Error: --topology is required to create mapping")
            sys.exit(1)
        
        valid_hostnames = get_hostnames_from_topology(Path(args.topology))
        if not valid_hostnames:
            print("Error: No valid hostnames found in topology")
            sys.exit(1)
        
        print(f"Valid hostnames from topology: {', '.join(valid_hostnames)}")
        
        ip_to_mac = parse_dhcp_leases()
        if not ip_to_mac:
            print("Error: No active DHCP leases found")
            sys.exit(1)
        
        print(f"Found {len(ip_to_mac)} active DHCP leases")
        
        # Get password for SSH
        password = args.password or getpass.getpass(f"SSH password for {args.username}: ")
        
        # Connect to each IP and get MAC, then prompt for hostname
        mac_to_hostname = {}
        for ip, mac in sorted(ip_to_mac.items()):
            print(f"\n  {ip} (MAC: {mac})")
            current = get_current_hostname(ip, args.username, password)
            print(f"    Current hostname: {current or 'unknown'}")
            
            # Try to match to a valid hostname
            suggested = None
            for h in valid_hostnames:
                if h not in mac_to_hostname.values():
                    suggested = h
                    break
            
            prompt = f"    Hostname [{suggested or 'skip'}]: "
            user_input = input(prompt).strip()
            
            if user_input:
                hostname = user_input
            elif suggested:
                hostname = suggested
            else:
                print("    Skipping...")
                continue
            
            if hostname in valid_hostnames:
                mac_to_hostname[mac] = hostname
                print(f"    ✓ Mapped {mac} -> {hostname}")
            else:
                print(f"    Warning: '{hostname}' not in valid hostnames, skipping")
        
        # Save mapping
        mapping_path = Path(args.create_mapping)
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mapping_path, 'w') as f:
            json.dump(mac_to_hostname, f, indent=2)
        
        print(f"\n✓ Saved mapping to {mapping_path}")
        print(f"  {len(mac_to_hostname)} entries")
        sys.exit(0)
    
    # Normal operation: set hostnames
    
    # Get MAC-to-hostname mapping
    mac_to_hostname = {}
    
    if args.mapping:
        mapping_path = Path(args.mapping)
        if not mapping_path.exists():
            print(f"Error: Mapping file not found: {mapping_path}")
            sys.exit(1)
        
        with open(mapping_path, 'r') as f:
            mac_to_hostname = json.load(f)
        
        # Normalize MACs to uppercase
        mac_to_hostname = {k.upper(): v for k, v in mac_to_hostname.items()}
        print(f"\nLoaded {len(mac_to_hostname)} MAC-to-hostname mappings from {mapping_path}")
    
    elif args.topology:
        # Build mapping from DHCP leases
        print("\nBuilding MAC-to-hostname mapping from DHCP leases...")
        
        valid_hostnames = get_hostnames_from_topology(Path(args.topology))
        if not valid_hostnames:
            print("Error: No valid hostnames found in topology")
            sys.exit(1)
        
        print(f"Valid hostnames: {', '.join(valid_hostnames)}")
        
        # Try to use existing from-dhcp.csv if available
        from_dhcp_csv = CONFIGS_DIR / "from-dhcp.csv"
        if from_dhcp_csv.exists():
            print(f"Found existing {from_dhcp_csv}")
            import csv
            with open(from_dhcp_csv, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Handle different column name cases (MAC vs mac, Hostname vs hostname)
                    mac = (row.get('MAC') or row.get('mac', '')).upper()
                    hostname = row.get('Hostname') or row.get('hostname', '')
                    if mac and hostname and hostname in valid_hostnames:
                        mac_to_hostname[mac] = hostname
            
            if mac_to_hostname:
                print(f"Loaded {len(mac_to_hostname)} mappings from CSV")
        
        if not mac_to_hostname:
            print("\nNo existing mapping found. Run with --create-mapping first,")
            print("or provide a --mapping file.")
            print("\nExample:")
            print(f"  {sys.argv[0]} --topology {args.topology} --create-mapping .configs/mac-hostnames.json")
            sys.exit(1)
    
    else:
        print("Error: Either --topology or --mapping is required")
        sys.exit(1)
    
    # Get IPs to process
    if args.ips:
        ips = parse_ip_range(args.ips)
    else:
        # Get from DHCP leases
        ip_to_mac = parse_dhcp_leases()
        ips = list(ip_to_mac.keys())
    
    if not ips:
        print("Error: No IPs to process")
        sys.exit(1)
    
    print(f"\nProcessing {len(ips)} IP addresses...")
    
    # Get password
    password = args.password or getpass.getpass(f"SSH password for {args.username}: ")
    
    # Process each IP
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    for ip in sorted(ips):
        print(f"\n  [{ip}]")
        
        # Get MAC address
        mac = get_switch_mac(ip, args.username, password)
        if not mac:
            print(f"    ✗ Could not get MAC address")
            fail_count += 1
            continue
        
        print(f"    MAC: {mac}")
        
        # Look up hostname
        hostname = mac_to_hostname.get(mac)
        if not hostname:
            print(f"    ⚠ No hostname mapping for this MAC, skipping")
            skip_count += 1
            continue
        
        # Get current hostname
        current = get_current_hostname(ip, args.username, password)
        print(f"    Current: {current or 'unknown'}")
        print(f"    Target:  {hostname}")
        
        if current == hostname:
            print(f"    ✓ Already correct, skipping")
            skip_count += 1
            continue
        
        # Set hostname
        print(f"    Setting hostname...")
        if set_hostname(ip, hostname, args.username, password, args.dry_run):
            if args.dry_run:
                success_count += 1
            elif verify_hostname(ip, hostname, args.username, password):
                print(f"    ✓ Hostname set to {hostname}")
                success_count += 1
            else:
                print(f"    ⚠ Hostname may not have been set correctly")
                fail_count += 1
        else:
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Skipped: {skip_count}")
    
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

