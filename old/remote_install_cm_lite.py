#!/usr/bin/env python3
"""
Script to install cm-lite-daemon on remote hosts from pre-transferred files.

This script connects to remote hosts and installs cm-lite-daemon and its dependencies
from the pip_packages_dep folder that should already be present on the remote host.
It can also transfer bootstrap certificates from BCM after device addition and
register the node with BCM for complete setup automation.

Usage:
    python3 remote_install_cm_lite.py --host <IP_or_hostname> --username <user> --password <pass>
    python3 remote_install_cm_lite.py --host <IP_or_hostname> --ssh-key ~/.ssh/id_rsa
    python3 remote_install_cm_lite.py --host <IP_or_hostname> --switch-name <switch_name> --transfer-bootstrap
    python3 remote_install_cm_lite.py --host <IP_or_hostname> --switch-name <switch_name> --transfer-bootstrap --register-node --bcm-master-ip <BCM_IP>
    python3 remote_install_cm_lite.py --host <IP_or_hostname> --switch-name <switch_name> --transfer-bootstrap --register-node --bcm-master-ip <BCM_IP> --vrf production
"""

import argparse
import getpass
import shutil
import subprocess
import sys
from pathlib import Path


class RemoteCMLiteInstaller:
    def __init__(self, host, username="cumulus", password=None, ssh_key=None, 
                 os_type="debian", home_dir=None, install_dir="/opt", sudo_password=None,
                 switch_name=None, transfer_bootstrap=False, bcm_bootstrap_dir=None,
                 bcm_master_name="master", bcm_master_ip=None, register_node=False, vrf="mgmt"):
        self.host = host
        self.username = username
        self.password = password
        self.ssh_key = ssh_key
        self.os_type = os_type.lower()
        self.home_dir = home_dir or f"/home/{username}"
        self.install_dir = install_dir
        self.sudo_password = sudo_password or password  # Use SSH password as sudo password by default
        self.switch_name = switch_name
        self.transfer_bootstrap = transfer_bootstrap
        self.bcm_bootstrap_dir = bcm_bootstrap_dir or f"/cm/local/apps/cmd/etc/htdocs/switch/{switch_name}" if switch_name else None
        self.bcm_master_name = bcm_master_name
        self.bcm_master_ip = bcm_master_ip
        self.register_node = register_node
        self.vrf = vrf
        
    def _run_ssh_command(self, command, check=True, capture_output=True, sudo_password=None):
        """Execute a command on the remote host via SSH"""
        ssh_base = ["ssh"]
        
        if self.ssh_key:
            ssh_base.extend(["-i", self.ssh_key])
            
        if self.password:
            ssh_base = ["sshpass", "-p", self.password] + ssh_base
            
        ssh_target = f"{self.username}@{self.host}"
        
        # Handle sudo commands that need password
        if "sudo " in command and sudo_password:
            # Use sudo -S to read password from stdin
            command = command.replace("sudo ", "sudo -S ", 1)
            full_cmd = ssh_base + [ssh_target, command]
            try:
                result = subprocess.run(full_cmd, input=f"{sudo_password}\n", 
                                     check=check, capture_output=capture_output, text=True)
                return result
            except subprocess.CalledProcessError as e:
                print(f"✗ Command failed: {command}")
                print(f"Error: {e}")
                if e.stderr:
                    print(f"stderr: {e.stderr}")
                raise
        else:
            full_cmd = ssh_base + [ssh_target, command]
            try:
                result = subprocess.run(full_cmd, check=check, capture_output=capture_output, text=True)
                return result
            except subprocess.CalledProcessError as e:
                print(f"✗ Command failed: {command}")
                print(f"Error: {e}")
                if e.stderr:
                    print(f"stderr: {e.stderr}")
                raise

    def _run_scp_command(self, source_file, destination, check=True):
        """Transfer a file to the remote host via SCP"""
        scp_base = ["scp"]
        
        if self.ssh_key:
            scp_base.extend(["-i", self.ssh_key])
            
        if self.password:
            scp_base = ["sshpass", "-p", self.password] + scp_base
            
        ssh_target = f"{self.username}@{self.host}"
        full_destination = f"{ssh_target}:{destination}"
        
        full_cmd = scp_base + [source_file, full_destination]
        
        try:
            result = subprocess.run(full_cmd, check=check, capture_output=True, text=True)
            return result
        except subprocess.CalledProcessError as e:
            print(f"✗ SCP command failed: {' '.join(full_cmd)}")
            print(f"Error: {e}")
            if e.stderr:
                print(f"stderr: {e.stderr}")
            raise
    
    def get_bcm_master_ip(self):
        """Automatically determine BCM master IP using cmsh command"""
        if self.bcm_master_ip:
            # IP already provided, no need to detect
            return self.bcm_master_ip
            
        print("Auto-detecting BCM master IP...")
        try:
            result = subprocess.run(
                ["cmsh", "-c", "device; use master; get ip"], 
                check=True, 
                capture_output=True, 
                text=True
            )
            detected_ip = result.stdout.strip()
            if detected_ip:
                print(f"✓ Detected BCM master IP: {detected_ip}")
                self.bcm_master_ip = detected_ip
                return detected_ip
            else:
                raise ValueError("Empty IP address returned from cmsh command")
                
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to detect BCM master IP using cmsh command: {e}"
            if e.stderr:
                error_msg += f"\nError output: {e.stderr.strip()}"
            raise RuntimeError(error_msg)
        except FileNotFoundError:
            raise RuntimeError("cmsh command not found. Make sure you're running this script on a BCM system.")
    


    def check_prerequisites(self):
        """Check if the required files exist on the remote host"""
        print(f"Checking prerequisites on {self.username}@{self.host}")
        
        # Check if cm-lite-daemon directory exists
        daemon_path = f"{self.home_dir}/cm-lite-daemon"
        result = self._run_ssh_command(f"test -d {daemon_path}", check=False)
        if result.returncode != 0:
            raise FileNotFoundError(f"cm-lite-daemon directory not found at {daemon_path}")
        print(f"✓ Found cm-lite-daemon at {daemon_path}")
        
        # Check if pip_packages_dep directory exists
        packages_path = f"{self.home_dir}/pip_packages_dep"
        result = self._run_ssh_command(f"test -d {packages_path}", check=False)
        if result.returncode != 0:
            raise FileNotFoundError(f"pip_packages_dep directory not found at {packages_path}")
        print(f"✓ Found pip_packages_dep at {packages_path}")
        
        # Check if requirements.txt exists
        requirements_path = f"{daemon_path}/requirements.txt"
        result = self._run_ssh_command(f"test -f {requirements_path}", check=False)
        if result.returncode != 0:
            raise FileNotFoundError(f"requirements.txt not found at {requirements_path}")
        print(f"✓ Found requirements.txt at {requirements_path}")

    def check_bootstrap_files(self):
        """Check if bootstrap files exist on BCM, waiting for them to be generated if needed"""
        if not self.transfer_bootstrap or not self.bcm_bootstrap_dir:
            return
            
        print(f"Checking bootstrap files in {self.bcm_bootstrap_dir}")
        
        bootstrap_key = Path(self.bcm_bootstrap_dir) / "bootstrap.key"
        bootstrap_pem = Path(self.bcm_bootstrap_dir) / "bootstrap.pem"
        
        # Wait for bootstrap files to be generated (they are created after BCM initialize completes)
        import time
        max_wait_time = 120  # Wait up to 2 minutes
        check_interval = 5   # Check every 5 seconds
        waited_time = 0
        
        while waited_time < max_wait_time:
            if bootstrap_key.exists() and bootstrap_pem.exists():
                print(f"✓ Found bootstrap.key at {bootstrap_key}")
                print(f"✓ Found bootstrap.pem at {bootstrap_pem}")
                return
            
            if waited_time == 0:
                print(f"⏳ Bootstrap files not yet available. Waiting for BCM initialize to complete...")
                print(f"   (Will check every {check_interval} seconds for up to {max_wait_time} seconds)")
            
            time.sleep(check_interval)
            waited_time += check_interval
            
            if waited_time % 15 == 0:  # Progress update every 15 seconds
                print(f"   Still waiting for bootstrap files... ({waited_time}s elapsed)")
        
        # Final check and detailed error message
        missing_files = []
        if not bootstrap_key.exists():
            missing_files.append(f"bootstrap.key at {bootstrap_key}")
        if not bootstrap_pem.exists():
            missing_files.append(f"bootstrap.pem at {bootstrap_pem}")
            
        if missing_files:
            error_msg = f"Bootstrap files not found after {max_wait_time} seconds:\n"
            for file in missing_files:
                error_msg += f"  - {file}\n"
            error_msg += f"\nPossible causes:\n"
            error_msg += f"  1. BCM initialize process is still running (check 'cmsh -c \"device; show\"')\n"
            error_msg += f"  2. Device was not properly added to BCM\n"
            error_msg += f"  3. Switch name '{self.switch_name}' doesn't match the name in BCM\n"
            error_msg += f"  4. BCM bootstrap generation failed\n"
            error_msg += f"\nCheck BCM logs and device status before retrying."
            raise FileNotFoundError(error_msg)

    def transfer_bootstrap_files(self):
        """Transfer bootstrap files from BCM to the remote device"""
        if not self.transfer_bootstrap or not self.bcm_bootstrap_dir:
            return
            
        print("Transferring bootstrap files...")
        
        bootstrap_key = Path(self.bcm_bootstrap_dir) / "bootstrap.key"
        bootstrap_pem = Path(self.bcm_bootstrap_dir) / "bootstrap.pem"
        
        # Create etc directory in the daemon installation
        daemon_etc_dir = f"{self.install_dir}/cm-lite-daemon/etc"
        self._run_ssh_command(f"sudo mkdir -p {daemon_etc_dir}", sudo_password=self.sudo_password)
        
        # Transfer bootstrap.key
        print("  Transferring bootstrap.key...")
        temp_key_path = f"{self.home_dir}/bootstrap.key"
        self._run_scp_command(str(bootstrap_key), temp_key_path)
        self._run_ssh_command(f"sudo mv {temp_key_path} {daemon_etc_dir}/", sudo_password=self.sudo_password)
        self._run_ssh_command(f"sudo chown root:root {daemon_etc_dir}/bootstrap.key", sudo_password=self.sudo_password)
        self._run_ssh_command(f"sudo chmod 600 {daemon_etc_dir}/bootstrap.key", sudo_password=self.sudo_password)
        print("  ✓ bootstrap.key transferred and secured")
        
        # Transfer bootstrap.pem
        print("  Transferring bootstrap.pem...")
        temp_pem_path = f"{self.home_dir}/bootstrap.pem"
        self._run_scp_command(str(bootstrap_pem), temp_pem_path)
        self._run_ssh_command(f"sudo mv {temp_pem_path} {daemon_etc_dir}/", sudo_password=self.sudo_password)
        self._run_ssh_command(f"sudo chown root:root {daemon_etc_dir}/bootstrap.pem", sudo_password=self.sudo_password)
        self._run_ssh_command(f"sudo chmod 644 {daemon_etc_dir}/bootstrap.pem", sudo_password=self.sudo_password)
        print("  ✓ bootstrap.pem transferred and secured")
        
        print(f"✓ Bootstrap files transferred to {daemon_etc_dir}")

    def register_with_bcm(self):
        """Register the node with BCM using register_node command"""
        if not self.register_node:
            return
            
        print("Registering node with BCM...")
        
        daemon_dir = f"{self.install_dir}/cm-lite-daemon"
        
        # Check if register_node exists
        register_cmd = f"test -f {daemon_dir}/register_node"
        result = self._run_ssh_command(register_cmd, check=False)
        if result.returncode != 0:
            print(f"⚠ register_node not found at {daemon_dir}/register_node")
            print("  The daemon may need to be configured manually")
            return
        
        # Make sure register_node is executable
        self._run_ssh_command(f"sudo chmod +x {daemon_dir}/register_node", sudo_password=self.sudo_password)
        
        # Run the register_node command using BCM master IP directly
        register_cmd = f"cd {daemon_dir} && sudo ./register_node --host {self.bcm_master_ip} --disable-cert-check --vrf {self.vrf}"
        
        print(f"Executing: {register_cmd}")
        try:
            result = self._run_ssh_command(register_cmd, sudo_password=self.sudo_password)
            print("✓ Node registration completed successfully")
            if result.stdout:
                print(f"Registration output: {result.stdout.strip()}")
        except subprocess.CalledProcessError as e:
            print(f"⚠ Node registration failed or returned non-zero exit code")
            if e.stderr:
                print(f"Registration error: {e.stderr.strip()}")
            if e.stdout:
                print(f"Registration output: {e.stdout.strip()}")
            # Don't raise exception as registration might still be successful
            # even with non-zero exit codes in some cases

    def detect_os(self):
        """Detect the operating system of the remote host"""
        print("Detecting remote OS...")
        
        # Try to determine OS type
        try:
            result = self._run_ssh_command("cat /etc/os-release")
            os_release = result.stdout.lower()
            
            if "ubuntu" in os_release or "debian" in os_release:
                self.os_type = "debian"
                print("✓ Detected Debian/Ubuntu-based system")
            elif "centos" in os_release or "rhel" in os_release or "red hat" in os_release:
                self.os_type = "rhel"
                print("✓ Detected RHEL/CentOS-based system")
            elif "fedora" in os_release:
                self.os_type = "fedora"
                print("✓ Detected Fedora-based system")
            elif "suse" in os_release:
                self.os_type = "suse"
                print("✓ Detected SUSE-based system")
            else:
                print("⚠ Unknown OS, defaulting to Debian-style commands")
                self.os_type = "debian"
                
        except subprocess.CalledProcessError:
            print("⚠ Could not detect OS, defaulting to Debian-style commands")
            self.os_type = "debian"
    
    def install_system_dependencies(self):
        """Install system dependencies based on the detected OS"""
        print("Installing system dependencies...")
        
        if self.os_type == "debian":
            commands = [
                "sudo apt update",
                "sudo apt install -y build-essential python3-dev python3-pip",
                "sudo apt install -y python3-openssl || echo 'python3-openssl not available, continuing...'"
            ]
        elif self.os_type in ["rhel", "centos"]:
            commands = [
                "sudo yum update -y || sudo dnf update -y",
                "sudo yum groupinstall -y 'Development Tools' || sudo dnf groupinstall -y 'Development Tools'",
                "sudo yum install -y python3-devel python3-pip || sudo dnf install -y python3-devel python3-pip",
                "sudo yum install -y pyOpenSSL || sudo dnf install -y python3-pyOpenSSL || echo 'PyOpenSSL not available via package manager'"
            ]
        elif self.os_type == "fedora":
            commands = [
                "sudo dnf update -y",
                "sudo dnf groupinstall -y 'Development Tools'",
                "sudo dnf install -y python3-devel python3-pip",
                "sudo dnf install -y python3-pyOpenSSL || echo 'python3-pyOpenSSL not available, will install via pip'"
            ]
        elif self.os_type == "suse":
            commands = [
                "sudo zypper refresh",
                "sudo zypper install -y -t pattern devel_basis",
                "sudo zypper install -y python3-devel python3-pip",
                "sudo zypper install -y python3-pyOpenSSL || echo 'python3-pyOpenSSL not available, will install via pip'"
            ]
        else:
            # Fallback to Debian-style
            commands = [
                "sudo apt update",
                "sudo apt install -y build-essential python3-dev python3-pip",
                "sudo apt install -y python3-openssl || echo 'python3-openssl not available, continuing...'"
            ]
        
        for cmd in commands:
            print(f"Executing: {cmd}")
            try:
                result = self._run_ssh_command(cmd, sudo_password=self.sudo_password)
                print("✓ Command completed successfully")
            except subprocess.CalledProcessError as e:
                if "echo" in cmd:  # Don't fail on optional packages
                    print("⚠ Optional package not available, continuing...")
                else:
                    print(f"✗ Command failed: {e}")
                    # Continue with installation despite some failures
    
    def copy_daemon_to_system(self):
        """Copy cm-lite-daemon to the system installation directory"""
        print(f"Copying cm-lite-daemon to {self.install_dir}...")
        
        daemon_source = f"{self.home_dir}/cm-lite-daemon"
        daemon_dest = f"{self.install_dir}/cm-lite-daemon"
        
        # Remove existing installation if present
        self._run_ssh_command(f"sudo rm -rf {daemon_dest}", check=False, sudo_password=self.sudo_password)
        
        # Copy to system directory
        cmd = f"sudo cp -r {daemon_source} {self.install_dir}/"
        self._run_ssh_command(cmd, sudo_password=self.sudo_password)
        
        # Set appropriate permissions
        self._run_ssh_command(f"sudo chown -R root:root {daemon_dest}", sudo_password=self.sudo_password)
        
        print(f"✓ cm-lite-daemon copied to {daemon_dest}")
    
    def install_python_dependencies(self):
        """Install Python dependencies from the pip_packages_dep folder"""
        print("Installing Python dependencies...")
        
        packages_dir = f"{self.home_dir}/pip_packages_dep"
        daemon_dir = f"{self.install_dir}/cm-lite-daemon"
        requirements_file = f"{daemon_dir}/requirements.txt"
        
        # Determine pip install command based on Python version and system
        pip_cmd_base = "sudo pip3 install"
        
        # Check if we need --break-system-packages (for newer Python versions)
        check_cmd = "python3 -c \"import sys; print(sys.version_info >= (3, 11))\""
        try:
            result = self._run_ssh_command(check_cmd)
            if result.stdout.strip() == "True":
                pip_cmd_base += " --break-system-packages"
                print("✓ Using --break-system-packages for Python 3.11+")
        except:
            print("⚠ Could not determine Python version, proceeding without --break-system-packages")
        
        # Install packages
        install_cmd = f"cd {daemon_dir} && {pip_cmd_base} --no-index --find-links {packages_dir} -r {requirements_file}"
        
        print(f"Executing: {install_cmd}")
        self._run_ssh_command(install_cmd, sudo_password=self.sudo_password)
        print("✓ Python dependencies installed successfully")
    
    def verify_installation(self):
        """Verify that the installation was successful"""
        print("Verifying installation...")
        
        daemon_dir = f"{self.install_dir}/cm-lite-daemon"
        
        # Check if daemon directory exists
        self._run_ssh_command(f"test -d {daemon_dir}")
        print(f"✓ cm-lite-daemon directory exists at {daemon_dir}")
        
        # Check if Python can import required packages
        test_imports = [
            "import OpenSSL",
            "import websocket",
            "import yaml",
            "import psutil",
            "import cpuinfo",
            "import uptime",
            "import netifaces",
            "import dmidecode",
            "import requests"
        ]
        
        for import_stmt in test_imports:
            try:
                cmd = f"cd {daemon_dir} && python3 -c \"{import_stmt}\""
                self._run_ssh_command(cmd)
                print(f"✓ {import_stmt} - OK")
            except subprocess.CalledProcessError:
                print(f"⚠ {import_stmt} - Failed (may not be critical)")
    
    def install(self):
        """Main installation process"""
        print(f"Starting cm-lite-daemon installation on {self.username}@{self.host}")
        print("=" * 60)
        
        try:
            # Validate bootstrap transfer requirements
            if self.transfer_bootstrap and not self.switch_name:
                raise ValueError("--switch-name is required when --transfer-bootstrap is enabled")
            
            # Validate node registration requirements
            if self.register_node and not self.switch_name:
                raise ValueError("--switch-name is required when --register-node is enabled (bootstrap certificates needed)")
            
            # Auto-detect BCM master IP for registration if not provided
            if self.register_node:
                self.get_bcm_master_ip()  # This will auto-detect if not already set
            
            # Detect OS first (needed for package installation)
            self.detect_os()
            print()
            
            # Check and extract zip file if present (before checking prerequisites)
            self.check_and_extract_zip()
            print()
            
            # Check prerequisites (files should be extracted now)
            self.check_prerequisites()
            print()
            
            # Check bootstrap files if transfer is requested
            if self.transfer_bootstrap:
                self.check_bootstrap_files()
                print()
            
            # Install system dependencies
            self.install_system_dependencies()
            print()
            
            # Copy daemon to system location
            self.copy_daemon_to_system()
            print()
            
            # Transfer bootstrap files if requested
            if self.transfer_bootstrap:
                self.transfer_bootstrap_files()
                print()
            
            # Install Python dependencies
            self.install_python_dependencies()
            print()
            
            # Register with BCM (after bootstrap files and Python deps are ready)
            if self.register_node:
                self.register_with_bcm()
                print()
            
            # Verify installation
            self.verify_installation()
            print()
            
            print("=" * 60)
            print("✓ Installation completed successfully!")
            print(f"cm-lite-daemon installed at: {self.install_dir}/cm-lite-daemon")
            if self.transfer_bootstrap:
                print(f"Bootstrap certificates installed at: {self.install_dir}/cm-lite-daemon/etc")
            if self.register_node:
                print(f"Node registered with BCM master: {self.bcm_master_ip}")
            print()
            print("Next steps:")
            if not self.register_node:
                print("1. Register the node with BCM using: sudo ./register_node --host <BCM_IP> --disable-cert-check")
                print("2. Configure cm-lite-daemon settings if needed")
                print("3. Start the cm-lite-daemon service")
                print("4. Check logs and verify connectivity to BCM")
            else:
                print("1. Configure cm-lite-daemon settings if needed")
                print("2. Start the cm-lite-daemon service")
                print("3. Check logs and verify connectivity to BCM")
                print("4. Verify node appears in BCM device management")
            
        except Exception as e:
            print(f"\n✗ Installation failed: {e}")
            raise

    def check_and_extract_zip(self):
        """Check for zip file and extract if present"""
        zip_path = f"{self.home_dir}/cm-lite-daemon.zip"
        daemon_path = f"{self.home_dir}/cm-lite-daemon"
        
        # Check if zip file exists
        result = self._run_ssh_command(f"test -f {zip_path}", check=False)
        if result.returncode == 0:
            print(f"Found cm-lite-daemon.zip, extracting...")
            
            # Install unzip if needed
            self.install_unzip()
            
            # Extract the zip file
            extract_cmd = f"cd {self.home_dir} && unzip -o cm-lite-daemon.zip"
            self._run_ssh_command(extract_cmd)
            print("✓ cm-lite-daemon.zip extracted successfully")
            
            # Clean up zip file
            self._run_ssh_command(f"rm {zip_path}")
            print("✓ Zip file cleaned up")
            
            return True
        else:
            # No zip file found, assume files are already extracted
            return False

    def install_unzip(self):
        """Install unzip utility on the remote host if not present"""
        # Check if unzip is already installed
        result = self._run_ssh_command("which unzip", check=False)
        if result.returncode == 0:
            print("✓ unzip is already installed")
            return
            
        print("Installing unzip utility...")
        
        try:
            if self.os_type == "debian":
                # Run commands separately for proper sudo password handling
                print("Executing: sudo apt update")
                self._run_ssh_command("sudo apt update", sudo_password=self.sudo_password)
                print("Executing: sudo apt install -y unzip")
                self._run_ssh_command("sudo apt install -y unzip", sudo_password=self.sudo_password)
            elif self.os_type in ["rhel", "centos"]:
                # Try yum first, then dnf
                try:
                    print("Executing: sudo yum install -y unzip")
                    self._run_ssh_command("sudo yum install -y unzip", sudo_password=self.sudo_password)
                except subprocess.CalledProcessError:
                    print("yum failed, trying dnf...")
                    print("Executing: sudo dnf install -y unzip")
                    self._run_ssh_command("sudo dnf install -y unzip", sudo_password=self.sudo_password)
            elif self.os_type == "fedora":
                print("Executing: sudo dnf install -y unzip")
                self._run_ssh_command("sudo dnf install -y unzip", sudo_password=self.sudo_password)
            elif self.os_type == "suse":
                print("Executing: sudo zypper install -y unzip")
                self._run_ssh_command("sudo zypper install -y unzip", sudo_password=self.sudo_password)
            else:
                # Fallback to Debian-style
                print("Executing: sudo apt update")
                self._run_ssh_command("sudo apt update", sudo_password=self.sudo_password)
                print("Executing: sudo apt install -y unzip")
                self._run_ssh_command("sudo apt install -y unzip", sudo_password=self.sudo_password)
            
            print("✓ unzip installed successfully")
        except subprocess.CalledProcessError as e:
            print(f"✗ Failed to install unzip: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Install cm-lite-daemon on remote hosts")
    parser.add_argument("--host", required=True, 
                       help="IP address or hostname of the target host")
    parser.add_argument("--username", default="cumulus", 
                       help="SSH username (default: cumulus)")
    parser.add_argument("--password", 
                       help="SSH password (will prompt if not provided)")
    parser.add_argument("--sudo-password", 
                       help="Sudo password (defaults to SSH password)")
    parser.add_argument("--ssh-key", 
                       help="Path to SSH private key file")
    parser.add_argument("--os-type", choices=["debian", "rhel", "centos", "fedora", "suse", "auto"],
                       default="auto", help="Target OS type (default: auto-detect)")
    parser.add_argument("--home-dir", 
                       help="Home directory path on remote host (default: /home/username)")
    parser.add_argument("--install-dir", default="/opt",
                       help="Installation directory for cm-lite-daemon (default: /opt)")
    parser.add_argument("--switch-name", 
                       help="Name of the switch for bootstrap file transfer (required for --transfer-bootstrap)")
    parser.add_argument("--transfer-bootstrap", action="store_true",
                       help="Transfer bootstrap certificates from BCM after device addition")
    parser.add_argument("--bcm-master-name", default="master",
                       help="BCM master hostname for /etc/hosts and registration (default: master)")
    parser.add_argument("--bcm-master-ip",
                       help="BCM master IP address (optional - will auto-detect using cmsh if not provided)")
    parser.add_argument("--register-node", action="store_true",
                       help="Register the node with BCM after installation")
    parser.add_argument("--vrf", default="mgmt",
                       help="VRF to use for node registration (default: mgmt)")
    
    args = parser.parse_args()
    
    # Prompt for password if not provided and no SSH key
    if not args.password and not args.ssh_key:
        args.password = getpass.getpass(f"Password for {args.username}@{args.host}: ")
    
    # Check for required tools
    required_tools = ["ssh"]
    if args.password:
        required_tools.append("sshpass")
    if args.transfer_bootstrap:
        required_tools.append("scp")
        
    missing_tools = []
    for tool in required_tools:
        if shutil.which(tool) is None:
            missing_tools.append(tool)
            
    if missing_tools:
        print(f"Error: Missing required tools: {', '.join(missing_tools)}")
        if "sshpass" in missing_tools:
            print("Install sshpass: apt install sshpass")
        sys.exit(1)
    
    # Create installer instance and run
    installer = RemoteCMLiteInstaller(
        host=args.host,
        username=args.username,
        password=args.password,
        ssh_key=args.ssh_key,
        os_type=args.os_type,
        home_dir=args.home_dir,
        install_dir=args.install_dir,
        sudo_password=args.sudo_password,
        switch_name=args.switch_name,
        transfer_bootstrap=args.transfer_bootstrap,
        bcm_master_name=args.bcm_master_name,
        bcm_master_ip=args.bcm_master_ip,
        register_node=args.register_node,
        vrf=args.vrf
    )
    
    try:
        installer.install()
    except Exception as e:
        print(f"Installation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 