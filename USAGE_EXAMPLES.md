# BCM Cumulus Device Management - Usage Examples

This document provides comprehensive examples for managing Cumulus devices with BCM using the provided scripts.

## Prerequisites

1. **Required files:**
   - `cumulus.csv` - Device inventory file
   - `add_cumulus_devices_to_bcm.py` - BCM device addition script
   - `transfer_cm_lite_daemon.py` - File transfer script
   - `remote_install_cm_lite.py` - Remote installation script

2. **Required access:**
   - BCM server access with cmsh permissions
   - SSH access to target Cumulus devices
   - Sudo privileges on target devices

## CSV File Format

Create a `cumulus.csv` file with your device information:

```csv
Hostname,IP,MAC,Network
spine01,10.141.1.1,44:38:39:00:01:01,internalnet
spine02,10.141.1.2,44:38:39:00:01:02,internalnet
leaf01,10.141.1.10,44:38:39:00:02:01,internalnet
leaf02,10.141.1.11,44:38:39:00:02:02,internalnet
```

## Complete Workflow Examples

### Example 1: Single Device Setup with Bootstrap Certificates

```bash
# Step 1: Add device to BCM (dry run first)
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv

# Step 2: Actually add the device to BCM
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv --execute

# Step 3: Transfer daemon files to the device
python3 transfer_cm_lite_daemon.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234

# Step 4: Install daemon with bootstrap certificates
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234 \
    --switch-name spine01 \
    --transfer-bootstrap
```

### Example 1: Complete Setup with Node Registration

```bash
# Step 1: Add device to BCM (dry run first)
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv

# Step 2: Actually add the device to BCM
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv --execute

# Step 3: Transfer daemon files to the device
python3 transfer_cm_lite_daemon.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234

# Step 4: Complete installation with node registration
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234 \
    --switch-name spine01 \
    --register-node \
    --bcm-master-ip 10.141.255.254
```

### Example 2: Multiple Devices with SSH Keys and Registration

```bash
# Step 1: Add all devices to BCM
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv --execute

# Step 2: Transfer and install for each device
DEVICES=(
    "10.141.1.1:spine01"
    "10.141.1.2:spine02" 
    "10.141.1.10:leaf01"
    "10.141.1.11:leaf02"
)

for device_info in "${DEVICES[@]}"; do
    IFS=':' read -r ip switch_name <<< "$device_info"
    
    echo "Processing $switch_name ($ip)..."
    
    # Transfer files
    python3 transfer_cm_lite_daemon.py \
        --host "$ip" \
        --ssh-key ~/.ssh/cumulus_key
    
    # Install with bootstrap certificates
    python3 remote_install_cm_lite.py \
        --host "$ip" \
        --ssh-key ~/.ssh/cumulus_key \
        --switch-name "$switch_name" \
        --transfer-bootstrap
        
    echo "Completed $switch_name"
    echo "---"
done
```

### Example 3: Multiple Devices with Complete Registration

```bash
# Step 1: Add all devices to BCM
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv --execute

# Step 2: Transfer and install with registration for each device
DEVICES=(
    "10.141.1.1:spine01"
    "10.141.1.2:spine02" 
    "10.141.1.10:leaf01"
    "10.141.1.11:leaf02"
)

BCM_MASTER_IP="10.141.255.254"

for device_info in "${DEVICES[@]}"; do
    IFS=':' read -r ip switch_name <<< "$device_info"
    
    echo "Processing $switch_name ($ip)..."
    
    # Transfer files
    python3 transfer_cm_lite_daemon.py \
        --host "$ip" \
        --ssh-key ~/.ssh/cumulus_key
    
    # Install with bootstrap certificates and register
    python3 remote_install_cm_lite.py \
        --host "$ip" \
        --ssh-key ~/.ssh/cumulus_key \
        --switch-name "$switch_name" \
        --register-node \
        --bcm-master-ip "$BCM_MASTER_IP"
        
    echo "Completed $switch_name"
    echo "---"
done
```

### Example 4: Custom Installation Directory

```bash
# Install to /usr/local instead of /opt
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username admin \
    --password secret123 \
    --switch-name spine01 \
    --transfer-bootstrap \
    --install-dir /usr/local
```

### Example 4: Mixed Authentication Methods

```bash
# Device with password authentication
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234 \
    --switch-name spine01 \
    --transfer-bootstrap

# Device with SSH key authentication  
python3 remote_install_cm_lite.py \
    --host 10.141.1.10 \
    --username admin \
    --ssh-key ~/.ssh/admin_key \
    --switch-name leaf01 \
    --transfer-bootstrap
```

## Advanced Scenarios

### Scenario 1: Different SSH and Sudo Passwords

```bash
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password ssh_password_123 \
    --sudo-password sudo_password_456 \
    --switch-name spine01 \
    --transfer-bootstrap
```

### Scenario 2: Custom Home Directory

```bash
# For devices with non-standard home directories
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username admin \
    --password admin123 \
    --home-dir /home/admin \
    --switch-name spine01 \
    --transfer-bootstrap
```

### Scenario 3: Force Specific OS Type

```bash
# For devices where auto-detection fails
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234 \
    --os-type debian \
    --switch-name spine01 \
    --transfer-bootstrap
```

## Batch Processing Scripts

### Complete Automation Script

```bash
#!/bin/bash
# complete_setup.sh - Automate entire BCM device setup

CSV_FILE="cumulus.csv"
USERNAME="cumulus"
PASSWORD="1234"

echo "Starting BCM device setup from $CSV_FILE"

# Step 1: Add all devices to BCM
echo "Adding devices to BCM..."
python3 add_cumulus_devices_to_bcm.py --csv "$CSV_FILE" --execute

if [ $? -ne 0 ]; then
    echo "Failed to add devices to BCM. Exiting."
    exit 1
fi

# Get BCM master IP for registration
echo "Enter BCM master IP address for node registration:"
read -r BCM_MASTER_IP

if [ -z "$BCM_MASTER_IP" ]; then
    echo "Warning: No BCM master IP provided. Nodes will not be registered automatically."
    REGISTER_NODES=false
else
    REGISTER_NODES=true
fi

# Step 2: Process each device
while IFS=, read -r hostname ip mac network; do
    # Skip header row
    if [ "$hostname" = "Hostname" ]; then
        continue
    fi
    
    echo "Processing $hostname ($ip)..."
    
    # Transfer files
    echo "  Transferring files..."
    python3 transfer_cm_lite_daemon.py \
        --host "$ip" \
        --username "$USERNAME" \
        --password "$PASSWORD"
    
    if [ $? -ne 0 ]; then
        echo "  Failed to transfer files to $hostname"
        continue
    fi
    
    # Install with bootstrap certificates and optionally register
    echo "  Installing daemon..."
    if [ "$REGISTER_NODES" = true ]; then
        python3 remote_install_cm_lite.py \
            --host "$ip" \
            --username "$USERNAME" \
            --password "$PASSWORD" \
            --switch-name "$hostname" \
            --register-node \
            --bcm-master-ip "$BCM_MASTER_IP"
    else
        python3 remote_install_cm_lite.py \
            --host "$ip" \
            --username "$USERNAME" \
            --password "$PASSWORD" \
            --switch-name "$hostname" \
            --transfer-bootstrap
    fi
    
    if [ $? -eq 0 ]; then
        echo "  ✓ Successfully completed $hostname"
    else
        echo "  ✗ Failed to install on $hostname"
    fi
    
    echo "  ---"
    
done < "$CSV_FILE"

echo "Batch setup completed!"
```

### Parallel Processing Script

```bash
#!/bin/bash
# parallel_setup.sh - Process multiple devices in parallel

CSV_FILE="cumulus.csv"
USERNAME="cumulus"  
PASSWORD="1234"
MAX_PARALLEL=4

process_device() {
    local hostname=$1
    local ip=$2
    local mac=$3
    local network=$4
    
    echo "[$hostname] Starting setup..."
    
    # Transfer files
    python3 transfer_cm_lite_daemon.py \
        --host "$ip" \
        --username "$USERNAME" \
        --password "$PASSWORD" &> "/tmp/${hostname}_transfer.log"
    
    if [ $? -ne 0 ]; then
        echo "[$hostname] Transfer failed. Check /tmp/${hostname}_transfer.log"
        return 1
    fi
    
    # Install with bootstrap
    python3 remote_install_cm_lite.py \
        --host "$ip" \
        --username "$USERNAME" \
        --password "$PASSWORD" \
        --switch-name "$hostname" \
        --transfer-bootstrap &> "/tmp/${hostname}_install.log"
    
    if [ $? -eq 0 ]; then
        echo "[$hostname] ✓ Setup completed successfully"
        return 0
    else
        echo "[$hostname] ✗ Installation failed. Check /tmp/${hostname}_install.log"
        return 1
    fi
}

# Add devices to BCM first
echo "Adding devices to BCM..."
python3 add_cumulus_devices_to_bcm.py --csv "$CSV_FILE" --execute

# Process devices in parallel
job_count=0
while IFS=, read -r hostname ip mac network; do
    # Skip header row
    if [ "$hostname" = "Hostname" ]; then
        continue
    fi
    
    # Wait if we've reached max parallel jobs
    while [ $(jobs -r | wc -l) -ge $MAX_PARALLEL ]; do
        sleep 1
    done
    
    # Start background job
    process_device "$hostname" "$ip" "$mac" "$network" &
    
done < "$CSV_FILE"

# Wait for all jobs to complete
wait
echo "All devices processed!"
```

## Verification Commands

After installation, verify the setup:

```bash
# Check daemon installation
ssh cumulus@10.141.1.1 "ls -la /opt/cm-lite-daemon/"

# Check bootstrap certificates
ssh cumulus@10.141.1.1 "sudo ls -la /opt/cm-lite-daemon/etc/"

# Test Python imports
ssh cumulus@10.141.1.1 "cd /opt/cm-lite-daemon && python3 -c 'import OpenSSL; print(\"OpenSSL OK\")'"

# Check daemon configuration
ssh cumulus@10.141.1.1 "sudo cat /opt/cm-lite-daemon/etc/bootstrap.pem | head -5"

# Check BCM master in /etc/hosts
ssh cumulus@10.141.1.1 "cat /etc/hosts | grep master"

# Test BCM master connectivity
ssh cumulus@10.141.1.1 "ping -c 3 master"

# Check if node is registered (if registration was performed)
ssh cumulus@10.141.1.1 "sudo /opt/cm-lite-daemon/register_node --status" 2>/dev/null || echo "Status check not available"

# Check daemon process
ssh cumulus@10.141.1.1 "ps aux | grep cm-lite-daemon"
```

## Troubleshooting Common Issues

### Issue: Bootstrap Files Not Found

```bash
# Check if device was added to BCM properly
cmsh -c 'device; show'

# Verify bootstrap files exist
ls -la /cm/local/apps/cmd/etc/htdocs/switch/spine01/

# Check exact switch name in BCM
cmsh -c 'device; show' | grep -i spine01
```

### Issue: SSH Connection Problems

```bash
# Test SSH connectivity manually
ssh -o ConnectTimeout=10 cumulus@10.141.1.1 "echo 'SSH OK'"

# Test SSH key authentication
ssh -i ~/.ssh/cumulus_key cumulus@10.141.1.1 "echo 'SSH Key OK'"

# Check SSH agent
ssh-add -l
```

### Issue: Sudo Permission Problems

```bash
# Test sudo access
ssh cumulus@10.141.1.1 "sudo -n echo 'Sudo OK'"

# Check sudoers configuration
ssh cumulus@10.141.1.1 "sudo cat /etc/sudoers | grep cumulus"
```

## Security Best Practices

1. **Use SSH keys when possible:**
   ```bash
   # Generate key for automation
   ssh-keygen -t rsa -b 4096 -f ~/.ssh/cumulus_automation_key -N ""
   
   # Deploy to devices
   ssh-copy-id -i ~/.ssh/cumulus_automation_key cumulus@10.141.1.1
   ```

2. **Secure password storage:**
   ```bash
   # Use environment variables instead of command line
   export CUMULUS_PASSWORD="your_password"
   python3 remote_install_cm_lite.py --host 10.141.1.1 --password "$CUMULUS_PASSWORD"
   ```

3. **Verify bootstrap certificate permissions:**
   ```bash
   ssh cumulus@10.141.1.1 "sudo ls -la /opt/cm-lite-daemon/etc/bootstrap.*"
   # Should show: -rw------- for .key and -rw-r--r-- for .pem
   ``` 