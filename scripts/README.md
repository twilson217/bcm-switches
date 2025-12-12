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

### `change-switch-defaults.py` (Recommended)
Change default password and/or hostname on Cumulus switches in a single SSH session.

```bash
# Change both password and hostname (most efficient)
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password --hostname

# Change password only
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password

# Change hostname only (when password already changed)
./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --hostname --current-password <pwd>
```

**Options:**
- `--csv FILE` - Required. CSV file with switch info
- `--password` - Change the default password
- `--hostname` - Set hostname from CSV file
- `--current-password PWD` - Current password if not default
- `--dry-run` - Show what would be done

**Requires:** `expect` and `sshpass` installed on the system.

### `change-default-password.py`
Simpler alternative to `change-switch-defaults.py` for password-only changes.

```bash
./scripts/change-default-password.py --csv .configs/from-dhcp.csv
```

**Options:**
- `--csv FILE` - Required. CSV file with switch info
- `--dry-run` - Show what would be done
- `--verify-only` - Check which switches need password change

**Requires:** `expect` or `sshpass` installed on the system.

### `change-default-hostname.py`
Set hostnames on switches based on MAC-to-hostname mapping.

```bash
./scripts/change-default-hostname.py --topology scripts/tests/sample-configs/test-topology.json --ips 192.168.200.161-166 --password <pwd>
```

**Options:**
- `--topology FILE` - Topology file for valid hostnames
- `--mapping FILE` - JSON file with MAC-to-hostname mapping
- `--ips RANGE` - IP addresses to process
- `--password PWD` - SSH password
- `--dry-run` - Show what would be done

**Requires:** `sshpass` installed on the system.

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
