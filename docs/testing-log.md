# Testing Log

This document records the tests performed to validate the BCM switch deployment scripts.

## Test Environment

All tests are performed in [NVIDIA Air](https://air.nvidia.com) using this automation repo: [twilson217/bcm-in-nvidia-air](https://github.com/twilson217/bcm-in-nvidia-air)

### Topology

The topology file used to create the simulation is saved at `docs/test-topology.json`. This was the `default.json` file in the bcm-in-nvidia-air project at the time of testing (the default topology may change in the future). To reproduce these tests exactly, use the `test-topology.json` file.

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

2. **Changed default passwords on switches**
   ```bash
   ./scripts/change-default-password.py --csv .configs/from-dhcp.csv
   ```
   This sets a new password and eliminates the requirement to change password on next login.

3. **Mapped CSV to topology for hostname resolution**
   ```bash
   ./scripts/map-csv-topology.py --csv .configs/from-dhcp.csv --topology docs/test-topology.json
   ```

4. **Deployed switches to BCM**
   ```bash
   ./deploy_bcm_switches.py --csv .configs/from-dhcp.csv
   ```

### Status

🔄 **In Progress** - Currently at step 4 (deploy_bcm_switches.py)

### Results

*(To be updated as testing continues)*

---

## Future Tests

- [ ] Test `--airgapped` deployment mode
- [ ] Test `--resume` functionality after interrupted deployment
- [ ] Test `--retry-failed` for partial failure recovery
- [ ] Test `--connectivity-test` VRF detection
- [ ] Test consistency check when switches already exist in BCM
- [ ] Test BCM 11.x compatibility (when supported)

