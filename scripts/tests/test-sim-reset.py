#!/usr/bin/env python3
"""
Test Simulation Reset Script

Resets the test environment by:
1. Removing switches from BCM (if present)
2. Rebuilding switches in NVIDIA Air to factory defaults

This enables automated test loops by providing a clean starting state.

Usage:
    python3 test-sim-reset.py
    python3 test-sim-reset.py --skip-bcm     # Skip BCM cleanup
    python3 test-sim-reset.py --skip-air     # Skip Air rebuild
    
Requirements:
    pip install -r requirements.txt
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
    from dotenv import load_dotenv
except ImportError:
    print("Error: Missing dependencies. Run: pip install -r requirements.txt")
    sys.exit(1)

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent.parent  # scripts/tests -> scripts -> repo root
ENV_FILE = SCRIPT_DIR / ".env"
SAMPLE_ENV = SCRIPT_DIR / "sample-configs" / "sample.env"
TOPOLOGY_FILE = SCRIPT_DIR / "sample-configs" / "test-topology.json"

# Switches we manage (Cumulus switches that get deployed to BCM)
MANAGED_SWITCHES = ["spine-01", "spine-02", "leaf-01", "leaf-02", "leaf-03", "leaf-04"]


def load_env_file():
    """Load environment variables from .env file."""
    if not ENV_FILE.exists():
        return None
    
    load_dotenv(ENV_FILE)
    return {
        'AIR_API_TOKEN': os.getenv('AIR_API_TOKEN'),
        'AIR_API_URL': os.getenv('AIR_API_URL', 'https://air.nvidia.com'),
        'AIR_USERNAME': os.getenv('AIR_USERNAME'),
        'SIMULATION_NAME': os.getenv('SIMULATION_NAME'),
        'SIMULATION_ID': os.getenv('SIMULATION_ID'),
    }


def create_env_file():
    """Create .env file from sample if it doesn't exist."""
    if SAMPLE_ENV.exists():
        import shutil
        shutil.copy(SAMPLE_ENV, ENV_FILE)
        print(f"Created {ENV_FILE} from sample")
    else:
        content = """# NVIDIA Air API Configuration

# Your username (email address) - REQUIRED
AIR_USERNAME=your_email@example.com

# NVIDIA Air API Token (from Account Settings -> API Tokens) - REQUIRED
AIR_API_TOKEN=your_api_token_here

# NVIDIA Air API URL
# External: https://air.nvidia.com
# Internal (NVIDIA VPN): https://air-inside.nvidia.com
AIR_API_URL=https://air.nvidia.com

# Simulation - specify EITHER name OR id
SIMULATION_NAME=your_simulation_name
# SIMULATION_ID=your_simulation_uuid
"""
        ENV_FILE.write_text(content)
        print(f"Created {ENV_FILE}")
    
    return False


def get_switches_from_topology():
    """Get switch names from topology file."""
    if not TOPOLOGY_FILE.exists():
        print(f"Warning: Topology file not found at {TOPOLOGY_FILE}")
        return MANAGED_SWITCHES
    
    try:
        with open(TOPOLOGY_FILE, 'r') as f:
            topology = json.load(f)
        
        nodes = topology.get('content', {}).get('nodes', {})
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
                    devices.append(parts[1])
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


class AirClient:
    """Simple client for NVIDIA Air API using login-based authentication."""
    
    def __init__(self, base_url: str, username: str, api_token: str):
        self.base_url = base_url.rstrip('/')
        # Remove any /api/vX suffix
        if '/api/' in self.base_url:
            self.base_url = self.base_url.split('/api/')[0]
        
        self.username = username
        self.api_token = api_token
        self.jwt_token = None
        self.session = requests.Session()
    
    def login(self):
        """Login to get JWT token."""
        url = f"{self.base_url}/api/v1/login/"
        response = self.session.post(url, data={
            'username': self.username,
            'password': self.api_token
        })
        
        if response.status_code != 200:
            raise Exception(f"Login failed: {response.status_code} - {response.text[:200]}")
        
        result = response.json()
        if 'token' not in result:
            raise Exception(f"No token in login response: {result}")
        
        self.jwt_token = result['token']
        self.session.headers.update({
            'Authorization': f'Bearer {self.jwt_token}',
            'Content-Type': 'application/json'
        })
        return True
    
    def get_simulations(self):
        """Get all simulations."""
        url = f"{self.base_url}/api/v2/simulations/"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()
    
    def find_simulation_by_name(self, name: str):
        """Find a simulation by name."""
        data = self.get_simulations()
        for sim in data.get('results', []):
            if sim.get('title') == name or sim.get('name') == name:
                return sim
        return None
    
    def get_simulation(self, sim_id: str):
        """Get simulation by ID."""
        url = f"{self.base_url}/api/v2/simulations/{sim_id}/"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()
    
    def get_simulation_nodes(self, sim_id: str):
        """Get nodes in a simulation."""
        url = f"{self.base_url}/api/v2/simulations/{sim_id}/nodes/"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()
    
    def rebuild_node(self, sim_id: str, node_id: str):
        """Rebuild a node."""
        url = f"{self.base_url}/api/v2/simulations/{sim_id}/nodes/{node_id}/rebuild/"
        response = self.session.post(url)
        if response.status_code not in (200, 201, 202, 204):
            raise Exception(f"Rebuild failed: {response.status_code} - {response.text[:200]}")
        return True
    
    def get_node(self, sim_id: str, node_id: str):
        """Get node details."""
        url = f"{self.base_url}/api/v2/simulations/{sim_id}/nodes/{node_id}/"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()


def rebuild_switches_in_air(client: AirClient, sim_id: str, switches: list):
    """Rebuild switches in NVIDIA Air."""
    print("\nGetting simulation nodes...")
    
    try:
        nodes_data = client.get_simulation_nodes(sim_id)
        nodes = nodes_data.get('results', nodes_data) if isinstance(nodes_data, dict) else nodes_data
        print(f"  Found {len(nodes)} nodes")
    except Exception as e:
        print(f"  ✗ Failed to get nodes: {e}")
        return False
    
    # Find switch nodes
    switch_nodes = []
    for node in nodes:
        name = node.get('name', '')
        if name in switches:
            switch_nodes.append(node)
    
    if not switch_nodes:
        print("  No matching switches found in simulation")
        print(f"  Looking for: {switches}")
        print(f"  Found nodes: {[n.get('name') for n in nodes]}")
        return False
    
    print(f"\nRebuilding {len(switch_nodes)} switches...")
    
    # Rebuild each switch
    rebuilding = []
    for node in switch_nodes:
        name = node.get('name')
        node_id = node.get('id')
        print(f"  Rebuilding {name}...")
        try:
            client.rebuild_node(sim_id, node_id)
            rebuilding.append((name, node_id))
            print(f"    ✓ Rebuild initiated")
        except Exception as e:
            print(f"    ✗ Failed: {e}")
    
    if not rebuilding:
        return False
    
    # Monitor rebuild status
    print("\nMonitoring rebuild status (this may take several minutes)...")
    max_wait = 300  # 5 minutes
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        all_ready = True
        status_line = []
        
        for name, node_id in rebuilding:
            try:
                node = client.get_node(sim_id, node_id)
                state = node.get('state', 'unknown')
                
                if state.lower() in ('running', 'booted', 'active'):
                    status_line.append(f"✓{name}")
                else:
                    status_line.append(f"⏳{name}:{state}")
                    all_ready = False
            except Exception as e:
                status_line.append(f"?{name}")
                all_ready = False
        
        print(f"  [{int(time.time() - start_time)}s] {' | '.join(status_line)}")
        
        if all_ready:
            print("\n✓ All switches are ready!")
            return True
        
        time.sleep(15)
    
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
- .env file with NVIDIA Air credentials (copy from sample-configs/sample.env)
- NVIDIA Air simulation running
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
    parser.add_argument("--debug", action="store_true",
                       help="Show debug information")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Test Simulation Reset")
    print("=" * 60)
    
    # Check/create .env file
    env = load_env_file()
    if env is None:
        print(f"\n.env file not found at {ENV_FILE}")
        create_env_file()
        print(f"\nPlease edit {ENV_FILE} with your credentials:")
        print("  - AIR_USERNAME: Your email address")
        print("  - AIR_API_TOKEN: Your NVIDIA Air API token")
        print("  - AIR_API_URL: API URL")
        print("  - SIMULATION_NAME: Your simulation name (or SIMULATION_ID)")
        print("\nThen run this script again.")
        sys.exit(1)
    
    # Validate required fields
    missing = []
    if not env.get('AIR_API_TOKEN') or env['AIR_API_TOKEN'].endswith('_here'):
        missing.append('AIR_API_TOKEN')
    if not env.get('AIR_USERNAME') or env['AIR_USERNAME'].endswith('example.com'):
        missing.append('AIR_USERNAME')
    if not env.get('SIMULATION_NAME') and not env.get('SIMULATION_ID'):
        missing.append('SIMULATION_NAME or SIMULATION_ID')
    
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
        
        api_url = env.get('AIR_API_URL', 'https://air.nvidia.com')
        
        if args.debug:
            print(f"  API URL: {api_url}")
            print(f"  Username: {env['AIR_USERNAME']}")
            print(f"  Token: {env['AIR_API_TOKEN'][:10]}...")
        
        print("Connecting to NVIDIA Air...")
        client = AirClient(api_url, env['AIR_USERNAME'], env['AIR_API_TOKEN'])
        
        try:
            client.login()
            print("  ✓ Logged in successfully")
        except Exception as e:
            print(f"  ✗ Login failed: {e}")
            sys.exit(1)
        
        # Find simulation
        sim_id = env.get('SIMULATION_ID')
        sim_name = env.get('SIMULATION_NAME')
        
        if sim_name and not sim_id:
            print(f"\nFinding simulation by name: {sim_name}")
            sim = client.find_simulation_by_name(sim_name)
            if sim:
                sim_id = sim.get('id')
                print(f"  ✓ Found: {sim.get('title')} (ID: {sim_id})")
            else:
                # List available simulations
                print(f"  ✗ Simulation '{sim_name}' not found")
                print("\n  Available simulations:")
                data = client.get_simulations()
                for s in data.get('results', []):
                    print(f"    - {s.get('title')}")
                sys.exit(1)
        
        if not sim_id:
            print("Error: No simulation ID or name configured")
            sys.exit(1)
        
        # Verify simulation
        try:
            sim = client.get_simulation(sim_id)
            print(f"  Simulation: {sim.get('title')}")
        except Exception as e:
            print(f"  ✗ Failed to get simulation: {e}")
            sys.exit(1)
        
        # Rebuild switches
        if rebuild_switches_in_air(client, sim_id, switches):
            print("\n✓ NVIDIA Air rebuild complete")
        else:
            print("\n⚠ NVIDIA Air rebuild may not have completed successfully")
            sys.exit(1)
    else:
        print("\n[Skipping NVIDIA Air rebuild]")
    
    print("\n" + "=" * 60)
    print("Reset Complete!")
    print("=" * 60)
    print("\nThe test environment is ready. Next steps:")
    print("  1. Wait for switches to get DHCP addresses (~1-2 min)")
    print("  2. Run: ./scripts/csv-from-dhcp.py")
    print("  3. Run: ./scripts/change-default-password.py --csv .configs/from-dhcp.csv")
    print("  4. Run: ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv")


if __name__ == "__main__":
    main()
