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
    python3 test-sim-reset.py --list         # List available simulations
    
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

# BCM version compatibility (cmsh path)
sys.path.insert(0, str((Path(__file__).resolve().parent.parent)))  # scripts/
from bcm_compat import get_cmsh_cmd

"""
Note on dependencies:
- This script supports `--help` even if optional deps are not installed.
- Runtime API calls require: `requests` and `python-dotenv` (see scripts/tests/requirements.txt).
"""

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
    
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        print("Error: Missing dependency 'python-dotenv'. Run: pip install -r requirements.txt")
        sys.exit(1)
    
    load_dotenv(ENV_FILE)
    return {
        'AIR_API_TOKEN': os.getenv('AIR_API_TOKEN'),
        'AIR_API_URL': os.getenv('AIR_API_URL', 'https://air.nvidia.com'),
        'AIR_USERNAME': os.getenv('AIR_USERNAME'),
        'SIMULATION_NAME': os.getenv('SIMULATION_NAME'),
        'SIMULATION_ID': os.getenv('SIMULATION_ID'),
    }


def _effective_simulation_from_args_env(args, env: dict) -> tuple:
    """Return (sim_name, sim_id) with CLI args taking precedence over env."""
    sim_id = args.sim_id or env.get('SIMULATION_ID')
    sim_name = args.sim_name or env.get('SIMULATION_NAME')
    return sim_name, sim_id


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
    cmsh = get_cmsh_cmd()
    try:
        result = subprocess.run(
            [cmsh, "-c", "device; list"],
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
        print(f"Error: cmsh not found at '{cmsh}'. This script must run on a BCM system.")
        return []


def remove_switches_from_bcm(switches_to_remove):
    """Remove specified switches from BCM.
    
    Devices must be closed before they can be removed.
    Command: cmsh -c "device; use <name>; close; remove; commit"
    """
    bcm_devices = get_bcm_devices()
    
    removed = []
    cmsh = get_cmsh_cmd()
    for switch in switches_to_remove:
        if switch in bcm_devices:
            print(f"  Removing {switch} from BCM...")
            try:
                # Must close the device before removing it
                result = subprocess.run(
                    [cmsh, "-c", f"device; use {switch}; close; remove; commit"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    removed.append(switch)
                    print(f"    ✓ Removed {switch}")
                else:
                    # Check if it's already removed or another error
                    if "Unable to remove" in result.stderr or "Unable to remove" in result.stdout:
                        print(f"    ✗ Failed to remove {switch}: Device is still in use")
                        print(f"       Try: cmsh -c 'device; use {switch}; usedby'")
                    else:
                        print(f"    ✗ Failed to remove {switch}: {result.stderr or result.stdout}")
            except Exception as e:
                print(f"    ✗ Error removing {switch}: {e}")
        else:
            print(f"  {switch} not in BCM, skipping")
    
    return removed


class AirClient:
    """Simple client for NVIDIA Air API using login-based authentication."""
    
    def __init__(self, base_url: str, username: str, api_token: str):
        try:
            import requests  # type: ignore
        except ImportError:
            print("Error: Missing dependency 'requests'. Run: pip install -r requirements.txt")
            sys.exit(1)
        
        self._requests = requests
        self.base_url = base_url.rstrip('/')
        # Remove any /api/vX suffix to get clean base URL
        if '/api/' in self.base_url:
            self.base_url = self.base_url.split('/api/')[0]
        
        self.username = username
        self.api_token = api_token
        self.jwt_token = None
        self.session = self._requests.Session()
    
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
    
    def list_simulations(self):
        """List all available simulations."""
        data = self.get_simulations()
        simulations = data.get('results', [])
        if not simulations:
            print("  No simulations found.")
            return []
        
        print(f"\n  Available simulations ({len(simulations)}):")
        print("  " + "-" * 50)
        for sim in simulations:
            title = sim.get('title', 'Untitled')
            sim_id = sim.get('id', 'N/A')
            state = sim.get('state', 'unknown')
            print(f"    {title}")
            print(f"      ID: {sim_id}")
            print(f"      State: {state}")
            print()
        return simulations
    
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
        """Get nodes in a simulation.
        
        Uses /api/v2/simulations/nodes/?simulation=<id> endpoint.
        NOT /api/v2/simulations/<id>/nodes/ (which doesn't exist).
        """
        # Correct endpoint: filter nodes by simulation ID
        url = f"{self.base_url}/api/v2/simulations/nodes/"
        params = {'simulation': sim_id}
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()
    
    def rebuild_node(self, node_id: str):
        """Rebuild a node using the control endpoint.
        
        Uses /api/v2/simulations/nodes/<id>/control/ with {"action": "rebuild"}
        """
        url = f"{self.base_url}/api/v2/simulations/nodes/{node_id}/control/"
        payload = {"action": "rebuild"}
        response = self.session.post(url, json=payload)
        if response.status_code not in (200, 201, 202, 204):
            raise Exception(f"Rebuild failed: {response.status_code} - {response.text[:200]}")
        return True
    
    def get_node(self, node_id: str):
        """Get node details."""
        url = f"{self.base_url}/api/v2/simulations/nodes/{node_id}/"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()


def rebuild_switches_in_air(client: AirClient, sim_id: str, switches: list):
    """Rebuild switches in NVIDIA Air."""
    print("\nGetting simulation nodes...")
    
    try:
        nodes_data = client.get_simulation_nodes(sim_id)
        # Handle paginated or non-paginated response
        if isinstance(nodes_data, dict):
            nodes = nodes_data.get('results', [])
        else:
            nodes = nodes_data
        print(f"  Found {len(nodes)} nodes in simulation")
    except Exception as e:
        print(f"  ✗ Failed to get nodes: {e}")
        return False
    
    # Find switch nodes
    switch_nodes = []
    all_node_names = []
    for node in nodes:
        name = node.get('name', '')
        all_node_names.append(name)
        if name in switches:
            switch_nodes.append(node)
    
    if not switch_nodes:
        print("  No matching switches found in simulation")
        print(f"  Looking for: {switches}")
        print(f"  Found nodes: {all_node_names}")
        return False
    
    print(f"\nRebuilding {len(switch_nodes)} switches...")
    
    # Rebuild each switch
    rebuilding = []
    for node in switch_nodes:
        name = node.get('name')
        node_id = node.get('id')
        print(f"  Rebuilding {name}...", end=" ", flush=True)
        try:
            client.rebuild_node(node_id)
            rebuilding.append((name, node_id))
            print("✓ initiated")
        except Exception as e:
            print(f"✗ Failed: {e}")
    
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
                node = client.get_node(node_id)
                state = node.get('state', 'unknown')
                
                if state.lower() in ('running', 'booted', 'active', 'ready'):
                    status_line.append(f"✓{name}")
                else:
                    status_line.append(f"⏳{name}:{state}")
                    all_ready = False
            except Exception as e:
                status_line.append(f"?{name}")
                all_ready = False
        
        elapsed = int(time.time() - start_time)
        print(f"\r  [{elapsed}s] {' | '.join(status_line)}                    ", end="", flush=True)
        
        if all_ready:
            print("\n\n✓ All switches are ready!")
            return True
        
        time.sleep(15)
    
    print("\n\n⚠ Timeout waiting for switches to be ready")
    return False


def air_health_check(client: AirClient, sim_id: str, switches: list) -> bool:
    """
    Fast validation that NVIDIA Air API is usable for this simulation.

    This is intended to catch cases where the simulation may appear "not loaded" in the UI
    (or the API is otherwise unhealthy) even if the underlying simulation is actually running.

    Checks (no side effects):
    - Can fetch the simulation object
    - Can list nodes for the simulation
    - Expected switch node names are present in the returned node list
    """
    print("\nNVIDIA Air health-check (no side effects)")
    try:
        sim = client.get_simulation(sim_id)
        title = sim.get("title") or sim.get("name") or sim_id
        state = sim.get("state", "unknown")
        print(f"  ✓ Simulation reachable: {title} (state={state})")
    except Exception as e:
        print(f"  ✗ Failed to fetch simulation {sim_id}: {e}")
        return False

    try:
        nodes_data = client.get_simulation_nodes(sim_id)
        if isinstance(nodes_data, dict):
            nodes = nodes_data.get("results", [])
        else:
            nodes = nodes_data
        print(f"  ✓ Nodes endpoint OK: {len(nodes)} nodes returned")
    except Exception as e:
        print(f"  ✗ Failed to list simulation nodes: {e}")
        return False

    names = {n.get("name", "") for n in nodes if isinstance(n, dict)}
    missing = [sw for sw in switches if sw not in names]
    if missing:
        print("  ✗ Expected switches not found in simulation nodes list:")
        for sw in missing:
            print(f"    - {sw}")
        sample = sorted([n for n in names if n])[:25]
        if sample:
            print(f"  Sample node names: {', '.join(sample)}")
        return False

    print("  ✓ Expected switch nodes are present")
    return True


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
  %(prog)s --list           # List available NVIDIA Air simulations
  %(prog)s --sim-name NAME  # Use simulation name (overrides .env)
  %(prog)s --sim-id ID      # Use simulation ID (overrides .env)
        """
    )
    
    parser.add_argument("--skip-bcm", action="store_true",
                       help="Skip BCM device cleanup")
    parser.add_argument("--skip-air", action="store_true",
                       help="Skip NVIDIA Air rebuild")
    parser.add_argument("--list", action="store_true",
                       help="List available NVIDIA Air simulations and exit")
    parser.add_argument("--health-check", action="store_true",
                       help="Validate NVIDIA Air API + simulation access (no side effects) and exit")
    parser.add_argument("--debug", action="store_true",
                       help="Show debug information")
    sim_group = parser.add_mutually_exclusive_group()
    sim_group.add_argument("--sim-name", type=str, default=None,
                           help="Simulation name/title to use (overrides SIMULATION_NAME in .env)")
    sim_group.add_argument("--sim-id", type=str, default=None,
                           help="Simulation ID to use (overrides SIMULATION_ID in .env)")
    
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
    
    # Validate required auth fields (but allow missing simulation for --list)
    auth_missing = []
    if not env.get('AIR_API_TOKEN') or env['AIR_API_TOKEN'].endswith('_here'):
        auth_missing.append('AIR_API_TOKEN')
    if not env.get('AIR_USERNAME') or env['AIR_USERNAME'].endswith('example.com'):
        auth_missing.append('AIR_USERNAME')
    
    if auth_missing:
        print(f"\nMissing or placeholder values in {ENV_FILE}:")
        for var in auth_missing:
            print(f"  - {var}")
        print("\nPlease update these values and run again.")
        sys.exit(1)
    
    # Handle --list option (can work without simulation name/ID)
    if args.list:
        api_url = env.get('AIR_API_URL', 'https://air.nvidia.com')
        print(f"\nConnecting to {api_url}...")
        client = AirClient(api_url, env['AIR_USERNAME'], env['AIR_API_TOKEN'])
        
        try:
            client.login()
            print("  ✓ Logged in successfully")
            client.list_simulations()
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            sys.exit(1)
        sys.exit(0)
    
    # For non-list operations, require simulation name/ID (CLI args override .env)
    # Health-check also requires a simulation selection.
    # For non-list operations, require simulation name/ID (CLI args override .env)
    eff_sim_name, eff_sim_id = _effective_simulation_from_args_env(args, env)
    if not eff_sim_name and not eff_sim_id:
        print(f"\nMissing SIMULATION_NAME or SIMULATION_ID in {ENV_FILE}")
        print("\nTo see available simulations, run:")
        print(f"  {sys.argv[0]} --list")
        
        # Try to list simulations if auth is available
        api_url = env.get('AIR_API_URL', 'https://air.nvidia.com')
        print(f"\nAttempting to list your simulations...")
        client = AirClient(api_url, env['AIR_USERNAME'], env['AIR_API_TOKEN'])
        
        try:
            client.login()
            print("  ✓ Connected to NVIDIA Air")
            client.list_simulations()
            print("\nUpdate your .env file with one of the simulation names above,")
            print("then run this script again.")
        except Exception as e:
            print(f"  Could not list simulations: {e}")
        
        sys.exit(1)
    
    # Get switches from topology
    switches = get_switches_from_topology()
    print(f"\nSwitches to manage: {', '.join(switches)}")

    # Handle --health-check (no BCM cleanup, no rebuild; just validate API + simulation access)
    if args.health_check:
        api_url = env.get('AIR_API_URL', 'https://air.nvidia.com')
        print(f"\nConnecting to {api_url}...")
        client = AirClient(api_url, env['AIR_USERNAME'], env['AIR_API_TOKEN'])
        try:
            client.login()
            print("  ✓ Logged in successfully")
        except Exception as e:
            print(f"  ✗ Login failed: {e}")
            sys.exit(1)

        # Resolve simulation ID if only a name is provided
        sim_name, sim_id = _effective_simulation_from_args_env(args, env)
        if sim_name and not sim_id:
            sim = client.find_simulation_by_name(sim_name)
            if sim:
                sim_id = sim.get("id")
        if not sim_id:
            print("  ✗ Could not resolve simulation ID (check SIMULATION_ID or SIMULATION_NAME)")
            sys.exit(1)

        ok = air_health_check(client, sim_id, switches)
        sys.exit(0 if ok else 1)
    
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
        
        # Find simulation (CLI args override .env)
        sim_name, sim_id = _effective_simulation_from_args_env(args, env)
        
        if sim_name and not sim_id:
            print(f"\nFinding simulation by name: {sim_name}")
            sim = client.find_simulation_by_name(sim_name)
            if sim:
                sim_id = sim.get('id')
                print(f"  ✓ Found: {sim.get('title')} (ID: {sim_id})")
            else:
                # List available simulations
                print(f"  ✗ Simulation '{sim_name}' not found")
                client.list_simulations()
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
    print("\nThe test environment is ready. Next steps (informational):")
    print("  1. Wait for switches to get DHCP addresses (~1-2 min)")
    print("  2. Run: ./scripts/csv-from-dhcp.py")
    print("  3. Run: ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password <old> --new-password <new>")
    print("  4. Run: ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv --non-interactive --username <user> --password <pwd>")


if __name__ == "__main__":
    main()
