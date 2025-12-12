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

### `change-default-password.py`
Change default passwords on Cumulus switches (default: cumulus/cumulus).

```bash
./scripts/change-default-password.py --csv .configs/from-dhcp.csv
```

**Options:**
- `--csv FILE` - Required. CSV file with switch info
- `--dry-run` - Show what would be done
- `--verify-only` - Check which switches need password change

**Requires:** `expect` or `sshpass` installed on the system.

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

---

## Test Scripts (Requires Dependencies)

Scripts in `tests/` directory interact with the NVIDIA Air API and require additional Python packages.

See [tests/README.md](tests/README.md) for setup instructions.
