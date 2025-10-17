#!/usr/bin/env python3
"""
Script to download cm-lite-daemon and transfer it to client hosts.

This script automates the process of:
1. Copying cm-lite-daemon.zip from BCM (without extracting)
2. Downloading required pip packages
3. Transferring both the zip file and pip packages to target client hosts

Usage:
    python3 transfer_cm_lite_daemon.py --host <IP_or_hostname> --username <user> --password <pass>
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import zipfile


class CMLiteDaemonTransfer:
    def __init__(self, host, username="cumulus", password=None, ssh_key=None):
        self.host = host
        self.username = username
        self.password = password
        self.ssh_key = ssh_key
        self.work_dir = None
        
    def setup_work_directory(self):
        """Create a temporary working directory"""
        self.work_dir = Path(tempfile.mkdtemp(prefix="cm_lite_daemon_"))
        print(f"Working directory: {self.work_dir}")
        return self.work_dir
        
    def copy_cm_lite_daemon_zip(self):
        """Copy cm-lite-daemon.zip without extracting"""
        cm_lite_zip_path = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")
        
        if not cm_lite_zip_path.exists():
            raise FileNotFoundError(f"cm-lite-daemon.zip not found at {cm_lite_zip_path}")
            
        # Copy to work directory
        local_zip = self.work_dir / "cm-lite-daemon.zip"
        shutil.copy2(cm_lite_zip_path, local_zip)
        print(f"Copied cm-lite-daemon.zip to {local_zip}")
        
        return local_zip
        
    def download_pip_packages(self):
        """Download required pip packages by extracting requirements.txt from zip"""
        pip_packages_dir = self.work_dir / "pip_packages_dep"
        pip_packages_dir.mkdir(exist_ok=True)
        
        # Extract requirements.txt from the zip file temporarily
        local_zip = self.work_dir / "cm-lite-daemon.zip"
        requirements_content = None
        
        with zipfile.ZipFile(local_zip, 'r') as zip_ref:
            # Look for requirements.txt in the zip
            requirements_file = None
            for filename in zip_ref.namelist():
                if filename.endswith('requirements.txt'):
                    requirements_file = filename
                    break
            
            if not requirements_file:
                raise FileNotFoundError("requirements.txt not found in cm-lite-daemon.zip")
            
            # Extract requirements.txt content
            with zip_ref.open(requirements_file) as req_file:
                requirements_content = req_file.read().decode('utf-8')
        
        # Write requirements.txt to work directory
        temp_requirements = self.work_dir / "requirements.txt"
        with open(temp_requirements, 'w') as f:
            f.write(requirements_content)
        
        print(f"Extracted requirements.txt from zip file")
        print(f"Downloading pip packages from {temp_requirements}")
        
        # Download packages using pip
        cmd = [
            "pip", "download",
            "--python-version", "3.11", 
            "-r", str(temp_requirements),
            "--dest", str(pip_packages_dir),
            "--no-deps"
        ]
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            print("Successfully downloaded pip packages")
            print(f"Packages saved to: {pip_packages_dir}")
            
            # List downloaded packages
            packages = list(pip_packages_dir.glob("*"))
            print(f"Downloaded {len(packages)} packages:")
            for pkg in packages:
                print(f"  - {pkg.name}")
                
        except subprocess.CalledProcessError as e:
            print(f"Error downloading pip packages: {e}")
            print(f"stdout: {e.stdout}")
            print(f"stderr: {e.stderr}")
            raise
        
        # Clean up temporary requirements.txt
        temp_requirements.unlink()
            
        return pip_packages_dir
        
    def transfer_to_client(self, zip_file, pip_packages_dir):
        """Transfer zip file and pip packages to the target client host using SCP"""
        print(f"Transferring files to {self.username}@{self.host}")
        
        # Prepare SCP command base
        scp_base = ["scp", "-r"]
        
        # Add SSH key if provided
        if self.ssh_key:
            scp_base.extend(["-i", self.ssh_key])
        
        # Add password handling if needed (requires sshpass)
        if self.password:
            scp_base = ["sshpass", "-p", self.password] + scp_base
            
        try:
            # Transfer cm-lite-daemon.zip
            cmd_zip = ["scp"]
            if self.ssh_key:
                cmd_zip.extend(["-i", self.ssh_key])
            if self.password:
                cmd_zip = ["sshpass", "-p", self.password] + cmd_zip
            
            cmd_zip.extend([
                str(zip_file),
                f"{self.username}@{self.host}:/home/{self.username}/"
            ])
            
            print("Transferring cm-lite-daemon.zip...")
            subprocess.run(cmd_zip, check=True)
            print("✓ cm-lite-daemon.zip transferred successfully")
            
            # Transfer pip packages directory
            cmd_packages = scp_base + [
                str(pip_packages_dir),
                f"{self.username}@{self.host}:/home/{self.username}/"
            ]
            
            print("Transferring pip packages...")
            subprocess.run(cmd_packages, check=True)
            print("✓ pip packages transferred successfully")
            
        except subprocess.CalledProcessError as e:
            print(f"Error during transfer: {e}")
            raise
    
    def run_remote_commands(self):
        """Run installation commands on the remote client host"""
        commands = [
            "sudo apt update",
            "sudo apt install -y build-essential python3-dev python3-openssl unzip",
            "cd /home/cumulus && unzip -o cm-lite-daemon.zip",
            "sudo cp -r /home/cumulus/cm-lite-daemon /opt/",
            "cd /opt/cm-lite-daemon && sudo pip install --break-system-packages --no-index --find-links /home/cumulus/pip_packages_dep -r requirements.txt",
            "rm /home/cumulus/cm-lite-daemon.zip"
        ]
        
        ssh_base = ["ssh"]
        
        if self.ssh_key:
            ssh_base.extend(["-i", self.ssh_key])
            
        if self.password:
            ssh_base = ["sshpass", "-p", self.password] + ssh_base
            
        ssh_target = f"{self.username}@{self.host}"
        
        print(f"Running installation commands on {ssh_target}")
        
        for cmd in commands:
            print(f"Executing: {cmd}")
            try:
                full_cmd = ssh_base + [ssh_target, cmd]
                result = subprocess.run(full_cmd, check=True, capture_output=True, text=True)
                print(f"✓ Command completed successfully")
                if result.stdout:
                    print(f"stdout: {result.stdout}")
            except subprocess.CalledProcessError as e:
                print(f"✗ Command failed: {e}")
                print(f"stderr: {e.stderr}")
                # Continue with other commands
                
    def cleanup(self):
        """Clean up temporary working directory"""
        if self.work_dir and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
            print(f"Cleaned up working directory: {self.work_dir}")
            
    def transfer(self, install_remotely=False):
        """Main transfer process"""
        try:
            # Setup
            self.setup_work_directory()
            
            # Copy zip file (without extracting)
            zip_file = self.copy_cm_lite_daemon_zip()
            
            # Download pip packages (extract requirements.txt from zip temporarily)
            pip_packages_dir = self.download_pip_packages()
            
            # Transfer to client
            self.transfer_to_client(zip_file, pip_packages_dir)
            
            # Optionally run installation commands
            if install_remotely:
                self.run_remote_commands()
                
            print("\n✓ Transfer completed successfully!")
            print(f"Files transferred to {self.username}@{self.host}:")
            print(f"  - /home/{self.username}/cm-lite-daemon.zip")
            print(f"  - /home/{self.username}/pip_packages_dep/")
            
            if not install_remotely:
                print("\nNext steps on the client host:")
                print("1. sudo apt update")
                print("2. sudo apt install build-essential python3-dev python3-openssl unzip")
                print("3. cd /home/cumulus && unzip -o cm-lite-daemon.zip")
                print("4. sudo cp -r /home/cumulus/cm-lite-daemon /opt/")
                print("5. cd /opt/cm-lite-daemon && sudo pip install --break-system-packages --no-index --find-links ~/pip_packages_dep -r requirements.txt")
                
        except Exception as e:
            print(f"Error during transfer: {e}")
            raise
        finally:
            self.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Transfer cm-lite-daemon to client hosts")
    parser.add_argument("--host", required=True, help="IP address or hostname of the target client host")
    parser.add_argument("--username", default="cumulus", help="SSH username (default: cumulus)")
    parser.add_argument("--password", help="SSH password (will prompt if not provided)")
    parser.add_argument("--ssh-key", help="Path to SSH private key file")
    parser.add_argument("--install", action="store_true", help="Also run installation commands remotely")
    
    args = parser.parse_args()
    
    # Prompt for password if not provided and no SSH key
    if not args.password and not args.ssh_key:
        import getpass
        args.password = getpass.getpass(f"Password for {args.username}@{args.host}: ")
    
    # Check for required tools
    required_tools = ["pip", "scp", "ssh"]
    if args.password:
        required_tools.append("sshpass")
        
    missing_tools = []
    for tool in required_tools:
        if shutil.which(tool) is None:
            missing_tools.append(tool)
            
    if missing_tools:
        print(f"Error: Missing required tools: {', '.join(missing_tools)}")
        if "sshpass" in missing_tools:
            print("Install sshpass: apt install sshpass")
        sys.exit(1)
    
    # Create transfer instance and run
    transfer = CMLiteDaemonTransfer(
        host=args.host,
        username=args.username,
        password=args.password,
        ssh_key=args.ssh_key
    )
    
    try:
        transfer.transfer(install_remotely=args.install)
    except Exception as e:
        print(f"Transfer failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 