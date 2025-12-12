#!/usr/bin/env python3
"""
Test Simulation Reset Script

Resets the test environment by:
1. Removing switches from BCM (if present)
2. Rebuilding switches in NVIDIA Air to factory defaults

This enables automated test loops by providing a clean starting state.

Usage:
    python3 scripts/test-sim-reset.py
    python3 scripts/test-sim-reset.py --skip-bcm     # Skip BCM cleanup
    python3 scripts/test-sim-reset.py --skip-air     # Skip Air rebuild
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' module not found. Install with: pip install requests")
    sys.exit(1)

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / ".configs"
ENV_FILE = CONFIG_DIR / ".env"
SAMPLE_ENV = REPO_DIR / "sample-configs" / "sample.env"
TOPOLOGY_FILE = REPO_DIR / "sample-configs" / "test-topology.json"

# Switches we manage (Cumulus switches that get deployed to BCM)
MANAGED_SWITCHES = ["spine-01", "spine-02", "leaf-01", "leaf-02", "leaf-03", "leaf-04"]


def load_env():
    """Load environment variables from .env file."""
    if not ENV_FILE.exists():
        return None
    
    env_vars = {}
    with open(ENV_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_vars[key.strip()] = value.strip()
    return env_vars


def create_env_file():
    """Create .env file from sample if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    if SAMPLE_ENV.exists():
        import shutil
        shutil.copy(SAMPLE_ENV, ENV_FILE)
        print(f"Created {ENV_FILE} from sample")
    else:
        # Create minimal .env
        content = """# NVIDIA Air API Configuration
# Fill in your credentials below

# NVIDIA Air API Token (get from air.nvidia.com -> Account Settings -> API Tokens)
AIR_API_TOKEN=your_api_token_here

# NVIDIA Air API URL
# External: https://air.nvidia.com/api/v1
# Internal (NVIDIA VPN): https://air-inside.nvidia.com/api/v1
AIR_API_URL=https://air.nvidia.com/api/v1

# Your simulation ID (from the simulation URL)
SIMULATION_ID=your_simulation_id_here
"""
        ENV_FILE.write_text(content)
        print(f"Created {ENV_FILE}")
    
    return False  # Indicates file was just created


def get_switches_from_topology():
    """Get switch names from topology file."""
    if not TOPOLOGY_FILE.exists():
        print(f"Warning: Topology file not found at {TOPOLOGY_FILE}")
        return MANAGED_SWITCHES
    
    try:
        with open(TOPOLOGY_FILE, 'r') as f:
            topology = json.load(f)
        
        nodes = topology.get('content', {}).get('nodes', {})
        # Filter to only Cumulus switches (exclude oob-mgmt-switch, bcm-01, cpu-*, etc.)
        switches = [name for name in nodes.keys() 
                   if name.startswith('spine-') or name.startswith('leaf-')]
        return sorted(switches) if switches else MANAGED_SWITCHES
    except Exception as e:
        print(f"Warning: Could not parse topology file: {e}")
        return MANAGED_SWITCHES


def get_bcm_devices():
    """Get list of devices currently in BCM."""
    try:
        result = subprocess.run(
            ["cmsh", "-c", "device; list"],
            capture_output=True, text=True, check=True
        )
        
        devices = []
        for line in result.stdout.strip().split('\n'):
            if line.strip() and not line.startswith('Type'):
                parts = line.split()
                if len(parts) >= 2:
                    devices.append(parts[1])  # Device name is second column
        return devices
    except subprocess.CalledProcessError as e:
        print(f"Error getting BCM devices: {e}")
        return []
    except FileNotFoundError:
        print("Error: cmsh not found. This script must run on a BCM system.")
        return []


def remove_switches_from_bcm(switches_to_remove):
    """Remove specified switches from BCM."""
    bcm_devices = get_bcm_devices()
    
    removed = []
    for switch in switches_to_remove:
        if switch in bcm_devices:
            print(f"  Removing {switch} from BCM...")
            try:
                result = subprocess.run(
                    ["cmsh", "-c", f"device; remove {switch}; commit"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    removed.append(switch)
                    print(f"    ✓ Removed {switch}")
                else:
                    print(f"    ✗ Failed to remove {switch}: {result.stderr}")
            except Exception as e:
                print(f"    ✗ Error removing {switch}: {e}")
        else:
            print(f"  {switch} not in BCM, skipping")
    
    return removed


class NvidiaAirClient:
    """Simple client for NVIDIA Air API."""
    
    def __init__(self, api_url: str, api_token: str, simulation_id: str):
        self.api_url = api_url.rstrip('/')
        self.api_token = api_token
        self.simulation_id = simulation_id
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
    
    def get_simulation(self):
        """Get simulation details."""
        url = f"{self.api_url}/simulations/{self.simulation_id}/"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    def get_simulation_nodes(self):
        """Get all nodes in the simulation."""
        url = f"{self.api_url}/simulations/{self.simulation_id}/nodes/"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    def rebuild_node(self, node_id: str):
        """Rebuild a node to factory defaults."""
        url = f"{self.api_url}/simulations/{self.simulation_id}/nodes/{node_id}/rebuild/"
        response = requests.post(url, headers=self.headers)
        response.raise_for_status()
        return response.json() if response.text else {"status": "rebuilding"}
    
    def get_node_status(self, node_id: str):
        """Get current status of a node."""
        url = f"{self.api_url}/simulations/{self.simulation_id}/nodes/{node_id}/"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()


def rebuild_switches_in_air(client: NvidiaAirClient, switches: list):
    """Rebuild switches in NVIDIA Air."""
    print("\nGetting simulation nodes...")
    
    try:
        nodes = client.get_simulation_nodes()
    except requests.exceptions.HTTPError as e:
        print(f"Error getting nodes: {e}")
        return False
    
    # Find node IDs for our switches
    switch_nodes = {}
    for node in nodes:
        name = node.get('name', '')
        if name in switches:
            switch_nodes[name] = node
    
    if not switch_nodes:
        print("No matching switches found in simulation")
        return False
    
    print(f"Found {len(switch_nodes)} switches to rebuild")
    
    # Rebuild each switch
    rebuilding = []
    for name, node in switch_nodes.items():
        node_id = node.get('id')
        print(f"  Rebuilding {name}...")
        try:
            client.rebuild_node(node_id)
            rebuilding.append((name, node_id))
            print(f"    ✓ Rebuild initiated for {name}")
        except requests.exceptions.HTTPError as e:
            print(f"    ✗ Failed to rebuild {name}: {e}")
    
    if not rebuilding:
        return False
    
    # Monitor rebuild status
    print("\nMonitoring rebuild status...")
    max_wait = 300  # 5 minutes
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        all_ready = True
        
        for name, node_id in rebuilding:
            try:
                status = client.get_node_status(node_id)
                state = status.get('state', 'unknown')
                
                # Common states: booting, running, stopped, rebuilding
                if state in ('running', 'booted'):
                    print(f"  ✓ {name}: {state}")
                elif state in ('rebuilding', 'booting', 'starting'):
                    print(f"  ⏳ {name}: {state}")
                    all_ready = False
                else:
                    print(f"  ? {name}: {state}")
                    all_ready = False
            except Exception as e:
                print(f"  ✗ {name}: error checking status - {e}")
                all_ready = False
        
        if all_ready:
            print("\n✓ All switches are ready!")
            return True
        
        elapsed = int(time.time() - start_time)
        remaining = max_wait - elapsed
        print(f"\n  Waiting... ({elapsed}s elapsed, {remaining}s remaining)")
        time.sleep(10)
    
    print("\n⚠ Timeout waiting for switches to be ready")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Reset test simulation for automated testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script prepares a clean environment for testing by:
1. Removing switches from BCM (if present)
2. Rebuilding switches in NVIDIA Air to factory defaults

Prerequisites:
- .configs/.env file with NVIDIA Air credentials
- NVIDIA Air simulation with test topology running
- Running on a BCM head node (for BCM cleanup)

Examples:
  %(prog)s                  # Full reset (BCM cleanup + Air rebuild)
  %(prog)s --skip-bcm       # Only rebuild in Air (skip BCM cleanup)
  %(prog)s --skip-air       # Only BCM cleanup (skip Air rebuild)
        """
    )
    
    parser.add_argument("--skip-bcm", action="store_true",
                       help="Skip BCM device cleanup")
    parser.add_argument("--skip-air", action="store_true",
                       help="Skip NVIDIA Air rebuild")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Test Simulation Reset")
    print("=" * 60)
    
    # Check/create .env file
    env = load_env()
    if env is None:
        print(f"\n.env file not found at {ENV_FILE}")
        create_env_file()
        print(f"\nPlease edit {ENV_FILE} with your credentials:")
        print("  - AIR_API_TOKEN: Your NVIDIA Air API token")
        print("  - AIR_API_URL: API URL (air.nvidia.com or air-inside.nvidia.com)")
        print("  - SIMULATION_ID: Your simulation UUID")
        print("\nThen run this script again.")
        sys.exit(1)
    
    # Validate .env
    required_vars = ['AIR_API_TOKEN', 'AIR_API_URL', 'SIMULATION_ID']
    missing = [v for v in required_vars if not env.get(v) or env.get(v).endswith('_here')]
    
    if missing:
        print(f"\nMissing or placeholder values in {ENV_FILE}:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease update these values and run again.")
        sys.exit(1)
    
    # Get switches from topology
    switches = get_switches_from_topology()
    print(f"\nSwitches to manage: {', '.join(switches)}")
    
    # Step 1: BCM cleanup
    if not args.skip_bcm:
        print("\n" + "-" * 60)
        print("Step 1: BCM Device Cleanup")
        print("-" * 60)
        
        bcm_devices = get_bcm_devices()
        switches_in_bcm = [s for s in switches if s in bcm_devices]
        
        if switches_in_bcm:
            print(f"Found {len(switches_in_bcm)} switches in BCM")
            removed = remove_switches_from_bcm(switches_in_bcm)
            print(f"\nRemoved {len(removed)} devices from BCM")
        else:
            print("No managed switches found in BCM, skipping cleanup")
    else:
        print("\n[Skipping BCM cleanup]")
    
    # Step 2: NVIDIA Air rebuild
    if not args.skip_air:
        print("\n" + "-" * 60)
        print("Step 2: NVIDIA Air Switch Rebuild")
        print("-" * 60)
        
        client = NvidiaAirClient(
            api_url=env['AIR_API_URL'],
            api_token=env['AIR_API_TOKEN'],
            simulation_id=env['SIMULATION_ID']
        )
        
        # Test connection
        print("Connecting to NVIDIA Air...")
        try:
            sim = client.get_simulation()
            print(f"  ✓ Connected to simulation: {sim.get('title', 'Unknown')}")
        except requests.exceptions.HTTPError as e:
            print(f"  ✗ Failed to connect: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"    Response: {e.response.text[:200]}")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ Connection error: {e}")
            sys.exit(1)
        
        # Rebuild switches
        if rebuild_switches_in_air(client, switches):
            print("\n✓ NVIDIA Air rebuild complete")
        else:
            print("\n⚠ NVIDIA Air rebuild may not have completed successfully")
            sys.exit(1)
    else:
        print("\n[Skipping NVIDIA Air rebuild]")
    
    print("\n" + "=" * 60)
    print("Reset Complete!")
    print("=" * 60)
    print("\nThe test environment is ready. You can now run:")
    print("  1. ./scripts/csv-from-dhcp.py")
    print("  2. ./scripts/change-default-password.py --csv .configs/from-dhcp.csv")
    print("  3. ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv")


if __name__ == "__main__":
    main()
