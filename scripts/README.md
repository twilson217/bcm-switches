# Scripts

This directory contains utility scripts for BCM switch deployment.

## Core Scripts (No External Dependencies)

These scripts use only Python standard library and can be run without installing additional packages.

### `csv-from-dhcp.py`
Generate a CSV file from DHCP leases for switch discovery.

```bash
./scripts/csv-from-dhcp.py
# Output: .configs/from-dhcp.csv
```

**Options:**
- `--output FILE` - Custom output path
- `--filter VENDOR` - Filter by vendor class

### `change-switch-defaults.py`
Change default password, hostname, and/or ZTP settings on Cumulus switches.

```bash
# Do ALL actions (default behavior for fresh switches)
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv

# Change password only
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password

# Change hostname only (when password already changed)
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --hostname --current-password <pwd>

# Disable ZTP only
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --disable-ztp --current-password <pwd>

# Combine specific actions
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password --disable-ztp
```

**Default behavior:** If no action flags are specified, ALL actions are performed:
- Change password (from default cumulus/cumulus)
- Set hostname (from CSV file)
- Disable ZTP on the switch

**Options:**
- `--csv FILE` - Required. CSV file with switch info
- `--password` - Change the default password
- `--hostname` - Set hostname from CSV file
- `--disable-ztp` - Disable ZTP on the switches (runs `sudo ztp --disable`)
- `--current-password PWD` - Current password if not default
- `--dry-run` - Show what would be done

**Requires:** `expect` and `sshpass` installed on the system.

### `map-csv-topology.py`
Map hostnames from an NVIDIA Air topology JSON to switches in a CSV file.

```bash
./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology scripts/tests/sample-configs/test-topology.json
```

**Options:**
- `--csv FILE` - Required. CSV file to update
- `--topology FILE` - Required. NVIDIA Air topology JSON

### `prep-airgapped.py`
Prepare files for airgapped deployment.

```bash
./scripts/prep-airgapped.py
```

Downloads pip packages into `.files/` directory, then creates a tarball for transfer to airgapped systems.

**Important:** In production airgapped environments, `deploy_bcm_switches.py` should use the **production BCM’s**
`cm-lite-daemon.zip` at deploy time (do not bundle a zip from a different system).

**Options:**
- `--output FILE` - Custom output tarball path
- `--python3-version X.Y` - **Required for best results**. Target switch Python version (e.g. `3.11`). Used to download compatible wheels for offline installation.
- `--requirements/-r FILE` - Path to a `requirements.txt` file (copied from inside `cm-lite-daemon.zip`)
- `--cm-lite-zip FILE` - Optional. Used only to extract `requirements.txt` if you did not provide `--requirements`

**Python version note:** You should set `--python3-version` to match the switches you will deploy to. On a switch, you can check with:

```bash
python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
```

**Where to run it:** `prep-airgapped.py` does **not** require BCM-specific tools (like `cmsh`). It can run on a laptop/WSL **as long as** you have internet access for `pip download`.

### `air-sim-setup.py`
Prepare an NVIDIA Air lab simulation for deployment/validation.

This script is a convenience wrapper that does common “setup” steps:
- Preflight `oob-mgmt-switch`: if its `swp0` took a DHCP lease (routed mode issue), configure bridging via NVUE for `swp0-50`
- Run `csv-from-dhcp.py` to generate/update `.configs/from-dhcp.csv`
- Run `map-csv-topology.py` to set hostnames in the CSV based on the topology JSON
- Run `change-switch-defaults.py` to set the switch password, hostnames, and disable ZTP

```bash
# Basic usage (uses test topology + default leases path)
./scripts/air-sim-setup.py --password 'Nvidia1234!'

# Override topology / output CSV
./scripts/air-sim-setup.py \
  --topology scripts/tests/sample-configs/test-topology.json \
  --csv .configs/from-dhcp.csv \
  --password 'Nvidia1234!'

# Skip oob-mgmt-switch preflight if you know it’s already correct
./scripts/air-sim-setup.py --skip-oob --password 'Nvidia1234!'
```

**Requires:** `sshpass` (and `expect` if your environment forces password change on first login).

### `validation-testing.py`
Comprehensive validation of BCM switch deployment. Checks both BCM-side and switch-side configuration.

```bash
# Full validation of all switches in BCM
./scripts/validation-testing.py

# Validate switches from a CSV file
./scripts/validation-testing.py --csv .configs/from-dhcp.csv

# Validate a single switch
./scripts/validation-testing.py --switch 192.168.200.166

# BCM-side checks only (no SSH to switches)
./scripts/validation-testing.py --bcm-only

# Verbose output with details
./scripts/validation-testing.py --verbose

# JSON output for automation
./scripts/validation-testing.py --json
```

**Checks Performed:**

*BCM System:*
- cmdaemon running and listening on ports 8080/8081
- DHCP server active
- Recent syslog errors

*Per-Switch BCM Checks:*
- Device exists in BCM
- Device status (UP vs INSTALLER_UNREACHABLE)
- Config mode set to MANUAL (BCM 10: `cumulusmode`, BCM 11: `nvconfigurationmode`)
- ZTP "run on each boot" disabled
- `hasclientdaemon` set
- Network assignment

*Per-Switch SSH Checks:*
- SSH connectivity
- Hostname matches expected
- `cm-lite-daemon` installed, running, enabled
- ZTP disabled on switch
- Required pip packages installed
- cm-lite-daemon config valid
- Switch can reach BCM head node
- Daemon logs for errors

**Options:**
- `--csv FILE` - CSV file with switches to validate
- `--switch IP` - Single switch IP to validate
- `--username USER` - SSH username (default: cumulus)
- `--password PWD` - SSH password (uses config.json if not provided)
- `--bcm-only` - Only run BCM-side checks
- `--switch-only` - Only run switch-side checks
- `--json` - Output as JSON
- `--verbose` - Show detailed output
- `--quiet` - Only show summary

**Exit Codes:**
- `0` - All checks passed
- `1` - One or more checks failed

---

## Test Scripts (Requires Dependencies)

Scripts in `tests/` directory interact with the NVIDIA Air API and require additional Python packages.

See [tests/README.md](tests/README.md) for setup instructions.

---

## ZTP Staging / Preflight (brownfield DR/RMA)

These scripts prepare BCM-side artifacts for future ZTP use **without enabling ZTP automatically**.

### `ztp-staging.py`
Stages ZTP configuration artifacts from each switch’s current running config by copying:
- `/etc/nvue.d/startup.yaml` (from the switch)
to:
- `/cm/local/apps/cmd/etc/htdocs/switch/<switch>/startup.yaml` (on BCM)

It also sets in BCM:
- Config mode = file (BCM 10: `cumulusmode`, BCM 11: `nvconfigurationmode`)
- Config file = startup.yaml (BCM 10: `cumulusfile`, BCM 11: `nvconfigurationfile`)
and runs:
- `cmsh -c "device; use <switch>; initialize"`

**Config-only staging (default):**

```bash
./scripts/ztp-staging.py --from-bcm
```

**Optional image staging:** By default BCM serves images under the HTTP path `.../switch/image/<filename>` (note: `image` is singular on BCM 10.x/11.x we’ve checked). If you enable image staging, `ztp-staging.py` copies the selected `cumulus-*.bin` into:
- `/cm/local/apps/cmd/etc/htdocs/switch/image/`
and sets `ztpsettings image` to the filename (while keeping `checkimageonboot=no`).

### `ztp-preflight.py`
Prints a BCM-sourced ZTP readiness **checklist** before you manually enable ZTP.

It prints every check it performs, plus explicit **manual TODO steps** (like DHCP option config and switch-side ZTP enable/disable confirmation).

By default it runs both config and image checks:

```bash
./scripts/ztp-preflight.py
```

**Only config checks:**

```bash
./scripts/ztp-preflight.py --config-only
```

**Only image checks:**

```bash
./scripts/ztp-preflight.py --image-only
```
