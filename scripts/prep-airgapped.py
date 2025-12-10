#!/usr/bin/env python3
"""
Airgapped Deployment Preparation Script

This script prepares a self-contained deployment package for airgapped BCM systems.
It collects all required external files and creates a tarball that can be transferred
to systems without internet access.

Usage:
    python3 prep-airgapped.py
    python3 prep-airgapped.py --output /path/to/output.tar.gz

Requirements:
    - Must run on a BCM system with internet access
    - pip must be available for downloading packages
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime


# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
FILES_DIR = REPO_DIR / ".files"
CM_LITE_ZIP_PATH = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")


def check_prerequisites():
    """Check that all prerequisites are met."""
    print("Checking prerequisites...")
    
    errors = []
    
    # Check for pip
    if not shutil.which("pip") and not shutil.which("pip3"):
        errors.append("pip/pip3 is not installed or not in PATH")
    
    # Check for cm-lite-daemon.zip
    if not CM_LITE_ZIP_PATH.exists():
        errors.append(f"cm-lite-daemon.zip not found at {CM_LITE_ZIP_PATH}")
    
    # Check internet connectivity
    try:
        result = subprocess.run(
            ["pip", "index", "versions", "requests"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            # Try alternative check
            result = subprocess.run(
                ["pip", "search", "requests"],
                capture_output=True, text=True, timeout=30
            )
    except subprocess.TimeoutExpired:
        errors.append("Network timeout - check internet connectivity")
    except Exception:
        pass  # Some pip versions don't support these commands
    
    if errors:
        print("\n✗ Prerequisites check failed:")
        for error in errors:
            print(f"  - {error}")
        return False
    
    print("✓ All prerequisites met")
    return True


def copy_cm_lite_daemon():
    """Copy cm-lite-daemon.zip to files directory."""
    print("\nCopying cm-lite-daemon.zip...")
    
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FILES_DIR / "cm-lite-daemon.zip"
    
    shutil.copy2(CM_LITE_ZIP_PATH, dest)
    print(f"✓ Copied to {dest}")
    
    return dest


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


def download_pip_packages(requirements: str):
    """Download pip packages for offline installation."""
    print("\nDownloading pip packages...")
    
    pip_dir = FILES_DIR / "pip_packages_dep"
    pip_dir.mkdir(parents=True, exist_ok=True)
    
    # Write temporary requirements file
    temp_req = FILES_DIR / "requirements.txt"
    temp_req.write_text(requirements)
    
    try:
        # Download packages for multiple Python versions to ensure compatibility
        python_versions = ["3.11", "3.10", "3.9"]
        
        for py_version in python_versions:
            print(f"  Downloading for Python {py_version}...")
            cmd = [
                "pip", "download",
                "--python-version", py_version,
                "-r", str(temp_req),
                "--dest", str(pip_dir),
                "--no-deps"
            ]
            
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    print(f"  ✓ Python {py_version} packages downloaded")
                else:
                    print(f"  ⚠ Some packages may not be available for Python {py_version}")
            except subprocess.TimeoutExpired:
                print(f"  ⚠ Timeout downloading Python {py_version} packages")
        
        # Also download with dependencies for completeness
        print("  Downloading with dependencies...")
        cmd = [
            "pip", "download",
            "-r", str(temp_req),
            "--dest", str(pip_dir)
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        # List downloaded packages
        packages = list(pip_dir.glob("*"))
        print(f"\n✓ Downloaded {len(packages)} package files:")
        for pkg in sorted(packages)[:10]:
            print(f"    - {pkg.name}")
        if len(packages) > 10:
            print(f"    ... and {len(packages) - 10} more")
        
    finally:
        # Clean up temp requirements
        if temp_req.exists():
            temp_req.unlink()
    
    return pip_dir


def create_tarball(output_path: Path):
    """Create compressed tarball of the entire repository with files."""
    print(f"\nCreating tarball: {output_path}")
    
    # Create tarball
    with tarfile.open(output_path, "w:gz") as tar:
        # Add all files from repo, excluding certain directories
        for item in REPO_DIR.iterdir():
            # Skip items we don't want in the tarball
            if item.name in ['.git', '.configs', '__pycache__', '.gitignore']:
                continue
            if item.name.endswith('.tar.gz'):
                continue
            
            arcname = f"bcm-switch-deploy/{item.name}"
            print(f"  Adding {item.name}...")
            tar.add(item, arcname=arcname)
    
    # Get file size
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n✓ Created tarball: {output_path}")
    print(f"  Size: {size_mb:.2f} MB")
    
    return output_path


def print_summary():
    """Print summary of collected files."""
    print("\n" + "=" * 60)
    print("COLLECTION SUMMARY")
    print("=" * 60)
    
    # Check files directory
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
This script collects all files needed for airgapped deployment:
  1. cm-lite-daemon.zip from BCM
  2. All required pip packages

The output tarball can be transferred to an airgapped BCM system
and extracted for use with deploy_bcm_switches.py --airgapped.

Examples:
  %(prog)s                                    # Create default tarball
  %(prog)s --output /tmp/deploy-package.tar.gz  # Custom output path
        """
    )
    
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=REPO_DIR / f"bcm-switches-deploy-airgapped-{datetime.now().strftime('%Y%m%d')}.tar.gz",
        help="Output tarball path (default: bcm-switches-deploy-airgapped-YYYYMMDD.tar.gz)"
    )
    
    parser.add_argument(
        "--skip-packages",
        action="store_true",
        help="Skip downloading pip packages (if already present)"
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("BCM Switch Deployment - Airgapped Preparation")
    print("=" * 60)
    
    # Check prerequisites
    if not check_prerequisites():
        sys.exit(1)
    
    try:
        # Step 1: Copy cm-lite-daemon.zip
        zip_path = copy_cm_lite_daemon()
        
        # Step 2: Extract requirements and download packages
        if not args.skip_packages:
            requirements = extract_requirements(zip_path)
            download_pip_packages(requirements)
        else:
            print("\nSkipping pip package download (--skip-packages)")
            if not (FILES_DIR / "pip_packages_dep").exists():
                print("⚠ Warning: pip_packages_dep directory does not exist")
        
        # Print summary of collected files
        print_summary()
        
        # Step 3: Create tarball
        tarball = create_tarball(args.output)
        
        print("\n" + "=" * 60)
        print("PREPARATION COMPLETE")
        print("=" * 60)
        print(f"\nAirgapped deployment package created: {tarball}")
        print("\nTo use on an airgapped system:")
        print(f"  1. Transfer {tarball.name} to the target BCM system")
        print("  2. Extract: tar -xzf " + tarball.name)
        print("  3. cd bcm-switch-deploy")
        print("  4. python3 deploy_bcm_switches.py --airgapped")
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

