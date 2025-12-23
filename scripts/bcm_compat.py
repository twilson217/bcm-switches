#!/usr/bin/env python3
"""
BCM 10.x / 11.x Compatibility Layer
-----------------------------------

This module provides version detection and property name abstraction to support
both BCM 10.x and BCM 11.x systems.

Key differences between BCM 10 and BCM 11 for Cumulus switches:

1. Configuration mode property names:
   - BCM 10: cumulusmode, cumulusfile
   - BCM 11: nvconfigurationmode, nvconfigurationfile

2. Access settings:
   - BCM 10: uses 'force' parameter
   - BCM 11: uses 'Update in ztp' / 'Update in NV'

Usage:
    from bcm_compat import get_bcm_version, BCMProps

    version = get_bcm_version()  # Returns (10, x) or (11, x)
    props = BCMProps(version)
    
    # Use props.config_mode instead of hardcoded "cumulusmode"
    cmsh_cmd = f"set {props.config_mode} file; set {props.config_file} startup.yaml"
"""

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Tuple, Optional


# Full path to cmsh - avoids dependency on 'module load cmsh'
CMSH_PATH = Path("/cm/local/apps/cmd/bin/cmsh")


def get_cmsh_cmd() -> str:
    """
    Return the path to cmsh executable.
    
    Uses full path if available, falls back to 'cmsh' (relies on PATH).
    """
    return str(CMSH_PATH) if CMSH_PATH.exists() else "cmsh"


@lru_cache(maxsize=1)
def get_bcm_version() -> Tuple[int, int]:
    """
    Detect BCM major and minor version.
    
    Returns:
        Tuple of (major, minor), e.g. (10, 25) or (11, 30)
        Falls back to (10, 0) if detection fails.
    
    Detection method:
        Uses `cmsh -c "main; versioninfo"` which outputs lines like:
            Cluster Manager          11.0
    """
    try:
        result = subprocess.run(
            [get_cmsh_cmd(), "-c", "main; versioninfo"],
            capture_output=True,
            text=True,
            timeout=30
        )
        for line in result.stdout.splitlines():
            if "Cluster Manager" in line:
                # Line format: "Cluster Manager          11.0"
                parts = line.split()
                if len(parts) >= 3:
                    version_str = parts[-1]  # e.g., "11.0" or "10.25"
                    version_parts = version_str.split(".")
                    major = int(version_parts[0])
                    minor = int(version_parts[1]) if len(version_parts) > 1 else 0
                    return (major, minor)
    except Exception:
        pass
    
    # Fallback: assume BCM 10 for backward compatibility
    return (10, 0)


def is_bcm_11() -> bool:
    """Return True if this is BCM 11.x or newer."""
    major, _ = get_bcm_version()
    return major >= 11


def is_bcm_10() -> bool:
    """Return True if this is BCM 10.x."""
    major, _ = get_bcm_version()
    return major == 10


class BCMProps:
    """
    BCM version-aware property names for Cumulus switch configuration.
    
    Usage:
        props = BCMProps()  # Auto-detects version
        props = BCMProps(version=(11, 0))  # Explicit version
        
        # Use in cmsh commands:
        f"set {props.config_mode} file"
        f"set {props.config_file} startup.yaml"
    """
    
    def __init__(self, version: Optional[Tuple[int, int]] = None):
        if version is None:
            version = get_bcm_version()
        self.version = version
        self.major = version[0]
        self.minor = version[1]
        self._is_11 = self.major >= 11
    
    @property
    def config_mode(self) -> str:
        """Property name for config mode (cumulusmode vs nvconfigurationmode)."""
        return "nvconfigurationmode" if self._is_11 else "cumulusmode"
    
    @property
    def config_file(self) -> str:
        """Property name for config file (cumulusfile vs nvconfigurationfile)."""
        return "nvconfigurationfile" if self._is_11 else "cumulusfile"
    
    @property
    def config_submode(self) -> str:
        """Submode name for NV configuration (cumulus vs nvconfiguration)."""
        return "nvconfiguration" if self._is_11 else "cumulus"
    
    @property
    def access_force_param(self) -> str:
        """
        Access settings parameter for forcing updates.
        BCM 10: 'force'
        BCM 11: 'updateinztp' (different model, may need different handling)
        """
        return "updateinztp" if self._is_11 else "force"
    
    def version_string(self) -> str:
        """Return version as string, e.g., '11.0' or '10.25'."""
        return f"{self.major}.{self.minor}"
    
    def __repr__(self) -> str:
        return f"BCMProps(version={self.version}, config_mode='{self.config_mode}')"


# Module-level convenience instances
_props: Optional[BCMProps] = None


def get_props() -> BCMProps:
    """Get cached BCMProps instance (auto-detects version on first call)."""
    global _props
    if _props is None:
        _props = BCMProps()
    return _props


# Quick accessors for common use
def config_mode() -> str:
    """Return the config mode property name for this BCM version."""
    return get_props().config_mode


def config_file() -> str:
    """Return the config file property name for this BCM version."""
    return get_props().config_file


if __name__ == "__main__":
    # Quick test / info display
    version = get_bcm_version()
    props = BCMProps(version)
    print(f"BCM Version: {props.version_string()}")
    print(f"  Major: {props.major}")
    print(f"  Is BCM 11+: {props._is_11}")
    print(f"Properties:")
    print(f"  config_mode: {props.config_mode}")
    print(f"  config_file: {props.config_file}")
    print(f"  config_submode: {props.config_submode}")
    print(f"  access_force_param: {props.access_force_param}")

