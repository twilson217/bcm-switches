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

# Debug mode
./test-sim-reset.py --debug
```

### `test_direct_auth.py`

Test NVIDIA Air API authentication.

```bash
source .venv/bin/activate
./test_direct_auth.py
```

## Sample Files

- `sample-configs/sample.env` - Template for `.env` file
- `sample-configs/test-topology.json` - NVIDIA Air topology used for testing

## Testing Log

See [testing-log.md](testing-log.md) for a record of tests performed.
