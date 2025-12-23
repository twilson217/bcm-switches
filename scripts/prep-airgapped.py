#!/usr/bin/env python3
"""
Airgapped Deployment Preparation Script

This script prepares a self-contained deployment package for airgapped BCM systems.
It downloads all required files (pip packages, deb packages, cm-lite-daemon) from
a Cumulus switch and creates a tarball that can be transferred to airgapped systems.

IMPORTANT: This script must be run with access to a Cumulus Linux switch that:
  1. Is running the SAME Cumulus version as your production switches
  2. Has internet access to download packages

You can run this script:
  - Directly ON a Cumulus switch (default: localhost)
  - From any system with SSH access to a Cumulus switch

Usage:
    python3 prep-airgapped.py                           # Interactive mode
    python3 prep-airgapped.py --switch 192.168.1.100    # Specify switch
    python3 prep-airgapped.py --output /path/to/out.tar.gz

For testing, you can use NVIDIA Air with Cumulus switches:
    https://github.com/twilson217/bcm-in-nvidia-air

Requirements:
    - Access to a Cumulus Linux switch with internet connectivity
    - Either a requirements file, cm-lite-daemon.zip, or use the built-in defaults
"""

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime
import re


# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
FILES_DIR = REPO_DIR / ".files"
CM_LITE_ZIP_PATH = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")

# Debian packages required for cm-lite-daemon installation
# These are needed to build Python packages with native extensions
REQUIRED_DEB_PACKAGES = [
    "build-essential",
    "python3-dev",
    "python3-pip",
    "unzip",
]

# Default requirements for cm-lite-daemon
# Compatible with both BCM 10.x and BCM 11.x (includes natsort for BCM 11)
DEFAULT_REQUIREMENTS = """\
pyOpenSSL>=21.0.0
websocket-client
pyyaml
psutil
py-cpuinfo
uptime
netifaces
py-dmidecode
requests
natsort
"""


def _python_version_to_tags(py_version: str) -> tuple[str, str]:
    """
    Convert '3.11' -> ('3.11', 'cp311'), '3.10' -> ('3.10', 'cp310'), '3.9' -> ('3.9', 'cp39').
    """
    parts = (py_version or "").strip().split(".")
    if len(parts) < 2:
        raise ValueError(f"Invalid python version '{py_version}' (expected MAJOR.MINOR, e.g. 3.11)")
    major = int(parts[0])
    minor = int(parts[1])
    if major != 3:
        raise ValueError(f"Unsupported python major version '{major}' (expected 3.x)")
    abi = f"cp{major}{minor}"
    return f"{major}.{minor}", abi


def check_cumulus_localhost() -> tuple[bool, str, str]:
    """
    Check if localhost is a Cumulus Linux system.
    
    Returns:
        Tuple of (is_cumulus, version, error_message)
    """
    try:
        with open("/etc/os-release") as f:
            content = f.read()
        
        if "cumulus" not in content.lower():
            return False, "", "This system is not running Cumulus Linux"
        
        # Extract version
        version = ""
        for line in content.splitlines():
            if line.startswith("VERSION_ID="):
                version = line.split("=", 1)[1].strip().strip('"')
                break
        
        return True, version, ""
    except FileNotFoundError:
        return False, "", "/etc/os-release not found"
    except Exception as e:
        return False, "", str(e)


def check_cumulus_remote(host: str, username: str, password: str) -> tuple[bool, str, str]:
    """
    Check if a remote host is a Cumulus Linux system via SSH.
    
    Returns:
        Tuple of (is_cumulus, version, error_message)
    """
    try:
        cmd = ["sshpass", "-p", password, "ssh",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=10",
               f"{username}@{host}",
               "cat /etc/os-release"]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            return False, "", f"SSH failed: {result.stderr.strip()[:200]}"
        
        content = result.stdout
        if "cumulus" not in content.lower():
            return False, "", "Remote system is not running Cumulus Linux"
        
        # Extract version
        version = ""
        for line in content.splitlines():
            if line.startswith("VERSION_ID="):
                version = line.split("=", 1)[1].strip().strip('"')
                break
        
        return True, version, ""
    except subprocess.TimeoutExpired:
        return False, "", "SSH connection timed out"
    except FileNotFoundError:
        return False, "", "sshpass not installed (required for remote access)"
    except Exception as e:
        return False, "", str(e)


def run_on_switch(host: str, username: str, password: str, command: str, 
                  *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command on a Cumulus switch via SSH."""
    if host == "localhost":
        # For localhost, handle sudo commands by piping password
        if command.strip().startswith("sudo "):
            # Use sudo -S to read password from stdin
            sudo_cmd = command.replace("sudo ", "sudo -S ", 1)
            proc = subprocess.run(
                sudo_cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, input=f"{password}\n"
            )
            return proc
        return subprocess.run(command, shell=True, capture_output=True, text=True, 
                            timeout=timeout, check=check)
    else:
        # For remote, handle sudo by piping password through echo
        if "sudo " in command:
            # Replace sudo with echo password | sudo -S
            command = command.replace("sudo ", f"echo '{password}' | sudo -S ", 1)
        
        ssh_cmd = ["sshpass", "-p", password, "ssh",
                   "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null",
                   f"{username}@{host}",
                   command]
        return subprocess.run(ssh_cmd, capture_output=True, text=True, 
                            timeout=timeout, check=check)


def copy_from_switch(host: str, username: str, password: str, 
                     remote_path: str, local_path: str) -> bool:
    """Copy files from a remote switch to local system."""
    if host == "localhost":
        if Path(remote_path.rstrip('/')).exists():
            src = Path(remote_path.rstrip('/'))
            if src.is_dir():
                shutil.copytree(src, local_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src, local_path)
            return True
        print(f"      [DEBUG] Local path not found: {remote_path}")
        return False
    else:
        # Use rsync for remote copy
        # Ensure local_path ends with / for directory copy
        local_target = str(local_path)
        if not local_target.endswith('/'):
            local_target += '/'
        
        rsync_cmd = [
            "sshpass", "-p", password,
            "rsync", "-avz", "--progress",
            "-e", "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            f"{username}@{host}:{remote_path}",
            local_target
        ]
        result = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            print(f"      [DEBUG] rsync failed: {result.stderr[:500] if result.stderr else 'no error output'}")
            return False
        
        return True


def download_packages_on_switch(host: str, username: str, password: str,
                                requirements: str, python_version: str) -> tuple[str, str]:
    """
    Download pip and deb packages on a Cumulus switch.
    
    Returns:
        Tuple of (pip_packages_dir, deb_packages_dir) on the switch
    """
    # Create temp directory on switch
    result = run_on_switch(host, username, password, "mktemp -d")
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create temp dir: {result.stderr}")
    temp_dir = result.stdout.strip()
    
    pip_dir = f"{temp_dir}/pip_packages_dep"
    deb_dir = f"{temp_dir}/deb_packages"
    
    print(f"\n  Downloading packages on {host}...")
    print(f"    Temp directory: {temp_dir}")
    
    # Create directories
    run_on_switch(host, username, password, f"mkdir -p {pip_dir} {deb_dir}")
    
    # Write requirements to switch
    req_file = f"{temp_dir}/requirements.txt"
    # Escape the requirements for shell
    escaped_req = requirements.replace("'", "'\\''")
    run_on_switch(host, username, password, f"echo '{escaped_req}' > {req_file}")
    
    # Download pip packages
    print("    Downloading pip packages...")
    v_norm, abi = _python_version_to_tags(python_version)
    
    # First try to download wheels
    pip_cmd = (
        f"pip3 download -r {req_file} --dest {pip_dir} "
        f"--python-version {v_norm} --implementation cp --abi {abi} "
        f"--platform manylinux2014_x86_64 --only-binary :all: 2>/dev/null || true"
    )
    run_on_switch(host, username, password, pip_cmd, timeout=600, check=False)
    
    # Download any missing packages as source (for packages without wheels)
    pip_any_cmd = f"pip3 download -r {req_file} --dest {pip_dir} --no-deps 2>/dev/null || true"
    run_on_switch(host, username, password, pip_any_cmd, timeout=600, check=False)
    
    # Count pip packages and verify they exist
    result = run_on_switch(host, username, password, f"ls -la {pip_dir}/ 2>/dev/null | head -5", check=False)
    if result.stdout:
        print(f"    [DEBUG] pip_dir contents: {result.stdout.strip()[:200]}")
    
    result = run_on_switch(host, username, password, f"ls {pip_dir}/*.whl 2>/dev/null | wc -l")
    wheel_count = int(result.stdout.strip() or "0")
    result = run_on_switch(host, username, password, f"ls {pip_dir}/* 2>/dev/null | wc -l")
    total_count = int(result.stdout.strip() or "0")
    print(f"    ✓ Downloaded {total_count} pip packages ({wheel_count} wheels)")
    
    # Download deb packages
    # Strategy: Use dry-run to get full dependency list, then download each package
    print("    Downloading deb packages...")
    
    # First, update package lists
    print("    Running apt-get update...")
    update_cmd = "sudo apt-get update -q 2>&1 | tail -3"
    run_on_switch(host, username, password, update_cmd, check=False, timeout=120)
    
    # Use dry-run to get the complete list of packages that would be installed
    pkg_list = " ".join(REQUIRED_DEB_PACKAGES)
    print(f"    Resolving dependencies for: {pkg_list}")
    
    # apt-get install --dry-run shows all packages that would be installed
    dryrun_cmd = f"apt-get install --dry-run {pkg_list} 2>&1"
    result = run_on_switch(host, username, password, dryrun_cmd, check=False, timeout=60)
    
    # Parse the dry-run output to extract package names
    # Look for lines like "Inst package-name (version ...)"
    packages_to_download = set()
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Inst "):
            # Format: "Inst package-name (version repo [arch])"
            parts = line.split()
            if len(parts) >= 2:
                pkg_name = parts[1]
                packages_to_download.add(pkg_name)
    
    if not packages_to_download:
        print(f"    ⚠ Could not parse dependencies. Dry-run output:")
        print(f"    {(result.stdout or '')[:500]}")
    else:
        print(f"    Found {len(packages_to_download)} packages to download (including dependencies)")
        
        # Download each package
        downloaded = 0
        failed = []
        
        for pkg in sorted(packages_to_download):
            dl_cmd = f"cd {deb_dir} && sudo apt-get download {pkg} 2>&1"
            dl_result = run_on_switch(host, username, password, dl_cmd, check=False, timeout=60)
            
            # Check if download succeeded (look for .deb file or success message)
            if dl_result.returncode == 0 and "E:" not in (dl_result.stdout or ""):
                downloaded += 1
            else:
                failed.append(pkg)
        
        print(f"    ✓ Downloaded {downloaded}/{len(packages_to_download)} packages")
        if failed and len(failed) <= 5:
            print(f"    ⚠ Failed: {', '.join(failed)}")
    
    # Count deb packages and verify they exist
    result = run_on_switch(host, username, password, f"ls -la {deb_dir}/ 2>/dev/null | head -5", check=False)
    if result.stdout:
        print(f"    [DEBUG] deb_dir contents: {result.stdout.strip()[:200]}")
    
    result = run_on_switch(host, username, password, f"ls {deb_dir}/*.deb 2>/dev/null | wc -l")
    deb_count = int(result.stdout.strip() or "0")
    print(f"    ✓ Downloaded {deb_count} deb packages")
    
    return pip_dir, deb_dir, temp_dir


def extract_requirements(zip_path: Path) -> str:
    """Extract requirements.txt content from cm-lite-daemon.zip."""
    print("\nExtracting requirements.txt from zip...")
    
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for filename in zip_ref.namelist():
            if filename.endswith('requirements.txt'):
                with zip_ref.open(filename) as req_file:
                    content = req_file.read().decode('utf-8')
                    print(f"✓ Found requirements.txt")
                    return content
    
    raise FileNotFoundError("requirements.txt not found in cm-lite-daemon.zip")


def load_requirements(*, requirements_path: Path | None, cm_lite_zip_src: Path | None) -> tuple[str, str]:
    """
    Return (requirements_text, source_description).
    """
    if requirements_path is not None:
        txt = requirements_path.read_text()
        return txt, f"requirements file: {requirements_path}"

    if cm_lite_zip_src is not None and cm_lite_zip_src.exists():
        txt = extract_requirements(cm_lite_zip_src)
        return txt, f"requirements extracted from: {cm_lite_zip_src}"

    # Fall back to default list (compatible with BCM 10.x and 11.x)
    return DEFAULT_REQUIREMENTS, "built-in default requirements (BCM 10.x/11.x compatible)"


def create_tarball(output_path: Path):
    """Create compressed tarball of the entire repository with files."""
    print(f"\nCreating tarball: {output_path}")
    
    def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """Exclude cm-lite-daemon.zip from tarball (use target BCM's version)."""
        n = tarinfo.name.replace("\\", "/")
        if n.endswith("/.files/cm-lite-daemon.zip"):
            return None
        return tarinfo
    
    # Create tarball
    with tarfile.open(output_path, "w:gz") as tar:
        for item in REPO_DIR.iterdir():
            if item.name in ['.git', '.configs', '__pycache__', '.gitignore', '.docs']:
                continue
            if item.name.endswith('.tar.gz'):
                continue
            
            arcname = f"bcm-switch-deploy/{item.name}"
            print(f"  Adding {item.name}...")
            tar.add(item, arcname=arcname, filter=_tar_filter)
    
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n✓ Created tarball: {output_path}")
    print(f"  Size: {size_mb:.2f} MB")
    
    return output_path


def print_summary():
    """Print summary of collected files."""
    print("\n" + "=" * 60)
    print("COLLECTION SUMMARY")
    print("=" * 60)
    
    if FILES_DIR.exists():
        print(f"\nFiles in {FILES_DIR}:")
        
        total_size = 0
        for item in FILES_DIR.iterdir():
            if item.is_file():
                size_kb = item.stat().st_size / 1024
                total_size += item.stat().st_size
                print(f"  - {item.name}: {size_kb:.1f} KB")
            elif item.is_dir():
                dir_size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                total_size += dir_size
                file_count = len(list(item.rglob("*")))
                print(f"  - {item.name}/: {file_count} files, {dir_size/1024:.1f} KB")
        
        print(f"\n  Total: {total_size / (1024*1024):.2f} MB")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare airgapped deployment package for BCM switch deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script downloads all files needed for airgapped deployment from a Cumulus switch:
  1. pip packages for cm-lite-daemon
  2. deb packages (build-essential, python3-dev, etc.)

IMPORTANT: The Cumulus switch used for downloads must:
  - Run the SAME Cumulus version as your production switches
  - Have internet access to download packages

Examples:
  %(prog)s                                    # Interactive - prompts for switch
  %(prog)s --switch 192.168.1.100             # Use specific switch
  %(prog)s --switch localhost                 # Run directly on a Cumulus switch
  %(prog)s --output /tmp/deploy-pkg.tar.gz    # Custom output path

For testing, set up NVIDIA Air with Cumulus switches:
  https://github.com/twilson217/bcm-in-nvidia-air
        """
    )
    
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=REPO_DIR / f"bcm-switches-deploy-airgapped-{datetime.now().strftime('%Y%m%d')}.tar.gz",
        help="Output tarball path (default: bcm-switches-deploy-airgapped-YYYYMMDD.tar.gz)"
    )
    
    parser.add_argument(
        "--switch", "-s",
        type=str,
        default=None,
        help="Hostname or IP of a Cumulus switch with internet access (default: prompt)"
    )
    
    parser.add_argument(
        "--username", "-u",
        type=str,
        default="cumulus",
        help="SSH username for the switch (default: cumulus)"
    )
    
    parser.add_argument(
        "--password", "-p",
        type=str,
        default=None,
        help="SSH password for the switch (default: prompt if needed)"
    )
    
    parser.add_argument(
        "--python3-version",
        type=str,
        default="3.11",
        help="Target switch Python version (default: 3.11)"
    )

    parser.add_argument(
        "--cm-lite-zip",
        type=Path,
        default=None,
        help="Path to cm-lite-daemon.zip (optional)"
    )

    parser.add_argument(
        "--requirements", "-r",
        type=Path,
        default=None,
        help="Path to requirements.txt file (optional)"
    )

    parser.add_argument(
        "--include-cm-lite-zip",
        action="store_true",
        help="Include cm-lite-daemon.zip in tarball (not recommended)"
    )
    
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Non-interactive mode (requires --switch)"
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("BCM Switch Deployment - Airgapped Preparation")
    print("=" * 60)
    
    print("""
This script prepares packages for airgapped cm-lite-daemon deployment.

IMPORTANT: You need access to a Cumulus Linux switch that:
  1. Is running the SAME Cumulus version as your production switches
  2. Has internet access to download packages

The packages will be downloaded ON that switch to ensure compatibility.
""")
    
    # Determine switch to use
    switch_host = args.switch
    username = args.username
    password = args.password
    
    if switch_host is None:
        if args.non_interactive:
            print("Error: --switch is required in non-interactive mode")
            sys.exit(1)
        
        switch_host = input("Enter Cumulus switch hostname or IP [localhost]: ").strip()
        if not switch_host:
            switch_host = "localhost"
    
    # Check if it's a valid Cumulus system
    print(f"\nChecking {switch_host}...")
    
    if switch_host == "localhost":
        is_cumulus, version, error = check_cumulus_localhost()
        if not is_cumulus:
            print(f"✗ {error}")
            print("\nTo use this script on localhost, you must run it on a Cumulus Linux switch.")
            print("Otherwise, specify a remote Cumulus switch with --switch <hostname>")
            sys.exit(1)
        print(f"✓ Cumulus Linux {version} detected")
    else:
        # Need credentials for remote access
        if username == "cumulus" and not args.non_interactive:
            # Prompt for username (show default)
            user_input = input(f"Enter username for {switch_host} [cumulus]: ").strip()
            if user_input:
                username = user_input
        
        if password is None:
            if args.non_interactive:
                print("Error: --password is required for remote switches in non-interactive mode")
                sys.exit(1)
            password = getpass.getpass(f"Enter password for {username}@{switch_host}: ")
        
        is_cumulus, version, error = check_cumulus_remote(switch_host, username, password)
        if not is_cumulus:
            print(f"✗ {error}")
            sys.exit(1)
        print(f"✓ Cumulus Linux {version} detected on {switch_host}")
    
    # Determine requirements
    cm_lite_zip_src = args.cm_lite_zip
    if cm_lite_zip_src is None and CM_LITE_ZIP_PATH.exists():
        cm_lite_zip_src = CM_LITE_ZIP_PATH
    
    if args.requirements is not None and not args.requirements.exists():
        print(f"Error: requirements file not found: {args.requirements}")
        sys.exit(1)
    
    requirements, req_src = load_requirements(
        requirements_path=args.requirements,
        cm_lite_zip_src=cm_lite_zip_src,
    )
    print(f"\nUsing requirements source: {req_src}")
    
    try:
        # Ensure local .files directory exists
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        
        # Download packages on the switch
        pip_dir, deb_dir, temp_dir = download_packages_on_switch(
            switch_host, username, password or "",
            requirements, args.python3_version
        )
        
        # Copy packages back to local .files directory
        print(f"\n  Copying packages to {FILES_DIR}...")
        
        local_pip_dir = FILES_DIR / "pip_packages_dep"
        local_deb_dir = FILES_DIR / "deb_packages"
        local_pip_dir.mkdir(parents=True, exist_ok=True)
        local_deb_dir.mkdir(parents=True, exist_ok=True)
        
        if copy_from_switch(switch_host, username, password or "", f"{pip_dir}/", str(local_pip_dir)):
            pip_count = len(list(local_pip_dir.glob("*")))
            print(f"    ✓ Copied {pip_count} pip packages")
        else:
            print("    ⚠ Failed to copy pip packages")
        
        if copy_from_switch(switch_host, username, password or "", f"{deb_dir}/", str(local_deb_dir)):
            deb_count = len(list(local_deb_dir.glob("*.deb")))
            print(f"    ✓ Copied {deb_count} deb packages")
        else:
            print("    ⚠ Failed to copy deb packages")
        
        # Clean up temp directory on switch
        if switch_host != "localhost":
            run_on_switch(switch_host, username, password or "", f"rm -rf {temp_dir}", check=False)
        
        # Optionally copy cm-lite-daemon.zip
        if args.include_cm_lite_zip and cm_lite_zip_src and cm_lite_zip_src.exists():
            dest = FILES_DIR / "cm-lite-daemon.zip"
            shutil.copy2(cm_lite_zip_src, dest)
            print(f"    ✓ Copied cm-lite-daemon.zip")
        
        # Print summary
        print_summary()
        
        # Create tarball
        tarball = create_tarball(args.output)
        
        print("\n" + "=" * 60)
        print("PREPARATION COMPLETE")
        print("=" * 60)
        print(f"\nAirgapped deployment package created: {tarball}")
        print("\nTo use on an airgapped BCM system:")
        print(f"  1. Transfer {tarball.name} to the target BCM system")
        print("  2. Extract: tar -xzf " + tarball.name)
        print("  3. cd bcm-switch-deploy")
        print("  4. python3 deploy_bcm_switches.py")
        print("\nThe deploy script will use the cached packages from .files/")
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
