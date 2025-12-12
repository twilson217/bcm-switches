# Test Scripts

This directory contains scripts for automated testing with NVIDIA Air simulations.

## Prerequisites

These scripts require additional Python packages. We recommend using `uv` for fast dependency management.

### Option 1: Using uv (Recommended)

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
cd scripts/tests
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### Option 2: Using pip

```bash
cd scripts/tests
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

1. Copy the sample environment file:
   ```bash
   cp sample-configs/sample.env .env
   ```

2. Edit `.env` with your credentials:
   ```bash
   # NVIDIA Air API Token (from Account Settings -> API Tokens)
   AIR_API_TOKEN=your_token_here
   
   # API URL (external or internal)
   AIR_API_URL=https://air.nvidia.com
   # or for NVIDIA employees: https://air-inside.nvidia.com
   
   # Your simulation UUID
   SIMULATION_ID=your_simulation_id
   
   # Your username (email)
   AIR_USERNAME=your_email@example.com
   ```

   **Important:** Internal (air-inside.nvidia.com) and external (air.nvidia.com) 
   use **different API tokens**. Generate a token from the correct site!

## Scripts

### `test-loop.py`

**Fully automated test loop** that runs complete deployment cycles:
- Reset simulation
- Setup switches (DHCP, passwords, hostnames)
- Deploy to BCM
- Validate deployment

```bash
# Activate virtual environment first
source .venv/bin/activate

# Run all tests (Test 1 and Test 2)
./test-loop.py

# Run only Test 1 (DHCP lease deployment)
./test-loop.py --test1

# Run only Test 2 (switch setup first, then deploy)
./test-loop.py --test2

# Skip simulation reset (use existing state)
./test-loop.py --test1 --no-reset

# Dry run - show what would be done
./test-loop.py --dry-run

# Verbose output
./test-loop.py --verbose
```

**Test 1: Full Deployment from DHCP Leases**
1. Reset simulation
2. Generate CSV from DHCP leases
3. Change default passwords
4. Map hostnames from topology
5. Deploy using `--csv`
6. Validate deployment

**Test 2: Deployment with Switch Setup First**
1. Reset simulation
2. Generate CSV from DHCP leases
3. Map hostnames from topology
4. Change passwords AND set hostnames on switches
5. Deploy using `--csv`
6. Validate deployment

### `test-sim-reset.py`

Resets the test environment by:
1. Removing switches from BCM (if present)
2. Rebuilding switches in NVIDIA Air to factory defaults

```bash
# Activate virtual environment first
source .venv/bin/activate

# Full reset
./test-sim-reset.py

# Skip BCM cleanup (only rebuild in Air)
./test-sim-reset.py --skip-bcm

# Skip Air rebuild (only BCM cleanup)
./test-sim-reset.py --skip-air

# List available simulations
./test-sim-reset.py --list

# Debug mode
./test-sim-reset.py --debug
```

## Sample Files

- `sample-configs/sample.env` - Template for `.env` file
- `sample-configs/test-topology.json` - NVIDIA Air topology used for testing

## Testing Log

See [testing-log.md](testing-log.md) for a record of tests performed.
