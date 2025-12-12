# Testing Log

This document records the tests performed to validate the BCM switch deployment scripts.

## Test Environment

All tests are performed in [NVIDIA Air](https://air.nvidia.com) using this automation repo: [twilson217/bcm-in-nvidia-air](https://github.com/twilson217/bcm-in-nvidia-air)

### Topology

The topology file used to create the simulation is saved at `sample-configs/test-topology.json`. This was the `default.json` file in the bcm-in-nvidia-air project at the time of testing (the default topology may change in the future). To reproduce these tests exactly, use the `test-topology.json` file.

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
   ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology sample-configs/test-topology.json
   ```

4. **Deployed switches to BCM (online mode - initial attempt)**
   ```bash
   ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv
   ```
   
   **Result:** Phase 4 (Installing cm-lite-daemon) failed due to pip package path mismatch bug. Fixed in commit c00f6dc.

5. **Prepared airgapped files**
   ```bash
   ./scripts/prep-airgapped.py
   ```
   This collected `cm-lite-daemon.zip` and pip dependencies into `.files/` directory.

6. **Deployed switches to BCM (airgapped mode)**
   ```bash
   ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv
   ```
   When prompted "Would you like to perform an airgapped installation?", selected **Y** (default).

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

## Future Tests

- [x] Test `--airgapped` deployment mode (tested via auto-detection)
- [x] Test `--resume` functionality after interrupted deployment
- [x] Test `--retry-failed` for partial failure recovery
- [ ] Test `--connectivity-test` VRF detection (standalone)
- [ ] Test consistency check when switches already exist in BCM
- [ ] Test BCM 11.x compatibility (when supported)

