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
    - Internet access (to download pip packages)
    - pip must be available for downloading packages
    - Either a requirements file (recommended) or access to cm-lite-daemon.zip to extract requirements.txt
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
import re


# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
FILES_DIR = REPO_DIR / ".files"
CM_LITE_ZIP_PATH = Path("/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip")

# Default requirements for BCM 10.x (verified on BCM 10.24.03 and BCM 10.30.0)
DEFAULT_REQUIREMENTS_BCM10 = """\
pyOpenSSL>=21.0.0
websocket-client
pyyaml
psutil
py-cpuinfo
uptime
netifaces
py-dmidecode
requests
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
    abi = f"cp{major}{minor}" if minor < 10 else f"cp{major}{minor}"
    return f"{major}.{minor}", abi


def _extract_missing_pkgs(stderr_text: str) -> list[str]:
    missing: list[str] = []
    if not stderr_text:
        return missing
    low = stderr_text.lower()
    for m in re.finditer(r"no matching distribution found for ([a-z0-9_.-]+)", low):
        missing.append(m.group(1))
    for m in re.finditer(r"satisfies the requirement ([a-z0-9_.-]+)", low):
        missing.append(m.group(1))
    out: list[str] = []
    for x in missing:
        if x not in out:
            out.append(x)
    return out


def check_prerequisites(*, need_requirements_source: bool):
    """Check that all prerequisites are met."""
    print("Checking prerequisites...")
    
    errors = []
    
    # Check for pip
    if not shutil.which("pip") and not shutil.which("pip3"):
        errors.append("pip/pip3 is not installed or not in PATH")
    
    # Check requirements source (either requirements.txt provided, or cm-lite-daemon.zip available)
    if need_requirements_source:
        errors.append("requirements source not available (provide --requirements, or --cm-lite-zip, or rely on BCM10 default requirements)")
    
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


def copy_cm_lite_daemon(src_zip: Path):
    """Copy cm-lite-daemon.zip to files directory."""
    print("\nCopying cm-lite-daemon.zip...")
    
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FILES_DIR / "cm-lite-daemon.zip"
    
    shutil.copy2(src_zip, dest)
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

    # Fall back to BCM10 default list
    return DEFAULT_REQUIREMENTS_BCM10, "built-in BCM 10.x default requirements"


def download_pip_packages(requirements: str, python3_version: str):
    """Download pip packages for offline installation."""
    v_norm, abi = _python_version_to_tags(python3_version)
    print(f"\nDownloading pip packages (target python {v_norm}, ABI {abi})...")
    
    pip_dir = FILES_DIR / "pip_packages_dep"
    pip_dir.mkdir(parents=True, exist_ok=True)
    
    # Write temporary requirements file
    temp_req = FILES_DIR / "requirements.txt"
    temp_req.write_text(requirements)
    
    try:
        # Wheelhouse strategy (aligned with deploy_bcm_switches.py):
        # - Prefer wheels for offline install, targeted at the switch python ABI + platform.
        # - If pip reports "no matching distribution" for a package under those constraints,
        #   automatically allow it as sdist and retry.
        # - Always include sdist-only packages like uptime.
        sdist_allowlist = {"uptime"}

        def _pkg_name_from_req_line(line: str) -> str | None:
            s = (line or "").strip()
            if not s or s.startswith("#"):
                return None
            return s.split("==", 1)[0].split("[", 1)[0].strip()

        def _write_filtered_requirements() -> Path:
            filtered_lines = []
            for line in requirements.splitlines():
                pkg = _pkg_name_from_req_line(line)
                if pkg and pkg in sdist_allowlist:
                    continue
                filtered_lines.append(line)
            filtered_req = FILES_DIR / "requirements.filtered.txt"
            filtered_req.write_text("\n".join(filtered_lines).strip() + "\n")
            return filtered_req

        # Retry wheel download a few times, expanding sdist allowlist based on pip error output
        attempt = 0
        last = None
        while attempt < 3:
            attempt += 1
            filtered_req = _write_filtered_requirements()
            cmd = [
                "pip", "download",
                "-r", str(filtered_req),
                "--dest", str(pip_dir),
                "--python-version", v_norm,
                "--implementation", "cp",
                "--abi", abi,
                "--platform", "manylinux2014_x86_64",
                "--only-binary", ":all:",
                "--no-binary", ":none:",
            ]
            last = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if last.returncode == 0:
                break
            missing = _extract_missing_pkgs(last.stderr or "")
            added = False
            for pkg in missing:
                if pkg and pkg not in sdist_allowlist:
                    sdist_allowlist.add(pkg)
                    added = True
            if not added:
                break

        # Download sdists for allowlisted packages
        for pkg in sorted(sdist_allowlist):
            print(f"  Downloading sdist for '{pkg}'...")
            sdist_cmd = ["pip", "download", "--no-binary", ":all:", "--no-deps", "--dest", str(pip_dir), pkg]
            subprocess.run(sdist_cmd, capture_output=True, text=True, timeout=300)

        packages = list(pip_dir.glob("*"))
        wheel_count = len(list(pip_dir.glob("*.whl")))
        if not packages or wheel_count == 0:
            print("  ✗ Failed to download required pip packages for offline install")
            print(f"    Downloaded files: {len(packages)} (wheels: {wheel_count})")
            if last is not None and last.returncode != 0:
                if last.stderr:
                    print(f"    pip stderr (first 500 chars): {last.stderr[:500]}")
                if last.stdout:
                    print(f"    pip stdout (first 500 chars): {last.stdout[:500]}")
            raise RuntimeError("pip package download failed")
        
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
            tar.add(item, arcname=arcname, filter=_tar_filter)
    
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


def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """
    Exclude cm-lite-daemon.zip from the tarball by default.
    In airgapped production, deploy_bcm_switches.py should use the target BCM's own
    cm-lite-daemon.zip (production version), not a potentially different one bundled
    from another system.
    """
    # tarinfo.name is the archive name (we use bcm-switch-deploy/<...>)
    n = tarinfo.name.replace("\\", "/")
    if n.endswith("/.files/cm-lite-daemon.zip"):
        return None
    return tarinfo


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
        "--python3-version",
        type=str,
        default="3.11",
        help="Target switch Python version (MAJOR.MINOR), e.g. 3.11. "
             "This is used to download compatible wheels for offline install. "
             "If omitted, defaults to 3.11."
    )

    parser.add_argument(
        "--cm-lite-zip",
        type=Path,
        default=None,
        help="Path to cm-lite-daemon.zip. If omitted, we try the BCM default path "
             f"({CM_LITE_ZIP_PATH}) then .files/cm-lite-daemon.zip."
    )

    parser.add_argument(
        "--include-cm-lite-zip",
        action="store_true",
        help="Include cm-lite-daemon.zip in the tarball (NOT recommended; prefer using the target BCM's production zip)."
    )

    parser.add_argument(
        "--requirements", "-r",
        type=Path,
        default=None,
        help="Path to a requirements.txt file (contents from inside cm-lite-daemon.zip). "
             "Useful when you cannot move the zip file but can copy the text file."
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("BCM Switch Deployment - Airgapped Preparation")
    print("=" * 60)
    
    # Determine cm-lite-daemon.zip source (optional; only used if we need to extract requirements)
    cm_lite_zip_src = args.cm_lite_zip
    if cm_lite_zip_src is None:
        if CM_LITE_ZIP_PATH.exists():
            cm_lite_zip_src = CM_LITE_ZIP_PATH

    # Validate requirements path if provided
    if args.requirements is not None and not args.requirements.exists():
        print(f"Error: requirements file not found: {args.requirements}")
        sys.exit(1)

    # Check prerequisites (pip + internet). Requirements source is optional because we have a BCM10 default.
    if not check_prerequisites(need_requirements_source=False):
        sys.exit(1)
    
    try:
        # Step 1: Determine requirements and download packages (always)
        requirements, req_src = load_requirements(
            requirements_path=args.requirements,
            cm_lite_zip_src=cm_lite_zip_src,
        )
        print(f"\nUsing requirements source: {req_src}")
        download_pip_packages(requirements, python3_version=args.python3_version)

        # Optionally copy cm-lite-daemon.zip into .files (not included in tarball unless requested)
        if args.include_cm_lite_zip:
            if not cm_lite_zip_src:
                raise FileNotFoundError("Cannot include cm-lite-daemon.zip: source not found. Provide --cm-lite-zip.")
            copy_cm_lite_daemon(cm_lite_zip_src)
        
        # Print summary of collected files
        print_summary()
        
        # Step 2: Create tarball
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

