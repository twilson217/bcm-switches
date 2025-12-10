# Complete Cumulus Setup Script Usage Guide

The `complete_cumulus_setup.py` script provides **end-to-end automation** for adding Cumulus devices to BCM management with cm-lite-daemon. It orchestrates all the existing scripts to provide a single-command solution.

## Overview

This script automatically performs the complete workflow:

1. **📋 Add devices to BCM** - Uses `add_cumulus_devices_to_bcm.py`
2. **🔍 Check connectivity** - Pings devices to verify reachability
3. **📦 Transfer cm-lite-daemon** - Uses `transfer_cm_lite_daemon.py`  
4. **⚙️ Install & register** - Uses `remote_install_cm_lite.py`
5. **📊 Report results** - Provides comprehensive summary with connectivity status

**Key Features:**
- ✅ **BCM Master IP auto-detection** - Automatically detects BCM master IP using cmsh
- ✅ **Automatic connectivity checks** - Skips unreachable devices
- ✅ **Intelligent error handling** - Continues with accessible devices
- ✅ **Comprehensive reporting** - Shows successful, failed, and skipped devices
- ✅ **Dry run mode** - Preview actions without execution

## Prerequisites

1. **Required scripts** (must be in the same directory):
   - `add_cumulus_devices_to_bcm.py`
   - `transfer_cm_lite_daemon.py`
   - `remote_install_cm_lite.py`

2. **System requirements**:
   - Python 3.6+ with required packages
   - SSH client and scp
   - Access to BCM server with cmsh permissions
   - Access to `/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip`

3. **Network access**:
   - SSH access to target Cumulus devices
   - Sudo privileges on target devices

## Usage Modes

### 1. Single Device Setup

```bash
# Basic single device setup (auto-detects BCM master IP)
python3 complete_cumulus_setup.py \
    --host 10.141.1.1 \
    --hostname spine01 \
    --mac 44:38:39:00:01:01 \
    --network internalnet

# With explicit BCM master IP
python3 complete_cumulus_setup.py \
    --host 10.141.1.1 \
    --hostname spine01 \
    --mac 44:38:39:00:01:01 \
    --network internalnet \
    --bcm-master-ip 10.141.255.254

# With SSH key authentication (auto-detects BCM master IP)
python3 complete_cumulus_setup.py \
    --host 10.141.1.1 \
    --hostname spine01 \
    --mac 44:38:39:00:01:01 \
    --network internalnet \
    --ssh-key ~/.ssh/cumulus_key
```

### 2. Multiple Devices from CSV

```bash
# Process all devices in CSV file (auto-detects BCM master IP)
python3 complete_cumulus_setup.py --csv cumulus.csv

# With custom authentication (auto-detects BCM master IP)
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --username admin \
    --password mypassword

# With SSH key for all devices (auto-detects BCM master IP)
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --ssh-key ~/.ssh/cumulus_automation_key

# With explicit BCM master IP
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --bcm-master-ip 10.141.255.254

# Skip ping checks
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --skip-ping
```

### 3. Dry Run Mode

```bash
# See what would be done without executing
python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run

# Dry run for single device
python3 complete_cumulus_setup.py \
    --host 10.141.1.10 \
    --hostname spine01 \
    --mac 44:38:39:00:01:01 \
    --network spine \
    --dry-run
```

### 4. Connectivity Management

```bash
# Default behavior - ping check each device
python3 complete_cumulus_setup.py --csv cumulus.csv

# Skip ping checks (useful when devices are in maintenance or behind firewall)
python3 complete_cumulus_setup.py --csv cumulus.csv --skip-ping

# Example output with connectivity checks:
# 🔍 Checking connectivity to spine01 (10.141.1.1)...
# ✅ spine01 is reachable
# 🔍 Checking connectivity to leaf01 (10.141.1.10)...
# ❌ leaf01 is not reachable via ping
# ⏭️ Skipping leaf01 due to connectivity issues
```

## Command Line Options

```bash
python3 complete_cumulus_setup.py [OPTIONS]

Device Specification (choose one):
  --host IP                  Single device IP address
  --csv FILE                 CSV file with device information

Single Device Parameters (required with --host):
  --hostname NAME            Device hostname
  --mac MAC_ADDRESS          Device MAC address  
  --network NETWORK          Device network/role

Authentication (choose one):
  --password PASSWORD        SSH password (prompted if not provided)
  --ssh-key PATH            Path to SSH private key

Configuration:
  --username USER           SSH username (default: cumulus)
  --bcm-master-ip IP        BCM master IP (optional - auto-detects using cmsh if not provided)
  --bcm-master-name NAME    BCM master hostname (default: master)
  --install-dir PATH        Installation directory (default: /opt)

Operation:
  --dry-run                 Show commands without executing
  --skip-ping               Skip ping connectivity checks before setup
```

## CSV File Format

The script uses the same CSV format as the other scripts:

```csv
Hostname,IP,MAC,Network
spine01,10.141.1.1,44:38:39:00:01:01,internalnet
spine02,10.141.1.2,44:38:39:00:01:02,internalnet
leaf01,10.141.1.10,44:38:39:00:02:01,internalnet
leaf02,10.141.1.11,44:38:39:00:02:02,internalnet
```

Supported column name variations:
- **Hostname**: `Hostname`, `hostname`, `HOSTNAME`
- **IP**: `IP`, `ip`, `IP_Address`
- **MAC**: `MAC`, `mac`, `MAC_Address`
- **Network**: `Network`, `network`, `NETWORK`

## BCM Master IP Auto-Detection

The script automatically detects the BCM master IP using the `cmsh` command:

```bash
cmsh -c 'device; use master; get ip'
```

**Auto-detection behavior:**
- ✅ **Enabled by default** when `--bcm-master-ip` is not specified
- ✅ **Runs on BCM systems** with `cmsh` available
- ✅ **Falls back to manual prompt** if auto-detection fails
- ✅ **Shows detected IP** for verification

**Manual override:**
- Use `--bcm-master-ip` to specify IP explicitly
- Useful when running from non-BCM systems or custom setups

**Example output:**
```
Auto-detecting BCM master IP...
✓ Detected BCM master IP: 10.141.255.254
```

## Bootstrap Certificate Handling

The script includes intelligent handling of BCM bootstrap certificate generation:

- **Initial Wait**: After adding devices to BCM, waits 15 seconds for the initialize process to begin
- **Automatic Polling**: During individual device setup, automatically waits up to 2 minutes for bootstrap files to be generated
- **Progress Updates**: Shows waiting progress every 15 seconds with clear status messages
- **Detailed Error Messages**: If files aren't generated within 2 minutes, provides specific troubleshooting guidance

This eliminates the common issue where bootstrap files aren't immediately available after the BCM initialize command completes.

## Workflow Phases

The script executes in two main phases:

### Phase 1: BCM Device Addition
- Creates temporary CSV file with all devices
- Adds all devices to BCM in batch using `add_cumulus_devices_to_bcm.py`
- Waits briefly for BCM initialize process to begin
- **Stops if BCM addition fails**

### Phase 2: Individual Device Setup
For each device:
1. **Transfer daemon files** using `transfer_cm_lite_daemon.py`
2. **Install and register** using `remote_install_cm_lite.py` with:
   - **Intelligent bootstrap certificate waiting** - automatically waits up to 2 minutes for BCM to generate certificates
   - Bootstrap certificate transfer
   - **Python dependencies installation** - installs all required packages including OpenSSL
   - BCM master hosts configuration
   - **Node registration** - executed after all prerequisites are in place

## Example Output

```
📝 Devices to be configured:
   - spine01 (10.141.1.1) - MAC: 44:38:39:00:01:01 - Network: internalnet
   - leaf01 (10.141.1.10) - MAC: 44:38:39:00:02:01 - Network: internalnet

Proceed with setup of 2 device(s)? (y/N): y

Auto-detecting BCM master IP...
✓ Detected BCM master IP: 10.141.255.254

🎯 Starting complete Cumulus device setup for 2 device(s)
================================================================================

📋 PHASE 1: Adding all devices to BCM
============================================================
STEP: Adding 2 device(s) to BCM
============================================================
Executing: python3 add_cumulus_devices_to_bcm.py --csv /tmp/tmpXXXXXX.csv --execute

Processing devices...
[1/2] Processing spine01...
✓ Device spine01 configured successfully
[2/2] Processing leaf01...
✓ Device leaf01 configured successfully

✓ Adding 2 device(s) to BCM completed successfully
Waiting 15 seconds for BCM initialize process to begin generating bootstrap certificates...
(Individual device setup will wait for actual file generation if needed)

🔧 PHASE 2: Setting up individual devices

--- Device 1/2 ---

🚀 Starting complete setup for spine01 (10.141.1.1)
🔍 Checking connectivity to spine01 (10.141.1.1)...
✅ spine01 is reachable
============================================================
STEP: Transferring cm-lite-daemon to spine01 (10.141.1.1)
============================================================
Executing: python3 transfer_cm_lite_daemon.py --host 10.141.1.1 --username cumulus --password ****

Working directory: /tmp/cm_lite_daemon_abc123
✓ cm-lite-daemon transferred successfully
✓ pip packages transferred successfully

✓ Transferring cm-lite-daemon to spine01 (10.141.1.1) completed successfully
============================================================
STEP: Installing and registering spine01 (10.141.1.1)
============================================================
Executing: python3 remote_install_cm_lite.py --host 10.141.1.1 --username cumulus --password **** --switch-name spine01 --register-node --bcm-master-ip 10.141.255.254

Starting cm-lite-daemon installation on cumulus@10.141.1.1
============================================================
✓ Installation completed successfully!
✓ Bootstrap certificates installed at: /opt/cm-lite-daemon/etc
✓ Node registered with BCM master: 10.141.255.254

✓ Installing and registering spine01 (10.141.1.1) completed successfully
✅ Successfully completed setup for spine01

--- Device 2/2 ---

🚀 Starting complete setup for leaf01 (10.141.1.20)
🔍 Checking connectivity to leaf01 (10.141.1.20)...
❌ leaf01 is not reachable via ping
⏭️ Skipping leaf01 due to connectivity issues

================================================================================
🏁 SETUP SUMMARY
================================================================================
✅ Successful: 1
   - spine01

⏭️ Skipped (connectivity issues): 1
   - leaf01

Total devices processed: 2
Success rate: 1/2 (50.0%)
Success rate (accessible devices only): 1/1 (100.0%)

🎉 Next steps for successful devices:
1. Verify devices appear in BCM device management
2. Start cm-lite-daemon service on devices if needed
3. Monitor logs for connectivity issues
4. Configure any device-specific settings in BCM

⚠️ For skipped devices:
1. Check network connectivity and routing
2. Verify device IP addresses are correct
3. Ensure devices are powered on and accessible
4. Re-run setup once connectivity is restored
```

## Interactive Prompts

The script may prompt for information not provided via command line:

### Password Prompt
```
Password for cumulus: [hidden input]
```

### BCM Master IP Auto-Detection and Fallback
```
Auto-detecting BCM master IP...
✓ Detected BCM master IP: 10.141.255.254
```

If auto-detection fails:
```
⚠️ Auto-detection failed: cmsh command not found. Make sure you're running this script on a BCM system.

BCM master IP is required for device registration.
Enter BCM master IP address manually: 10.141.1.5
```

### Confirmation Prompt
```
Proceed with setup of 3 device(s)? (y/N): y
```

## Error Handling

### Script Validation
```
FileNotFoundError: Missing required scripts: transfer_cm_lite_daemon.py, remote_install_cm_lite.py
```
**Solution**: Ensure all required scripts are in the same directory.

### Device Validation
```
Error: All device information is required (hostname, ip, mac, network)
```
**Solution**: Provide all required parameters for single device setup.

### Connectivity Issues
```
❌ spine01 is not reachable via ping
⏭️ Skipping spine01 due to connectivity issues
```
**Solution**: 
- Verify the device IP address is correct
- Check network routing and connectivity
- Ensure the device is powered on and network interface is up
- Test manual ping: `ping -c 3 10.141.1.10`
- Use `--skip-ping` if devices are behind firewall but SSH accessible

### Ping Timeout Issues
```
❌ leaf01 ping timeout after 15 seconds
```
**Solution**:
- Check for network latency or routing issues
- Verify firewall rules allow ICMP traffic
- Consider using `--skip-ping` for high-latency environments

### BCM Master IP Auto-Detection Failure
```
⚠️ Auto-detection failed: cmsh command not found. Make sure you're running this script on a BCM system.
```
**Solution**: 
- Ensure you're running the script on a BCM system with `cmsh` available
- Or manually specify the BCM master IP using `--bcm-master-ip <IP_ADDRESS>`

```
⚠️ Auto-detection failed: Failed to detect BCM master IP using cmsh command: Command 'cmsh -c device; use master; get ip' returned non-zero exit status 1
```
**Solution**: 
- Check BCM cluster status: `cmsh -c 'cluster; show'`
- Verify BCM master is configured and accessible
- Use `--bcm-master-ip` to specify IP manually

### BCM Addition Failure
```
❌ Failed to add devices to BCM. Aborting.
```
**Solution**: Check BCM connectivity and cmsh permissions.

### Partial Failures
```
❌ Failed: 1
   - leaf02

Success rate: 3/4 (75.0%)
```
The script continues processing other devices even if some fail.

## Advanced Usage

### Custom Installation Directory
```bash
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --install-dir /usr/local \
    --bcm-master-ip 10.141.1.5
```

### Custom BCM Master Name
```bash
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --bcm-master-name bcm-server
    # BCM master IP will be auto-detected
```

### Manual BCM Master IP Override
```bash
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --bcm-master-ip 10.141.1.5
    # Skips auto-detection and uses specified IP
```

### Environment Variables
```bash
# Set password via environment (auto-detects BCM master IP)
export CUMULUS_PASSWORD="mypassword"
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --password "$CUMULUS_PASSWORD"
```

## Comparison with Manual Process

| Task | Manual Process | Complete Setup Script |
|------|---------------|----------------------|
| **Connectivity Check** | Manual ping/test each device | ✅ Automatic ping checks |
| **Device Addition** | Run `add_cumulus_devices_to_bcm.py` separately | ✅ Automated |
| **File Transfer** | Run `transfer_cm_lite_daemon.py` for each device | ✅ Automated |
| **Installation** | Run `remote_install_cm_lite.py` for each device | ✅ Automated |
| **Error Handling** | Manual intervention needed | ✅ Continues on partial failures |
| **Progress Tracking** | No centralized tracking | ✅ Phase-based progress |
| **Summary Reporting** | Manual verification | ✅ Comprehensive summary |
| **Unreachable Devices** | Fails with SSH timeout | ✅ Skips with clear reporting |
| **Dry Run** | Not available | ✅ Available |

## Best Practices

1. **Always test with dry run first**:
   ```bash
   python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run
   ```

2. **Use SSH keys for automation**:
   ```bash
   python3 complete_cumulus_setup.py \
       --csv cumulus.csv \
       --ssh-key ~/.ssh/cumulus_automation_key
   ```

3. **Verify CSV file before running**:
   ```bash
   head -5 cumulus.csv
   python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run
   ```

4. **Test connectivity manually for troubleshooting**:
   ```bash
   # Test ping connectivity
   ping -c 3 10.141.1.10
   
   # Test SSH connectivity
   ssh -o ConnectTimeout=10 cumulus@10.141.1.10 "echo 'SSH OK'"
   ```

5. **Monitor during execution**:
   - Watch for connectivity failures in Phase 2
   - Check network connectivity if many devices are skipped
   - Verify BCM accessibility

6. **Handle mixed environments**:
   ```bash
   # For environments with firewalls blocking ping but allowing SSH
   python3 complete_cumulus_setup.py --csv cumulus.csv --skip-ping
   
   # For high-latency networks, test ping timeout first
   ping -c 3 -W 5 10.141.1.10
   ```

7. **Post-setup verification**:
   ```bash
   # Check devices in BCM
   cmsh -c 'device; show'
   
   # Verify daemon status on devices
   ssh cumulus@10.141.1.10 "ps aux | grep cm-lite-daemon"
   ```

This script provides the ultimate automation solution for Cumulus device management setup, reducing a complex multi-step process to a single command while maintaining flexibility and comprehensive error reporting. 