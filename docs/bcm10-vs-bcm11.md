## BCM 10.x vs BCM 11.x for Cumulus switches (documented differences)

This document summarizes **documented** differences between NVIDIA Base Command Manager (BCM) **10.x** and **11.x** that impact **Cumulus Linux switch** onboarding/monitoring, ZTP, image management, and where per-switch assets are stored.

### Scope and sources

- **Scope**: Cumulus switch onboarding and management topics, including `cm-lite-daemon`, ZTP, image management, and per-switch staging directories.
- **Primary sources** (from `.docs/`):
  - BCM 10: `.docs/bcm10manuals/admin-manual.txt`, `.docs/bcm10manuals/upgrade-manual.txt`
  - BCM 11: `.docs/bcm11manuals/admin-manual.txt`, `.docs/bcm11manuals/upgrade-manual.txt`, `.docs/bcm11manuals/nvidia-mission-control-manual.txt`

### Executive summary (high-impact deltas)

- **BCM 11 renames Cumulus configuration mode controls** from `cumulusmode/cumulusfile` to `nvconfigurationmode/nvconfigurationfile` (and renames the related submode from `cumulus` to `nvconfiguration`).
- **BCM 10 access settings include a `force` parameter** used in examples for Cumulus switch access configuration; **BCM 11 replaces that “override/force” concept with `Update in ztp` and `Update in NV` toggles** (documented for password update flows).
- **BCM 11 expands `ztpsettings`** to include new parameters/submodes like **`JSON template`**, **`Install lite daemon`**, and additional hooks (pre/post-install scripts, firmware, PTM topology file, etc.).
- **BCM 11 upgrade manual explicitly warns** that **mixing BCM 10 and BCM 11 `cm-lite-daemon` deployments is not supported** during/after a 10→11 upgrade.

---

## Differences by topic

### 1) Switch configuration mode / file-based config staging

#### BCM 10 documented behavior

- Cumulus configuration is managed via:
  - `device; use <switch>; set cumulusmode <auto|manual|file>`
  - `device; use <switch>; set cumulusfile startup.yaml`
- The manual describes applying configuration in the `cumulus` mode/submode, with `apply` used in auto/manual modes, and **not used in file mode** because file mode is applied via ZTP.

#### BCM 11 documented behavior

- The same conceptual model is documented, but the controls and submode name change:
  - `device; use <switch>; set nvconfigurationmode <auto|manual|file>`
  - `device; use <switch>; set nvconfigurationfile startup.yaml`
- The manual describes applying configuration in the `nvconfiguration` mode/submode, and likewise states **`apply` is not used in file mode** because file mode is applied via ZTP.

#### Compatibility implications

If scripts are targeting both BCM 10 and BCM 11, they should be prepared to:

- Set **either** `cumulusmode/cumulusfile` (BCM 10) **or** `nvconfigurationmode/nvconfigurationfile` (BCM 11).
- Expect the `cmsh` device configuration submode to be named `cumulus` (BCM 10) vs `nvconfiguration` (BCM 11) per the manuals.

---

### 2) Cumulus switch access settings (username/password + update mechanisms)

#### BCM 10 documented behavior

BCM 10 documents configuring Cumulus switch access via `accesssettings`, and examples include:

- Setting username/password and a **`force`** parameter (example uses `set -e force true`).
- A note that `force=true` must be set if `cm-lite-daemon` and ZTP are not installed, to allow the configuration script mechanism to carry out an image change during reboot via ZTP.

#### BCM 11 documented behavior

BCM 11 also documents configuring the username/password in `accesssettings`, but the presented fields differ:

- The `accesssettings` `show` output includes:
  - **`Update in ztp`** (yes/no)
  - **`Update in NV`** (yes/no)
- It documents that if `startup.yaml` does not set the password and `cm-lite-daemon` is not installed, then during reboot either:
  - `Update in ztp` must be set to yes (allow password change using ZTP), **or**
  - `Update in NV` must be set to yes (allow password change using NV)

#### Compatibility implications

- If tooling relies on the BCM 10 `accesssettings.force` knob, BCM 11 may not have it (or it may have a different model).
- BCM 11 introduces a second “update path” (NV vs ZTP) that could affect how/when credentials are pushed to switches.

---

### 3) ZTP settings (`ztpsettings`) and ZTP workflow

#### Core ZTP workflow (documented as the same in BCM 10 and BCM 11)

Both BCM 10 and BCM 11 admin manuals state:

- Committing a Cumulus switch configuration causes BCM to automatically configure **DHCP and DNS**.
- DHCP is configured by BCM to point the switch at the **ZTP custom provisioning script**.
- The per-switch ZTP script is generated on-demand from a template and can be generated immediately via `initialize`.

#### BCM 10 `ztpsettings` (documented fields)

BCM 10 shows `ztpsettings` including fields such as:

- Script template (`cumulus-ztp.sh`)
- Image (Cumulus image filename)
- Check image on boot
- Run ZTP on each boot
- Authorized key file root / cumulus
- Enable API / Enable external access API
- Key value settings (submode)

#### BCM 10 CMDaemon Lite via ZTP (observed behavior on BCM 10.0)

Based on lab observation (see `agent-to-agent/messages.md`), BCM 10 **does not expose** an `installlitedaemon` / `Install lite daemon` setting under `device; use <switch>; ztpsettings`.

However, BCM 10’s **ZTP script template already includes** the cm-lite-daemon installation/registration logic, gated by a variable:

- `CM_LITE_DAEMON='yes'` (lowercase string `yes`, not `YES`)

BCM 10 appears to control whether that variable and the supporting URLs are injected into the per-switch `cumulus-ztp.sh` **via the device property `hasclientdaemon`**:

- When `hasclientdaemon=yes` and `initialize` is run, BCM 10 injects `CM_LITE_DAEMON='yes'` and per-switch artifact URLs such as:
  - `CMD_BOOTSTRAP_KEY`, `CMD_BOOTSTRAP_PEM`, `CMD_CLUSTER_PEM`
  - `CMD_CM_REPO_U18/U20/U22/U24`, `CMD_CM_AUTH_U18/U20/U22/U24`, `CMD_CM_GPG`
  - `CMD_HEALTH_CHECKS`
- When `hasclientdaemon=no` and `initialize` is run, BCM 10 stops injecting those variables (the template still contains the cm-lite-daemon block, but it is skipped because `CM_LITE_DAEMON` is unset).

#### BCM 10 vs BCM 11: VRF injection differences (observed)

In BCM 11, the per-switch generated ZTP script includes an explicit `CMD_VRF='<vrf>'` (for example `CMD_VRF='mgmt'`).

In BCM 10 lab observation:

- `device; use <switch>; get vrf` is **not** a valid property (BCM 10 does not appear to model VRF on the device object the same way BCM 11 does).
- The per-switch generated ZTP script **does not set `CMD_VRF`** in the autogenerated section; the template’s cm-lite-daemon logic falls back to a default `mgmt` string when `CMD_VRF` is empty.

#### BCM 11 `ztpsettings` (documented expanded fields)

BCM 11 shows `ztpsettings` includes everything above, and additionally:

- **`JSON template`** (new field shown in the `ztpsettings` output)
- **`Install lite daemon`** (new field shown in the `ztpsettings` output)
- **`Authorized key file admin`** (additional field shown)
- Additional items/submodes:
  - **Firmwares**
  - **Preinstall scripts**
  - **Post-install scripts**
  - **PTM topology file**

#### Compatibility implications

BCM 11 appears to broaden the ZTP control surface beyond just “script + config + image”:

- It exposes additional hooks (pre/post, firmware, PTM topology).
- It suggests BCM can orchestrate **installing CMDaemon Lite** as part of ZTP (`Install lite daemon`), which is not shown in BCM 10’s `ztpsettings` view.

#### BCM 11 `Install lite daemon` (observed behavior on BCM 11.30.x)

In our BCM 11 lab, enabling/disabling `ztpsettings` **Install lite daemon** for a switch changes the **generated per-switch** `cumulus-ztp.sh` content under:

- `/cm/local/apps/cmd/etc/htdocs/switch/<switch>/cumulus-ztp.sh`

Specifically:

- When enabled, the autogenerated variables include:
  - `CM_LITE_DAEMON='YES'`
  - per-switch URLs for CM repo/auth/GPG and bootstrap/cluster artifacts (for example `CMD_CM_REPO_U20`, `CMD_CM_AUTH_U20`, `CMD_CM_GPG`, `CMD_BOOTSTRAP_KEY`, `CMD_BOOTSTRAP_PEM`, `CMD_CLUSTER_PEM`).
- When disabled, the generated script:
  - flips `CM_LITE_DAEMON='NO'`
  - omits the CM repo/auth/GPG + bootstrap/cluster URL variables from the autogenerated section.

The **installation logic itself is already present in the ZTP script template**, and is gated by `CM_LITE_DAEMON`. On BCM 11 systems, the template lives at:

- `/cm/local/apps/cmd/etc/htdocs/switch/template/cumulus-ztp.sh`

When `CM_LITE_DAEMON='YES'`, the script configures an apt repo, installs `cm-python3` + `cm-lite-daemon`, and runs `register_node` to register the daemon with the head node.

For reference, we copied the BCM 11 cm-lite-daemon install block into:

- `scripts/ztp-install-cmd.sh`

---

### 4) Image management (Cumulus OS images via ZTP)

#### Documented behavior (same concept in BCM 10 and BCM 11)

Both BCM 10 and BCM 11 admin manuals document:

- Setting an image in `ztpsettings` controls the image offered via ZTP.
- `checkimageonboot` determines whether the switch enforces that image at boot (and installs it if it differs).

#### Documented directory path for images (same in manuals)

Both manuals mention placing images under:

- `/cm/local/apps/cmd/etc/htdocs/switch/images/` (plural `images/`)

> Note: In our earlier lab checks (outside the manuals), we observed BCM systems serving images from a singular `image/` directory. That discrepancy appears to be documentation-vs-implementation, not a BCM10-vs-BCM11 documented difference.

---

### 5) File/folder structure for per-switch assets (templates, per-switch artifacts)

#### Documented structure (same in BCM 10 and BCM 11)

Both BCM 10 and BCM 11 admin manuals document:

- ZTP script template location:
  - `/cm/local/apps/cmd/etc/htdocs/switch/template` (template `cumulus-ztp.sh`)
- Per-switch staging directory:
  - `/cm/local/apps/cmd/etc/htdocs/switch/<switch or host name>/`
  - `startup.yaml` is staged there for file-based config application via ZTP

---

### 6) `cm-lite-daemon` deployment (CMDaemon Lite) and upgrade constraints

#### Installation/distribution (documented similarly in BCM 10 and BCM 11)

Both BCM 10 and BCM 11 admin manuals document:

- Install on head node (example): `yum install cm-lite-daemon`
- A ZIP artifact is placed at:
  - `/cm/shared/apps/cm-lite-daemon-dist/cm-lite-daemon.zip`
- Copy/unzip on the target “lite node”, then run `register_node` to install dependencies, request a certificate, register with the head node, and install as a service.

#### BCM 11 upgrade constraint (documented)

BCM 11 upgrade manual documents:

- BCM 10→11 upgrades support CMDaemon Lite running on cluster switches (such as Cumulus switches).
- **Mixing BCM 10 and BCM 11 deployments of `cm-lite-daemon` is not supported.**
  - It explicitly notes this includes CMDaemon Lite servers not managed by CMDaemon (e.g., PCs running CMDaemon Lite in a Python environment), which must be upgraded manually during the cluster upgrade.

---

### 7) BCM 11 “new features” adjacent to Cumulus switch management

BCM 11 includes a new manual:

- `.docs/bcm11manuals/nvidia-mission-control-manual.txt`

That manual describes NVLink switches (NVOS) and their ZTP model, including use of:

- ZTP “JSON template” files used to check the configured image and install `cm-lite-daemon`.

While this is not Cumulus-specific, it’s strong evidence that BCM 11 broadened switch ZTP management to cover additional switch OS families, and helps explain why BCM 11’s `ztpsettings` exposes a `JSON template` knob and an `Install lite daemon` knob.

---

## Checklist of BCM10→BCM11 compatibility items for our scripts (derived from manuals)

- **Configuration staging mode**
  - BCM 10: `cumulusmode`, `cumulusfile`
  - BCM 11: `nvconfigurationmode`, `nvconfigurationfile`
- **Access settings**
  - BCM 10: includes `force` in documented examples/flow
  - BCM 11: includes `Update in ztp` and `Update in NV`
- **ZTP settings surface**
  - BCM 11 adds `JSON template`, `Install lite daemon`, and additional hook submodes (pre/post-install, firmware, PTM topology)
- **Upgrade consideration**
  - BCM 11: do not mix BCM10 and BCM11 `cm-lite-daemon` deployments during/after upgrade


