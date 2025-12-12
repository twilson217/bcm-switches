#!/usr/bin/env python3
"""
Automated Test Loop for BCM Switch Deployment

Runs the complete test cycle: reset → setup → deploy → validate

Test 1: Full Deployment from DHCP Leases
  - Generate CSV from DHCP leases
  - Change default passwords
  - Map hostnames from topology
  - Deploy using --csv option
  - Validate deployment

Test 2: Deployment with Switch Setup First
  - Generate CSV from DHCP leases
  - Map hostnames from topology
  - Change passwords AND set hostnames on switches
  - Deploy using --csv option (switches already have correct hostnames)
  - Validate deployment

Usage:
    ./test-loop.py              # Run all tests
    ./test-loop.py --test1      # Run only Test 1
    ./test-loop.py --test2      # Run only Test 2
    ./test-loop.py --dry-run    # Show what would be done
    ./test-loop.py --no-reset   # Skip simulation reset
    ./test-loop.py --verbose    # Show detailed output
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
CONFIG_DIR = REPO_DIR / ".configs"
TOPOLOGY_FILE = SCRIPT_DIR / "sample-configs" / "test-topology.json"

# CSV file paths
FROM_DHCP_CSV = CONFIG_DIR / "from-dhcp.csv"

# Switches we're testing with (excludes oob-mgmt-switch)
TEST_SWITCHES = ["spine-01", "spine-02", "leaf-01", "leaf-02", "leaf-03", "leaf-04"]

# Wait times
DHCP_WAIT_SECONDS = 90  # Wait for switches to get DHCP after reset
BOOT_STABILIZE_SECONDS = 30  # Extra wait for boot to stabilize

# Default test password (can be overridden via --password)
DEFAULT_TEST_PASSWORD = "Nvidia1234!"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class StepResult:
    """Result of a test step."""
    name: str
    success: bool
    duration: float
    message: str
    output: str = ""


@dataclass
class TestResult:
    """Result of a complete test."""
    name: str
    success: bool
    steps: List[StepResult] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    duration: float = 0.0


# ============================================================================
# Utility Functions
# ============================================================================

def run_cmd(cmd: str, cwd: Path = None, timeout: int = 600, 
            capture: bool = True, input_text: str = None) -> Tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd or REPO_DIR,
            capture_output=capture, text=True, 
            timeout=timeout, input=input_text
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def run_script(script_path: str, args: str = "", timeout: int = 600,
               input_text: str = None, verbose: bool = False) -> StepResult:
    """Run a Python script and return the result."""
    start = time.time()
    
    # Build command
    if script_path.startswith("./"):
        script_path = script_path[2:]
    full_path = REPO_DIR / script_path
    
    cmd = f"python3 {full_path}"
    if args:
        cmd += f" {args}"
    
    if verbose:
        print(f"    Running: {cmd}")
    
    rc, stdout, stderr = run_cmd(cmd, timeout=timeout, input_text=input_text)
    duration = time.time() - start
    
    success = rc == 0
    output = stdout + stderr
    
    if verbose and output:
        for line in output.split('\n')[:20]:
            print(f"      {line}")
        if len(output.split('\n')) > 20:
            print(f"      ... ({len(output.split(chr(10))) - 20} more lines)")
    
    message = "Success" if success else f"Failed (exit code {rc})"
    
    return StepResult(
        name=script_path.split('/')[-1],
        success=success,
        duration=duration,
        message=message,
        output=output
    )


def wait_with_countdown(seconds: int, message: str):
    """Wait with a countdown display."""
    print(f"  {message}")
    for remaining in range(seconds, 0, -10):
        print(f"    Waiting... {remaining}s remaining", end="\r")
        time.sleep(min(10, remaining))
    print(f"    Waiting... done!                    ")


def filter_csv_remove_oob_switch(csv_path: Path) -> bool:
    """Remove oob-mgmt-switch from CSV file."""
    try:
        rows = []
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                hostname = row.get('Hostname', '') or row.get('hostname', '')
                if 'oob' not in hostname.lower():
                    rows.append(row)
        
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        
        return True
    except Exception as e:
        print(f"    Error filtering CSV: {e}")
        return False


def count_devices_in_csv(csv_path: Path) -> int:
    """Count devices in a CSV file."""
    try:
        with open(csv_path, 'r') as f:
            return sum(1 for _ in csv.DictReader(f))
    except:
        return 0


def clear_config():
    """Clear the config.json to start fresh."""
    config_file = CONFIG_DIR / "config.json"
    if config_file.exists():
        try:
            config_file.unlink()
            return True
        except:
            pass
    return True


def get_validation_summary(output: str) -> Tuple[int, int]:
    """Parse validation output to get passed/failed counts."""
    passed = 0
    failed = 0
    for line in output.split('\n'):
        if 'Passed:' in line and 'switches' not in line.lower():
            try:
                passed = int(line.split(':')[1].strip())
            except:
                pass
        elif 'Failed:' in line and 'switches' not in line.lower():
            try:
                failed = int(line.split(':')[1].strip())
            except:
                pass
    return passed, failed


# ============================================================================
# Test Implementations
# ============================================================================

class TestRunner:
    """Runs automated tests."""
    
    def __init__(self, verbose: bool = False, dry_run: bool = False, 
                 password: str = DEFAULT_TEST_PASSWORD):
        self.verbose = verbose
        self.dry_run = dry_run
        self.password = password
        self.results: List[TestResult] = []
    
    def run_step(self, name: str, script: str, args: str = "", 
                 timeout: int = 600, input_text: str = None) -> StepResult:
        """Run a single test step."""
        print(f"\n  Step: {name}")
        
        if self.dry_run:
            print(f"    [DRY RUN] Would run: {script} {args}")
            return StepResult(name=name, success=True, duration=0, 
                            message="[DRY RUN] Skipped")
        
        result = run_script(script, args, timeout, input_text, self.verbose)
        
        status = "✓" if result.success else "✗"
        print(f"    {status} {result.message} ({result.duration:.1f}s)")
        
        return result
    
    def reset_simulation(self) -> StepResult:
        """Reset the NVIDIA Air simulation and clean BCM."""
        print("\n  Step: Reset Simulation")
        
        if self.dry_run:
            print("    [DRY RUN] Would reset simulation")
            return StepResult(name="reset", success=True, duration=0,
                            message="[DRY RUN] Skipped")
        
        start = time.time()
        
        # Run test-sim-reset.py
        result = run_script(
            "scripts/tests/test-sim-reset.py", 
            "", 
            timeout=400,  # 6+ minutes for rebuild
            verbose=self.verbose
        )
        
        if not result.success:
            return result
        
        # Wait for switches to boot and get DHCP
        wait_with_countdown(DHCP_WAIT_SECONDS, "Waiting for switches to get DHCP addresses...")
        wait_with_countdown(BOOT_STABILIZE_SECONDS, "Waiting for boot stabilization...")
        
        result.duration = time.time() - start
        return result
    
    def generate_csv_from_dhcp(self) -> StepResult:
        """Generate CSV from DHCP leases."""
        result = self.run_step(
            "Generate CSV from DHCP",
            "scripts/csv-from-dhcp.py",
            ""
        )
        
        if result.success and not self.dry_run:
            # Filter out oob-mgmt-switch
            print("    Filtering out oob-mgmt-switch...")
            if filter_csv_remove_oob_switch(FROM_DHCP_CSV):
                count = count_devices_in_csv(FROM_DHCP_CSV)
                print(f"    ✓ CSV has {count} devices")
            else:
                result.success = False
                result.message = "Failed to filter CSV"
        
        return result
    
    def map_hostnames(self) -> StepResult:
        """Map hostnames from topology file."""
        return self.run_step(
            "Map hostnames from topology",
            "scripts/map-csv-topology.py",
            f"--csv {FROM_DHCP_CSV} --topology {TOPOLOGY_FILE}"
        )
    
    def change_passwords(self) -> StepResult:
        """Change default passwords on switches."""
        # Need to provide password input
        # The script prompts for: new password, confirm password
        input_text = f"{self.password}\n{self.password}\n"
        
        return self.run_step(
            "Change default passwords",
            "scripts/change-switch-defaults.py",
            f"--csv {FROM_DHCP_CSV} --password",
            timeout=300,
            input_text=input_text
        )
    
    def change_passwords_and_hostnames(self) -> StepResult:
        """Change passwords AND set hostnames on switches."""
        input_text = f"{self.password}\n{self.password}\n"
        
        return self.run_step(
            "Change passwords and set hostnames",
            "scripts/change-switch-defaults.py",
            f"--csv {FROM_DHCP_CSV} --password --hostname",
            timeout=300,
            input_text=input_text
        )
    
    def deploy_switches(self) -> StepResult:
        """Deploy switches to BCM."""
        # The deploy script will prompt for:
        # - username (use cumulus)
        # - password
        # - network confirmation (y)
        # - VRF (just press enter for default)
        # - proceed prompts (y)
        
        # Since we're using --csv, it skips IP prompts
        # We need to handle: username, password, network, vrf, proceed prompts
        input_text = f"cumulus\n{self.password}\ny\n\ny\ny\ny\ny\n"
        
        return self.run_step(
            "Deploy to BCM",
            "deploy_bcm_switches.py",
            f"--csv {FROM_DHCP_CSV}",
            timeout=900,  # 15 minutes for full deployment
            input_text=input_text
        )
    
    def validate_deployment(self) -> StepResult:
        """Run validation testing."""
        result = self.run_step(
            "Validate deployment",
            "scripts/validation-testing.py",
            f"--password {self.password}",
            timeout=120
        )
        
        if not self.dry_run:
            passed, failed = get_validation_summary(result.output)
            print(f"    Validation: {passed} passed, {failed} failed")
        
        return result
    
    def run_test_1(self, skip_reset: bool = False) -> TestResult:
        """
        Test 1: Full Deployment from DHCP Leases
        
        Steps:
        1. Reset simulation
        2. Generate CSV from DHCP
        3. Change default passwords
        4. Map hostnames from topology
        5. Deploy using --csv
        6. Validate deployment
        """
        print("\n" + "=" * 70)
        print("TEST 1: Full Deployment from DHCP Leases")
        print("=" * 70)
        
        test = TestResult(
            name="Test 1: Full Deployment from DHCP Leases",
            success=True,
            start_time=datetime.now().isoformat()
        )
        
        start = time.time()
        
        # Clear any existing config
        if not self.dry_run:
            clear_config()
        
        steps = []
        
        # Step 1: Reset simulation
        if not skip_reset:
            step = self.reset_simulation()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test
        
        # Step 2: Generate CSV from DHCP
        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 3: Change default passwords
        step = self.change_passwords()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 4: Map hostnames
        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 5: Deploy
        step = self.deploy_switches()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 6: Validate (even if deploy failed, to see what state we're in)
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test
    
    def run_test_2(self, skip_reset: bool = False) -> TestResult:
        """
        Test 2: Deployment with Switch Setup First
        
        Steps:
        1. Reset simulation
        2. Generate CSV from DHCP
        3. Map hostnames from topology
        4. Change passwords AND set hostnames on switches
        5. Deploy using --csv
        6. Validate deployment
        """
        print("\n" + "=" * 70)
        print("TEST 2: Deployment with Switch Setup First")
        print("=" * 70)
        
        test = TestResult(
            name="Test 2: Deployment with Switch Setup First",
            success=True,
            start_time=datetime.now().isoformat()
        )
        
        start = time.time()
        
        # Clear any existing config
        if not self.dry_run:
            clear_config()
        
        steps = []
        
        # Step 1: Reset simulation
        if not skip_reset:
            step = self.reset_simulation()
            steps.append(step)
            if not step.success:
                test.success = False
                test.steps = steps
                test.end_time = datetime.now().isoformat()
                test.duration = time.time() - start
                return test
        
        # Step 2: Generate CSV from DHCP
        step = self.generate_csv_from_dhcp()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 3: Map hostnames (before changing on switches)
        step = self.map_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 4: Change passwords AND set hostnames
        step = self.change_passwords_and_hostnames()
        steps.append(step)
        if not step.success:
            test.success = False
            test.steps = steps
            test.end_time = datetime.now().isoformat()
            test.duration = time.time() - start
            return test
        
        # Step 5: Deploy
        step = self.deploy_switches()
        steps.append(step)
        if not step.success:
            test.success = False
        
        # Step 6: Validate
        step = self.validate_deployment()
        steps.append(step)
        if not step.success:
            test.success = False
        
        test.steps = steps
        test.end_time = datetime.now().isoformat()
        test.duration = time.time() - start
        
        return test
    
    def print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        
        all_passed = True
        
        for test in self.results:
            status = "✓ PASSED" if test.success else "✗ FAILED"
            print(f"\n{test.name}")
            print(f"  Status: {status}")
            print(f"  Duration: {test.duration:.1f}s")
            print(f"  Steps:")
            
            for step in test.steps:
                step_status = "✓" if step.success else "✗"
                print(f"    {step_status} {step.name}: {step.message} ({step.duration:.1f}s)")
            
            if not test.success:
                all_passed = False
        
        print("\n" + "-" * 70)
        if all_passed:
            print("OVERALL: ✓ ALL TESTS PASSED")
        else:
            passed = sum(1 for t in self.results if t.success)
            total = len(self.results)
            print(f"OVERALL: ✗ {passed}/{total} TESTS PASSED")
        print("=" * 70)
        
        return all_passed


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Automated test loop for BCM switch deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tests Available:
  Test 1: Full Deployment from DHCP Leases
    - CSV generated from DHCP, passwords changed, hostnames mapped, deploy
  
  Test 2: Deployment with Switch Setup First  
    - Hostnames set on switches before deploy, then deploy discovers them

Examples:
  %(prog)s                    # Run all tests
  %(prog)s --test1            # Run only Test 1
  %(prog)s --test2            # Run only Test 2
  %(prog)s --test1 --no-reset # Run Test 1 without resetting simulation
  %(prog)s --dry-run          # Show what would be done

Prerequisites:
  - scripts/tests/.env configured with NVIDIA Air credentials
  - NVIDIA Air simulation running
  - BCM head node with network access to switches
        """
    )
    
    parser.add_argument("--test1", action="store_true",
                       help="Run only Test 1 (DHCP lease deployment)")
    parser.add_argument("--test2", action="store_true",
                       help="Run only Test 2 (switch setup first)")
    parser.add_argument("--no-reset", action="store_true",
                       help="Skip simulation reset (use existing state)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without running")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed output from each step")
    parser.add_argument("--password", type=str, default=DEFAULT_TEST_PASSWORD,
                       help=f"Password to set on switches (default: {DEFAULT_TEST_PASSWORD})")
    
    args = parser.parse_args()
    
    # Determine which tests to run
    run_test1 = args.test1 or (not args.test1 and not args.test2)
    run_test2 = args.test2 or (not args.test1 and not args.test2)
    
    # Password is passed to TestRunner
    test_password = args.password
    
    print("=" * 70)
    print("BCM SWITCH DEPLOYMENT - AUTOMATED TEST LOOP")
    print("=" * 70)
    print(f"Start time: {datetime.now().isoformat()}")
    print(f"Tests to run: ", end="")
    if run_test1 and run_test2:
        print("Test 1, Test 2")
    elif run_test1:
        print("Test 1 only")
    else:
        print("Test 2 only")
    print(f"Skip reset: {args.no_reset}")
    print(f"Dry run: {args.dry_run}")
    
    # Check prerequisites
    if not args.dry_run:
        env_file = SCRIPT_DIR / ".env"
        if not env_file.exists():
            print(f"\nError: {env_file} not found")
            print("Please copy sample-configs/sample.env to .env and configure it.")
            sys.exit(1)
    
    # Run tests
    runner = TestRunner(verbose=args.verbose, dry_run=args.dry_run, password=test_password)
    
    try:
        if run_test1:
            result = runner.run_test_1(skip_reset=args.no_reset)
            runner.results.append(result)
            
            # If running both tests, reset between them
            if run_test2 and not args.no_reset:
                print("\n" + "-" * 70)
                print("Preparing for Test 2...")
        
        if run_test2:
            # For Test 2, always reset if we ran Test 1 (unless --no-reset)
            skip_reset = args.no_reset
            result = runner.run_test_2(skip_reset=skip_reset)
            runner.results.append(result)
        
        # Print summary
        all_passed = runner.print_summary()
        
        # Exit code
        sys.exit(0 if all_passed else 1)
        
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        runner.print_summary()
        sys.exit(130)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

