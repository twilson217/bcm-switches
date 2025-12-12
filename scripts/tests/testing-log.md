# Testing Log

This document records the tests performed to validate the BCM switch deployment scripts.

## Test Environment

All tests are performed in [NVIDIA Air](https://air.nvidia.com) using this automation repo: [twilson217/bcm-in-nvidia-air](https://github.com/twilson217/bcm-in-nvidia-air)

### Topology

The topology file used to create the simulation is saved at `scripts/tests/sample-configs/test-topology.json`. This was the `default.json` file in the bcm-in-nvidia-air project at the time of testing (the default topology may change in the future). To reproduce these tests exactly, use the `test-topology.json` file.

### Manual Configuration

After the simulation was launched, the following manual change was required on the `oob-mgmt-switch`:

```bash
nv set interface swp0-50 bridge domain br_default
nv config apply
```

**Reason:** Out of the box, the oob-mgmt-switch placed swp0 (connecting to bcm-01) into routed mode and pulled a DHCP address, which prevented the spine and leaf switches from being able to pull DHCP on their eth0 ports. These commands put all of those ports into bridged mode.

### BCM Version

All tests have been performed on **BCM 10.30.0**.

> **Note:** Support for BCM 11.x will be added in the future. For now, the `deploy_bcm_switches.py` script is expected **not** to work with BCM 11.x.

---

## Test 1: Full Deployment from DHCP Leases

**Date:** 2025-12-12

**Objective:** Test the complete workflow from DHCP lease discovery through BCM deployment.

### Steps Performed

1. **Generated CSV from DHCP leases**
   ```bash
   ./scripts/csv-from-dhcp.py
   ```
   Output: `.configs/from-dhcp.csv`

   > **Manual step:** Removed `oob-mgmt-switch` from the CSV file since it was no longer reachable after the bridging configuration change and was not planned for BCM deployment.

2. **Changed default passwords on switches**
   ```bash
   ./scripts/change-default-password.py --csv .configs/from-dhcp.csv
   ```
   This sets a new password and eliminates the requirement to change password on next login.

3. **Mapped CSV to topology for hostname resolution**
   ```bash
   ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology scripts/tests/sample-configs/test-topology.json
   ```

4. **Deployed switches to BCM**
   ```bash
   ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv
   ```
   
   The script automatically downloads required files (`cm-lite-daemon.zip` and pip packages) to `.files/` on the first run, then uses these cached files for all switch deployments. This "partially airgapped" approach means:
   - BCM downloads dependencies once from the internet
   - Switches receive files directly from BCM (no internet access required on switches)
   - Subsequent runs reuse cached files without re-downloading

   **Issues encountered and fixed:**
   - pip package path mismatch (packages transferred to wrong directory) - fixed in commit c00f6dc
   - Transfer check not verifying pip_packages_dep directory - fixed in commit 2992cef
   - Missing `cffi` package for Python 3.11 (only had cp312 version) - fixed in commit d2b1db7

### Status

✅ **PASSED** - All 6 switches successfully deployed to BCM.

### Results

- **Phase 2 (Add to BCM):** ✅ All 6 devices added successfully
- **Phase 3 (Transfer):** ✅ Files transferred successfully  
- **Phase 4 (Install):** ✅ cm-lite-daemon installed successfully
- **Phase 5 (Register):** ✅ All devices registered with BCM

### Devices Deployed

| Hostname | IP | MAC |
|----------|-----|-----|
| spine-02 | 192.168.200.161 | 48:B0:2D:09:AE:AC |
| leaf-04 | 192.168.200.162 | 48:B0:2D:F6:37:28 |
| leaf-02 | 192.168.200.163 | 48:B0:2D:A0:C1:83 |
| spine-01 | 192.168.200.164 | 48:B0:2D:C1:1D:6C |
| leaf-03 | 192.168.200.165 | 48:B0:2D:82:69:E6 |
| leaf-01 | 192.168.200.166 | 48:B0:2D:3B:C8:E6 |

---

## Test 2: Deployment with IP Discovery (No Input CSV)

**Date:** 2025-12-12

**Objective:** Test the workflow where the user provides IP addresses directly, and the script discovers hostnames from the switches.

### Prerequisites

This test was run immediately after Test 1. The simulation was reset using:
```bash
./scripts/tests/test-sim-reset.py
```

This removed the switches from BCM and rebuilt them in NVIDIA Air to factory defaults.

### Steps Performed

1. **Generated CSV from DHCP leases**
   ```bash
   ./scripts/csv-from-dhcp.py
   ```
   Output: `.configs/from-dhcp.csv` with IP and MAC addresses (hostnames show as "cumulus" - the default).

2. **Mapped MAC addresses to hostnames from topology**
   ```bash
   ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology scripts/tests/sample-configs/test-topology.json
   ```
   This updates the CSV with correct hostnames based on MAC address matching.

3. **Changed default passwords AND set hostnames in one step**
   ```bash
   ./scripts/change-switch-defaults.py --csv .configs/from-dhcp.csv --password --hostname
   ```
   This script:
   - SSHs to each switch once
   - Handles the forced password change on first login
   - Sets the hostname using NVUE commands
   - Verifies the hostname was applied

### Status

✅ **PASSED** - All 6 switches had passwords changed and hostnames set correctly.

### Notes

- The combined `change-switch-defaults.py` script is more efficient than running separate password and hostname scripts, as it only SSHs to each switch once.
- The `--password` and `--hostname` flags can be used independently or together.
- When used together, the script uses the new password immediately after changing it to set the hostname.

---

## Future Tests

- [x] Test `--resume` functionality after interrupted deployment
- [x] Test `--retry-failed` for partial failure recovery
- [x] Test fully airgapped deployment with `prep-airgapped.py` tarball
- [x] Test combined `change-switch-defaults.py` script
- [ ] Test `--connectivity-test` VRF detection (standalone)
- [ ] Test consistency check when switches already exist in BCM
- [ ] Test BCM 11.x compatibility (when supported)

