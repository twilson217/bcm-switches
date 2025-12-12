#!/usr/bin/env python3
"""
Map hostnames in a CSV file using MAC addresses from a topology JSON file.

Usage:
    ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology docs/test-topology.json
"""

import argparse
import csv
import json
import sys
from pathlib import Path


def build_mac_to_hostname_map(topology_data: dict) -> dict:
    """
    Build a case-insensitive MAC address to hostname mapping from topology data.
    
    Args:
        topology_data: Parsed JSON topology data
        
    Returns:
        Dictionary mapping lowercase MAC addresses to hostnames
    """
    mac_to_hostname = {}
    
    links = topology_data.get("content", {}).get("links", [])
    
    for link in links:
        for endpoint in link:
            # Skip string endpoints like "outbound"
            if isinstance(endpoint, dict):
                mac = endpoint.get("mac")
                node = endpoint.get("node")
                if mac and node:
                    # Store with lowercase MAC for case-insensitive lookup
                    mac_to_hostname[mac.lower()] = node
    
    return mac_to_hostname


def update_csv_hostnames(csv_path: Path, mac_to_hostname: dict) -> tuple[list, int]:
    """
    Read CSV and update hostnames based on MAC address lookup.
    
    Args:
        csv_path: Path to the CSV file
        mac_to_hostname: MAC to hostname mapping (lowercase MAC keys)
        
    Returns:
        Tuple of (updated rows, count of updates made)
    """
    updated_rows = []
    updates_made = 0
    
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        
        for row in reader:
            mac = row.get("MAC", "")
            if mac:
                # Case-insensitive lookup
                hostname = mac_to_hostname.get(mac.lower())
                if hostname and hostname != row.get("Hostname"):
                    row["Hostname"] = hostname
                    updates_made += 1
            updated_rows.append(row)
    
    return fieldnames, updated_rows, updates_made


def write_csv(csv_path: Path, fieldnames: list, rows: list) -> None:
    """Write rows back to CSV file."""
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Map hostnames in CSV using MAC addresses from topology JSON"
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to the CSV file to update"
    )
    parser.add_argument(
        "--topology",
        required=True,
        help="Path to the topology JSON file"
    )
    
    args = parser.parse_args()
    
    csv_path = Path(args.csv)
    topology_path = Path(args.topology)
    
    # Validate files exist
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    
    if not topology_path.exists():
        print(f"Error: Topology file not found: {topology_path}", file=sys.stderr)
        sys.exit(1)
    
    # Load topology and build MAC mapping
    with open(topology_path, "r") as f:
        topology_data = json.load(f)
    
    mac_to_hostname = build_mac_to_hostname_map(topology_data)
    print(f"Loaded {len(mac_to_hostname)} MAC-to-hostname mappings from topology")
    
    # Update CSV hostnames
    fieldnames, updated_rows, updates_made = update_csv_hostnames(csv_path, mac_to_hostname)
    
    # Write updated CSV
    write_csv(csv_path, fieldnames, updated_rows)
    
    print(f"Updated {updates_made} hostname(s) in {csv_path}")


if __name__ == "__main__":
    main()

