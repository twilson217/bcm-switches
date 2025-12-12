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

Downloads `cm-lite-daemon.zip` and pip packages into `.files/` directory, then creates a tarball for transfer to airgapped systems.

**Options:**
- `--output FILE` - Custom output tarball path
- `--skip-packages` - Skip downloading pip packages

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
- `cumulusmode` set to MANUAL (monitoring-only)
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
