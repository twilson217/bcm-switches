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
    
Requirements:
    pip install air-sdk
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Check for air-sdk
try:
    from air_sdk import AirApi
    from air_sdk.exceptions import AirAuthorizationError, AirForbiddenError
except ImportError:
    print("Error: 'air-sdk' not found. Install with: pip install air-sdk")
    sys.exit(1)

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent.parent  # scripts/tests -> scripts -> repo root
ENV_FILE = SCRIPT_DIR / ".env"
SAMPLE_ENV = SCRIPT_DIR / "sample-configs" / "sample.env"
TOPOLOGY_FILE = SCRIPT_DIR / "sample-configs" / "test-topology.json"

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
    if SAMPLE_ENV.exists():
        import shutil
        shutil.copy(SAMPLE_ENV, ENV_FILE)
        print(f"Created {ENV_FILE} from sample")
    else:
        content = """# NVIDIA Air API Configuration
# Fill in your credentials below

# NVIDIA Air API Token (get from air.nvidia.com -> Account Settings -> API Tokens)
# Note: Internal and external sites use DIFFERENT tokens!
AIR_API_TOKEN=your_api_token_here

# NVIDIA Air API URL (just the base URL, script adds /api/v1)
# External: https://air.nvidia.com
# Internal (NVIDIA VPN): https://air-inside.nvidia.com
AIR_API_URL=https://air.nvidia.com

# Your simulation ID (from the simulation URL)
SIMULATION_ID=your_simulation_id_here
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


def normalize_api_url(url):
    """Normalize API URL to the format expected by air-sdk."""
    url = url.rstrip('/')
    # Remove any existing /api/v1 or /api/v2 suffix
    if '/api/v' in url:
        url = url.split('/api/v')[0]
    elif url.endswith('/api'):
        url = url[:-4]
    # Add /api/ suffix
    return url + '/api/'


def decode_token_if_needed(token):
    """Decode base64 token if it appears to be encoded."""
    # Check if token looks like base64-encoded UUID
    try:
        decoded = base64.b64decode(token).decode('utf-8')
        # If it decodes to a UUID-like string, return decoded
        if len(decoded) == 36 and decoded.count('-') == 4:
            return decoded
    except:
        pass
    return token


def rebuild_switches_in_air(api: AirApi, simulation_id: str, switches: list):
    """Rebuild switches in NVIDIA Air."""
    print("\nGetting simulation nodes...")
    
    try:
        sim = api.simulations.get(simulation_id)
        print(f"  Simulation: {sim.title}")
        print(f"  State: {sim.state}")
    except Exception as e:
        print(f"  ✗ Failed to get simulation: {e}")
        return False
    
    # Get nodes
    try:
        nodes = list(sim.nodes.list())
        print(f"  Found {len(nodes)} nodes")
    except Exception as e:
        print(f"  ✗ Failed to get nodes: {e}")
        return False
    
    # Find switch nodes
    switch_nodes = []
    for node in nodes:
        if node.name in switches:
            switch_nodes.append(node)
    
    if not switch_nodes:
        print("  No matching switches found in simulation")
        return False
    
    print(f"\nRebuilding {len(switch_nodes)} switches...")
    
    # Rebuild each switch
    rebuilding = []
    for node in switch_nodes:
        print(f"  Rebuilding {node.name}...")
        try:
            node.rebuild()
            rebuilding.append(node)
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
        
        for node in rebuilding:
            try:
                # Refresh node state
                node = api.simulations.get(simulation_id).nodes.get(node.id)
                state = node.state if hasattr(node, 'state') else 'unknown'
                
                if state in ('running', 'booted', 'RUNNING', 'BOOTED'):
                    status_line.append(f"✓{node.name}")
                else:
                    status_line.append(f"⏳{node.name}:{state}")
                    all_ready = False
            except Exception as e:
                status_line.append(f"?{node.name}")
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
- .configs/.env file with NVIDIA Air credentials
- air-sdk installed (pip install air-sdk)
- NVIDIA Air simulation running
- Running on a BCM head node (for BCM cleanup)

Token Notes:
- External (air.nvidia.com) and internal (air-inside.nvidia.com) 
  use DIFFERENT API tokens!
- Generate your token from Account Settings -> API Tokens
- If you see 403 errors, regenerate your token for the correct site

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
    env = load_env()
    if env is None:
        print(f"\n.env file not found at {ENV_FILE}")
        create_env_file()
        print(f"\nPlease edit {ENV_FILE} with your credentials:")
        print("  - AIR_API_TOKEN: Your NVIDIA Air API token")
        print("  - AIR_API_URL: API URL (air.nvidia.com or air-inside.nvidia.com)")
        print("  - SIMULATION_ID: Your simulation UUID")
        print("\nIMPORTANT: Use a token generated for the CORRECT site!")
        print("           Internal and external sites use different tokens.")
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
        
        # Normalize API URL
        api_url = normalize_api_url(env['AIR_API_URL'])
        token = decode_token_if_needed(env['AIR_API_TOKEN'])
        
        if args.debug:
            print(f"  API URL: {api_url}")
            print(f"  Token (first 20): {token[:20]}...")
            print(f"  Simulation: {env['SIMULATION_ID']}")
        
        print("Connecting to NVIDIA Air...")
        try:
            api = AirApi(api_url=api_url, api_version='v1', bearer_token=token)
            # Test connection by getting simulation
            sim = api.simulations.get(env['SIMULATION_ID'])
            print(f"  ✓ Connected to simulation: {sim.title}")
        except AirForbiddenError:
            print("  ✗ Authentication failed (403 Forbidden)")
            print("\n  Possible causes:")
            print("    1. Token is invalid or expired")
            print("    2. Token is for the WRONG site (internal vs external)")
            print("       - air.nvidia.com and air-inside.nvidia.com use DIFFERENT tokens")
            print("    3. Token doesn't have permission for this simulation")
            print("\n  Solution:")
            print(f"    1. Go to {env['AIR_API_URL']}")
            print("    2. Account Settings -> API Tokens")
            print("    3. Generate a NEW token")
            print(f"    4. Update AIR_API_TOKEN in {ENV_FILE}")
            sys.exit(1)
        except AirAuthorizationError as e:
            print(f"  ✗ Authorization error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ Connection error: {e}")
            if args.debug:
                import traceback
                traceback.print_exc()
            sys.exit(1)
        
        # Rebuild switches
        if rebuild_switches_in_air(api, env['SIMULATION_ID'], switches):
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
    print("  1. Wait for switches to get DHCP addresses")
    print("  2. Run: ./scripts/csv-from-dhcp.py")
    print("  3. Run: ./scripts/change-default-password.py --csv .configs/from-dhcp.csv")
    print("  4. Run: ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv")


if __name__ == "__main__":
    main()
