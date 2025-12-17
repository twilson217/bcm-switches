# Manual BCM Switch Onboarding + cm-lite-daemon Install (No Automation)

This document describes how to **manually** add Cumulus Linux switches to **BCM 10.x** and manually install/register **cm-lite-daemon**, **without using any automation/scripts from this repo**.

## Assumptions / Inputs

- **BCM**: BCM 10.x system with access to `cmsh`.
- **Switch OS**: Cumulus Linux on NVIDIA switches.
- **Switch access**: SSH access to each switch (usually `cumulus` user) and ability to use `sudo`.
- **Management VRF**: Most modern Cumulus images place management on **VRF `mgmt`** (e.g., `eth0` in `mgmt`). Your environment might use the **default VRF**.

For each switch, you need:
- **Hostname** (desired BCM hostname, e.g. `leaf-01`)
- **Mgmt IP** (e.g. `192.168.200.163`)
- **Mgmt MAC** (MAC of the mgmt interface used to reach BCM; often `eth0`)
- **BCM network name** (e.g. `internalnet`)
- **SSH username/password** for the switch (after you set it)
- **Switch python3 minor** (e.g. `3.11`)

## Step 0 — Decide VRF and Python version (per switch)

On the switch:

```bash
# Identify where the mgmt IP lives (VRF)
nv show int eth0 | grep vrf

# Determine python3 MAJOR.MINOR
python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
```

Record the VRF to use for daemon registration (commonly `mgmt`) and the python version (commonly `3.11` on newer images).

## Step 1 — Switch-side prep (recommended)

On each switch, ensure:
- Password is set to the intended operational password
- Hostname is set (optional but recommended before BCM registration)
- ZTP is disabled (to prevent unexpected changes/reboots)

Example (adjust as needed):

```bash
sudo hostnamectl set-hostname leaf-01

# Disable ZTP (method varies by image)
sudo ztp --disable || true
sudo systemctl disable --now ztp.service 2>/dev/null || true
```

Re-login after changing hostname/password if required.

## Step 2 — Add switches to BCM (cmsh)

On the BCM head node, for each switch:

```bash
cmsh
device
add switch leaf-01
use leaf-01
set ip 192.168.200.163
set mac 44:38:39:00:01:10
set network internalnet
set hasclientdaemon yes

# BCM needs credentials to reach the switch
accesssettings
set username cumulus
set password '<SWITCH_PASSWORD>'
set -e force true
commit

# Optional: set monitoring-only intent (recommended for safety)
set cumulusmode manual
ztpsettings
set runztponeachboot no
set enableapi yes
commit

# Trigger initialization (helps bootstrap material generation)
initialize

quit
quit
```

### Bootstrap certificates

BCM generates bootstrap materials per device under:

```bash
/cm/local/apps/cmd/etc/htdocs/switch/<hostname>/
```

You should see:
- `bootstrap.key`
- `bootstrap.pem`

Example:

```bash
ls -l /cm/local/apps/cmd/etc/htdocs/switch/leaf-01/bootstrap.*
```

## Step 3 — Get the production `cm-lite-daemon.zip`

On the **target production BCM**, locate the daemon zip:

```bash
ls -l /cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip
```

**Important:** For production, you typically want the daemon zip from the **same production BCM version** you are deploying against.

## Step 4 — Prepare offline pip packages (on an internet-connected host)

If the target BCM is truly airgapped, prepare the python dependencies on **any host with internet** (laptop/WSL is fine), using the **switch’s python version**.

### 4.1 Get the requirements list

Preferred: extract `requirements.txt` from the same `cm-lite-daemon.zip` you will use in production.

If you can only copy text out of a restricted environment, use the requirements content directly.

BCM 10.x requirements commonly include:

```
pyOpenSSL>=21.0.0
websocket-client
pyyaml
psutil
py-cpuinfo
uptime
netifaces
py-dmidecode
requests
```

### 4.2 Download wheels/sdists for the switch python version

Create a wheelhouse directory:

```bash
mkdir -p pip_packages_dep
```

Run `pip download` targeting the switch platform/ABI (example for python 3.11):

```bash
pip download \
  -r requirements.txt \
  --dest pip_packages_dep \
  --python-version 3.11 \
  --implementation cp \
  --abi cp311 \
  --platform manylinux2014_x86_64 \
  --only-binary :all:
```

If `pip` reports **“No matching distribution found”** for a package (common for `netifaces` / `uptime`), download those as sdists:

```bash
pip download --no-binary :all: --no-deps --dest pip_packages_dep netifaces uptime
```

Repeat with the correct `--python-version/--abi` for your switches (e.g. 3.10 → `--python-version 3.10 --abi cp310`).

## Step 5 — Copy artifacts to each switch

From BCM (or your staging host), copy:
- `cm-lite-daemon.zip`
- `pip_packages_dep/` directory
- `bootstrap.key` and `bootstrap.pem`

Example:

```bash
scp /cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip cumulus@192.168.200.163:/home/cumulus/
scp -r ./pip_packages_dep cumulus@192.168.200.163:/home/cumulus/
scp /cm/local/apps/cmd/etc/htdocs/switch/leaf-01/bootstrap.* cumulus@192.168.200.163:/home/cumulus/
```

## Step 6 — Install and register on the switch

SSH to the switch:

```bash
ssh cumulus@192.168.200.163
```

Install prerequisites:

```bash
sudo apt-get update
sudo apt-get install -y unzip python3-pip build-essential python3-dev
```

Extract and install daemon:

```bash
unzip -q cm-lite-daemon.zip
sudo mkdir -p /opt/cm-lite-daemon
sudo cp -r ./cm-lite-daemon/* /opt/cm-lite-daemon/
sudo chown -R root:root /opt/cm-lite-daemon
```

Install bootstrap certs:

```bash
sudo mkdir -p /opt/cm-lite-daemon/etc
sudo cp ./bootstrap.key ./bootstrap.pem /opt/cm-lite-daemon/etc/
sudo chown root:root /opt/cm-lite-daemon/etc/bootstrap.*
sudo chmod 600 /opt/cm-lite-daemon/etc/bootstrap.key
sudo chmod 644 /opt/cm-lite-daemon/etc/bootstrap.pem
```

Install python deps from the offline wheelhouse:

```bash
sudo pip3 install --break-system-packages --no-index \
  --find-links /home/cumulus/pip_packages_dep \
  -r /opt/cm-lite-daemon/requirements.txt
```

Register to BCM (choose the correct VRF):

```bash
BCM_IP="<BCM_MASTER_IP>"
VRF="mgmt"   # or "default" in some labs
cd /opt/cm-lite-daemon
sudo ./register_node --host "${BCM_IP}" --disable-cert-check --vrf "${VRF}"
```

Enable + start service:

```bash
sudo systemctl enable --now cm-lite-daemon
sudo systemctl status cm-lite-daemon --no-pager -l
```

Check logs if needed:

```bash
sudo journalctl -u cm-lite-daemon -n 200 --no-pager
```

## Step 7 — Verify in BCM

On BCM:

```bash
cmsh -c "device; use leaf-01; show"
cmsh -c "device; use leaf-01; get status"
```

## Troubleshooting (high-signal)

- **Service crash loop / status=2**:
  - Check VRF mismatch (mgmt vs default):
    - On switch: `nv show int eth0 | grep vrf`
    - Ensure register/service is using the same VRF that routes to BCM.
  - Check journal: `sudo journalctl -u cm-lite-daemon -n 200 --no-pager`

- **Bootstrap certs missing on BCM**:
  - Ensure device exists + `initialize` was run.
  - Check `/cm/local/apps/cmd/etc/htdocs/switch/<hostname>/`

- **pip install cannot find a package offline**:
  - Your `pip_packages_dep/` does not contain a compatible wheel/sdist for the switch python version.
  - Re-download using the correct `--python-version`/`--abi` and include sdists for packages without wheels.


