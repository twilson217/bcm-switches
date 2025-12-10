# BCM Cumulus Device Management Scripts

This repository provides comprehensive automation scripts for managing Cumulus devices with Nvidia's BCM (Base Command Manager) using cm-lite-daemon.

> **Note:** These scripts are designed for BCM 10.

## 🚀 Quick Start - Complete Setup

For the simplest experience, use the **complete setup script** that handles everything automatically:

```bash
# Single device setup
python3 complete_cumulus_setup.py \
    --host 10.141.1.1 \
    --hostname spine01 \˜
    --mac 44:38:39:00:01:01 \
    --network internalnet \
    --bcm-master-ip 10.141.255.254

# Multiple devices from CSV
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --bcm-master-ip 10.141.255.254

# Dry run to see what would be done
python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run
```

**What it does automatically:**
- ✅ Adds devices to BCM
- ✅ Intelligently waits for and transfers bootstrap certificates
- ✅ Transfers cm-lite-daemon files
- ✅ Installs daemon with dependencies
- ✅ Registers nodes with BCM
- ✅ Provides comprehensive progress reporting

## 📁 Available Scripts

### Primary Script

| Script | Purpose | Usage |
|--------|---------|-------|
| **`complete_cumulus_setup.py`** | **🎯 Complete end-to-end automation** | **Recommended for most users** |

### Individual Scripts (for advanced users)

| Script | Purpose | Usage |
|--------|---------|-------|
| `add_cumulus_devices_to_bcm.py` | Add devices to BCM from CSV | Manual workflow control |
| `transfer_cm_lite_daemon.py` | Transfer daemon files to devices | Custom file management |
| `remote_install_cm_lite.py` | Install daemon and register with BCM | Advanced configuration |

## 📋 CSV File Format

Create a `cumulus.csv` file with your device information:

```csv
Hostname,IP,MAC,Network
spine01,10.141.1.1,44:38:39:00:01:01,internalnet
spine02,10.141.1.2,44:38:39:00:01:02,internalnet
leaf01,10.141.1.10,44:38:39:00:02:01,internalnet
leaf02,10.141.1.11,44:38:39:00:02:02,internalnet
```

## 🛠️ Prerequisites

1. **On BCM Server:**
   - Python 3.6+
   - SSH client and scp
   - cmsh access permissions
   - Access to `/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip`

2. **On Target Devices:**
   - SSH access (password or key-based)
   - Sudo privileges
   - Network connectivity to BCM

3. **Required Tools:**
   ```bash
   # For password authentication
   sudo apt install sshpass
   
   # Verify required files
   ls -la /cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip
   ```

## 📖 Documentation

### Quick Reference
- **[COMPLETE_SETUP_USAGE.md](COMPLETE_SETUP_USAGE.md)** - 🎯 **Start here** - Complete automation guide
- **[USAGE_EXAMPLES.md](USAGE_EXAMPLES.md)** - Comprehensive examples and batch scripts

### Advanced Documentation
- **[REMOTE_INSTALL_USAGE.md](REMOTE_INSTALL_USAGE.md)** - Remote installation details
- Individual script documentation within each file

## 🎯 Common Use Cases

### 1. Single Device Setup
```bash
python3 complete_cumulus_setup.py \
    --host 10.141.1.1 \
    --hostname spine01 \
    --mac 44:38:39:00:01:01 \
    --network internalnet
```

### 2. Bulk Device Setup
```bash
python3 complete_cumulus_setup.py --csv cumulus.csv
```

### 3. SSH Key Authentication
```bash
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --ssh-key ~/.ssh/cumulus_key \
    --bcm-master-ip 10.141.255.254
```

### 4. Custom Configuration
```bash
python3 complete_cumulus_setup.py \
    --csv cumulus.csv \
    --username admin \
    --bcm-master-name bcm-server \
    --bcm-master-ip 10.141.255.254 \
    --install-dir /usr/local
```

## 🔧 Manual Workflow (Advanced Users)

If you need fine-grained control, use the individual scripts:

```bash
# Step 1: Add devices to BCM
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv --execute

# Step 2: Transfer daemon to each device
python3 transfer_cm_lite_daemon.py --host 10.141.1.1 --username cumulus --password 1234

# Step 3: Install and register
python3 remote_install_cm_lite.py \
    --host 10.141.1.1 \
    --username cumulus \
    --password 1234 \
    --switch-name spine01 \
    --register-node \
    --bcm-master-ip 10.141.255.254
```

## 🐛 Troubleshooting

### Common Issues

1. **Missing Scripts Error**
   ```
   FileNotFoundError: Missing required scripts: ...
   ```
   **Solution:** Ensure all scripts are in the same directory.

2. **Authentication Failures**
   ```
   SSH connection failed
   ```
   **Solution:** Verify SSH credentials and network connectivity.

3. **BCM Connection Issues**
   ```
   Failed to add devices to BCM
   ```
   **Solution:** Check cmsh access and BCM server connectivity.

4. **Bootstrap Certificate Not Found**
   ```
   bootstrap.key not found
   ```
   **Solution:** Ensure devices were successfully added to BCM first.

### Debug Mode
```bash
# Use dry run to see what would be executed
python3 complete_cumulus_setup.py --csv cumulus.csv --dry-run

# Check individual components
python3 add_cumulus_devices_to_bcm.py --csv cumulus.csv  # dry run by default
```

## 📊 Features

| Feature | Complete Setup | Individual Scripts |
|---------|---------------|-------------------|
| **Ease of Use** | ✅ Single command | ❌ Multiple steps |
| **Progress Tracking** | ✅ Phase-based | ❌ Manual |
| **Error Recovery** | ✅ Continues on partial failure | ❌ Manual intervention |
| **Dry Run** | ✅ Available | ✅ Partial support |
| **Customization** | ✅ Command line options | ✅ Full control |
| **Batch Processing** | ✅ Automatic | ❌ Manual scripting |
| **Summary Reporting** | ✅ Comprehensive | ❌ Manual verification |

## 🤝 Contributing

1. Test scripts with dry run mode
2. Follow existing code style and documentation patterns
3. Update relevant documentation files
4. Ensure backward compatibility with existing CSV files

## 📝 License

This project is provided as-is for BCM Cumulus device management automation.

---

**⭐ Recommended:** Start with `complete_cumulus_setup.py` for the best experience, then explore individual scripts if you need advanced customization.


  
