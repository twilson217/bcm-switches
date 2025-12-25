#!/usr/bin/env python3
"""
Generate switch CSV from DHCP leases.

Parses /var/lib/dhcpd/dhcpd.leases and maps IP addresses to BCM networks
using 'cmsh -c "network list"' output.

Usage:
    ./scripts/csv-from-dhcp.py                    # Output to .configs/from-dhcp.csv
    ./scripts/csv-from-dhcp.py -o custom.csv      # Output to custom file
    ./scripts/csv-from-dhcp.py --filter cumulus   # Only include leases with 'cumulus' in vendor-class
"""

import argparse
import csv
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# BCM version compatibility (cmsh path)
from bcm_compat import get_cmsh_cmd

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / ".configs"
DEFAULT_OUTPUT = CONFIG_DIR / "from-dhcp.csv"
DHCP_LEASES_FILE = Path("/var/lib/dhcpd/dhcpd.leases")


class DHCPLeaseParser:
    """Parse DHCP leases file."""
    
    def __init__(self, leases_file: Path = DHCP_LEASES_FILE):
        self.leases_file = leases_file
    
    def parse(self) -> List[Dict]:
        """Parse DHCP leases file and return list of active leases.
        
        Returns list of dicts with: ip, mac, hostname
        Only returns the most recent lease for each IP (last one wins).
        """
        if not self.leases_file.exists():
            print(f"Error: DHCP leases file not found: {self.leases_file}")
            sys.exit(1)
        
        with open(self.leases_file, 'r') as f:
            content = f.read()
        
        # Parse all lease blocks
        lease_pattern = re.compile(
            r'lease\s+([\d.]+)\s*\{([^}]+)\}',
            re.MULTILINE | re.DOTALL
        )
        
        mac_pattern = re.compile(r'hardware\s+ethernet\s+([\da-fA-F:]+);')
        hostname_pattern = re.compile(r'client-hostname\s+"([^"]+)";')
        vendor_pattern = re.compile(r'set\s+vendor-class-identifier\s*=\s*"([^"]+)";')
        state_pattern = re.compile(r'binding\s+state\s+(\w+);')
        
        # Use dict to keep only most recent lease per IP
        leases_by_ip = {}
        
        for match in lease_pattern.finditer(content):
            ip = match.group(1)
            block = match.group(2)
            
            # Check binding state
            state_match = state_pattern.search(block)
            if state_match and state_match.group(1) != 'active':
                continue
            
            # Extract MAC
            mac_match = mac_pattern.search(block)
            if not mac_match:
                continue
            mac = mac_match.group(1).upper()
            
            # Extract hostname (optional)
            hostname_match = hostname_pattern.search(block)
            hostname = hostname_match.group(1) if hostname_match else ""
            
            # Extract vendor class (optional, for filtering)
            vendor_match = vendor_pattern.search(block)
            vendor_class = vendor_match.group(1) if vendor_match else ""
            
            leases_by_ip[ip] = {
                'ip': ip,
                'mac': mac,
                'hostname': hostname,
                'vendor_class': vendor_class
            }
        
        return list(leases_by_ip.values())


class NetworkMapper:
    """Map IP addresses to BCM networks."""
    
    def __init__(self):
        self.networks = []
        self._cmsh = get_cmsh_cmd()
    
    def detect_networks(self) -> bool:
        """Get available BCM networks using cmsh."""
        try:
            result = subprocess.run(
                [self._cmsh, "-c", "network; list"],
                capture_output=True, text=True, check=True
            )
            self.networks = self._parse_network_list(result.stdout)
            return len(self.networks) > 0
        except subprocess.CalledProcessError as e:
            print(f"Error detecting networks: {e}")
            return False
        except FileNotFoundError:
            print(f"Error: cmsh command not found at '{self._cmsh}'. Make sure you're running on a BCM system.")
            return False
    
    def _parse_network_list(self, output: str) -> List[Dict]:
        """Parse cmsh network list output."""
        networks = []
        lines = output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip header lines
            if line.startswith('Name') or '--' in line and line.count('-') > 10:
                continue
            
            # Parse: Name Type NetmaskBits BaseAddress DomainName IPv6
            parts = line.split()
            if len(parts) >= 4:
                try:
                    name = parts[0]
                    net_type = parts[1]
                    netmask_bits = int(parts[2])
                    base_address = parts[3]
                    
                    if '.' in base_address and 0 <= netmask_bits <= 32:
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
        """Find which network an IP belongs to (most specific match)."""
        try:
            ip_obj = ipaddress.ip_address(ip)
            
            matches = []
            for network in self.networks:
                try:
                    net = ipaddress.ip_network(
                        f"{network['base_address']}/{network['netmask_bits']}",
                        strict=False
                    )
                    if ip_obj in net:
                        matches.append((network['netmask_bits'], network['name']))
                except ValueError:
                    continue
            
            if matches:
                # Most specific match (highest netmask bits)
                matches.sort(reverse=True)
                return matches[0][1]
                
        except ValueError:
            pass
        
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Generate switch CSV from DHCP leases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Output to .configs/from-dhcp.csv
  %(prog)s -o switches.csv           # Output to custom file
  %(prog)s --filter cumulus          # Only Cumulus Linux devices
  %(prog)s --leases /path/to/file    # Use custom leases file
        """
    )
    
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                       help="Output CSV file path (default: .configs/from-dhcp.csv)")
    parser.add_argument("--leases", type=Path, default=DHCP_LEASES_FILE,
                       help="Path to DHCP leases file (default: /var/lib/dhcpd/dhcpd.leases)")
    parser.add_argument("--filter", type=str, default=None,
                       help="Filter leases by vendor-class (e.g., 'cumulus')")
    parser.add_argument("--no-network-map", action="store_true",
                       help="Skip network mapping (leave Network column empty)")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("DHCP Lease to CSV Converter")
    print("=" * 60)
    
    # Parse DHCP leases
    print(f"\nReading DHCP leases from: {args.leases}")
    lease_parser = DHCPLeaseParser(args.leases)
    leases = lease_parser.parse()
    
    print(f"  Found {len(leases)} active lease(s)")
    
    # Filter by vendor class if specified
    if args.filter:
        filter_lower = args.filter.lower()
        leases = [l for l in leases if filter_lower in l.get('vendor_class', '').lower()]
        print(f"  After filtering for '{args.filter}': {len(leases)} lease(s)")
    
    if not leases:
        print("\nNo leases found matching criteria. Exiting.")
        sys.exit(0)
    
    # Map to networks
    network_mapper = NetworkMapper()
    
    if not args.no_network_map:
        print("\nDetecting BCM networks...")
        if network_mapper.detect_networks():
            print(f"  Found {len(network_mapper.networks)} network(s):")
            for net in network_mapper.networks:
                print(f"    - {net['name']}: {net['base_address']}/{net['netmask_bits']}")
        else:
            print("  Warning: Could not detect networks. Network column will be empty.")
    
    # Build output data
    output_data = []
    for lease in leases:
        network = ""
        if not args.no_network_map:
            network = network_mapper.match_ip_to_network(lease['ip']) or ""
        
        output_data.append({
            'Hostname': lease['hostname'],
            'IP': lease['ip'],
            'MAC': lease['mac'],
            'Network': network
        })
    
    # Sort by IP address
    output_data.sort(key=lambda x: tuple(int(p) for p in x['IP'].split('.')))
    
    # Ensure output directory exists
    args.output.parent.mkdir(parents=True, exist_ok=True)
    
    # Write CSV
    with open(args.output, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['Hostname', 'IP', 'MAC', 'Network'])
        writer.writeheader()
        writer.writerows(output_data)
    
    print(f"\n" + "=" * 60)
    print(f"CSV written to: {args.output}")
    print("=" * 60)
    
    # Display summary
    print(f"\nDevices found:")
    for row in output_data:
        hostname_display = row['Hostname'] if row['Hostname'] else "(no hostname)"
        network_display = row['Network'] if row['Network'] else "(no network)"
        print(f"  {row['IP']:16} {row['MAC']:18} {hostname_display:20} {network_display}")
    
    # Warn about missing hostnames
    missing_hostnames = [r for r in output_data if not r['Hostname']]
    if missing_hostnames:
        print(f"\n⚠ Warning: {len(missing_hostnames)} device(s) have no hostname in DHCP.")
        print("  These devices may need to be configured with hostnames before deployment.")
        print("  You can edit the CSV file to add hostnames manually.")


if __name__ == "__main__":
    main()
