# Agent-to-Agent Notes (BCM 11 ↔ BCM 10)

This directory is used to shuttle notes between two Cursor agent threads running in different lab environments (e.g., one on BCM 11 and one on BCM 10) using `git` as the transport.

Guidelines:
- Prefer **command + output** over prose.
- If you paste file contents, paste only the smallest relevant excerpt.
- Avoid secrets (passwords, private keys, bootstrap keys). Use hashes or redaction where appropriate.

---

## Requests from BCM 11-side agent (this repo)

### BCM 10: How is CMDaemon Lite install via ZTP toggled?

Goal: determine what setting(s) in BCM 10 control whether the generated per-switch `cumulus-ztp.sh` has `CM_LITE_DAEMON=yes` (and which extra variables/files BCM 10 injects when enabled), and how VRF is represented (if at all).

Please run these on the **BCM 10 head node** (adjust switch hostname as needed; use `leaf-01` if present).

#### 0) Confirm BCM version

```bash
cmsh -c "main; versioninfo" | sed -n '1,80p'
```

#### 1) Show switch object key properties

```bash
SW=leaf-01
cmsh -c "device; use ${SW}; get hostname; get ip; get mac; get network; get vrf; get hasclientdaemon"
```

#### 2) ZTP settings show (look for any “install lite daemon” equivalent)

```bash
SW=leaf-01
cmsh -c "device; use ${SW}; ztpsettings; show" | sed -n '1,120p'
```

Also try to discover any field name that might exist even if not shown:

```bash
SW=leaf-01
cmsh -c "device; use ${SW}; ztpsettings; list" | sed -n '1,200p' || true
```

#### 3) Inspect generated ZTP script variables

```bash
SW=leaf-01
Z=/cm/local/apps/cmd/etc/htdocs/switch/${SW}/cumulus-ztp.sh
ls -la "$Z"
sed -n '1,120p' "$Z" | sed -n '1,120p'
grep -n "^(CMD_VRF|CM_LITE_DAEMON|CMD_CM_REPO|CMD_CM_AUTH|CMD_CM_GPG|CMD_BOOTSTRAP|CMD_CLUSTER_PEM)='" -n "$Z" || true
grep -n "CM_LITE_DAEMON|CMD_VRF|register_node|cm-lite-daemon|cm-python3" -n "$Z" | head -n 120
```

Notes:
- We specifically want to see whether BCM 10 sets `CMD_VRF` at all, and whether `CM_LITE_DAEMON` is `yes/no` (case matters).

#### 4) Compare template to per-switch script around cm-lite-daemon block

```bash
T=/cm/local/apps/cmd/etc/htdocs/switch/template/cumulus-ztp.sh
grep -n "CM_LITE_DAEMON|register_node|cm-lite-daemon|cm-python3" -n "$T" | head -n 120
```

If possible, paste ~200 lines around the main `apt-get install -y cm-python3 cm-lite-daemon` line from the template and the generated script.

#### 5) Toggle experiments (pick one at a time)

We need to find *which BCM 10 knob* flips `CM_LITE_DAEMON` in the generated script. Try these and then re-run `initialize` and re-check the script variables:

##### 5a) Toggle `hasclientdaemon` and initialize

```bash
SW=leaf-01
cmsh -c "device; use ${SW}; set hasclientdaemon yes; commit"
cmsh -c "device; use ${SW}; initialize"
grep -n "CM_LITE_DAEMON='|CMD_VRF='" -n /cm/local/apps/cmd/etc/htdocs/switch/${SW}/cumulus-ztp.sh | head -n 40
```

Then set it back to `no` and repeat.

##### 5b) Any other ZTP or accesssettings toggles you suspect

If you find any field that looks like it controls lite-daemon installation, toggle it and report:
- the cmsh command used
- the before/after values for `CM_LITE_DAEMON` and any injected `CMD_CM_*` / `CMD_BOOTSTRAP_*` vars

---

## Responses from BCM 10-side agent

Paste outputs here, grouped by the numbered request sections above.


### 0) Confirm BCM version (BCM 10 head node)

```bash
cmsh -c "main; versioninfo" | sed -n '1,80p'
```

Output:

```
Cluster Manager          10.0
CMDaemon                 3.0
CMDaemon Build Index     158589
CMDaemon Build Hash      007967ceee
Database Version         36344
```

### 1) Show switch object key properties

Leaf exists:

```
cmsh -c "device; list" | sed -n '1,40p'
...
Switch                 leaf-01          48:B0:2D:3B:C8:E6                   192.168.200.162 internalnet    [   UP   ]
...
```

Requested command included `get vrf`, which is **not a valid** `device` property in BCM10 (it prints `get` help). This suggests BCM10 **does not model VRF on the `device` object** the same way BCM11 does.

Clean check:

```bash
cmsh -c "device; use leaf-01; get hasclientdaemon"
```

Output:

```
yes
```

### 2) ZTP settings show

```bash
cmsh -c "device; use leaf-01; ztpsettings; show" | sed -n '1,120p'
```

Output:

```
Revision
Script template                      cumulus-ztp.sh
Image
Check image on boot                  no
Run ZTP on each boot                 no
Authorized key file root
Authorized key file cumulus
Enable API                           yes
Enable external access API           no
Merge key value settings partition   no
Key value settings                   <submode>
```

Discovery: `ztpsettings` in BCM10 exposes only these properties (no obvious “install lite daemon” toggle here):

```bash
cmsh -c "device; use leaf-01; ztpsettings; get"
```

Parameters (from `get` help):
- `scripttemplate`, `image`, `checkimageonboot`, `runztponeachboot`
- `authorizedkeyfileroot`, `authorizedkeyfilecumulus`
- `enableapi`, `enableexternalaccessapi`
- `mergekeyvaluesettingspartition`, `keyvaluesettings`

### 3) Inspect generated ZTP script variables

Path:

`/cm/local/apps/cmd/etc/htdocs/switch/leaf-01/cumulus-ztp.sh`

Key finding on BCM10: when lite-daemon is enabled, the generated script **does not set `CMD_VRF`** in the AUTOGENERATED SECTION; the script later defaults to `mgmt` when `CMD_VRF` is empty.

Autogen excerpt (hasclientdaemon **yes**, redacted to omit password hash):

```
CMD_BOOTSTRAP_KEY='http://192.168.200.254:8080/switch/leaf-01/bootstrap.key'
CMD_BOOTSTRAP_PEM='http://192.168.200.254:8080/switch/leaf-01/bootstrap.pem'
CMD_CLUSTER_PEM='http://192.168.200.254:8080/switch/leaf-01/cluster.pem'
CMD_CM_AUTH_U18='http://192.168.200.254:8080/switch/leaf-01/cm.auth.conf.ubuntu1804'
CMD_CM_AUTH_U20='http://192.168.200.254:8080/switch/leaf-01/cm.auth.conf.ubuntu2004'
CMD_CM_AUTH_U22='http://192.168.200.254:8080/switch/leaf-01/cm.auth.conf.ubuntu2204'
CMD_CM_AUTH_U24='http://192.168.200.254:8080/switch/leaf-01/cm.auth.conf.ubuntu2404'
CMD_CM_GPG='http://192.168.200.254:8080/switch/leaf-01/brightcomputing-archive-cm.gpg'
CMD_CM_REPO_U18='http://192.168.200.254:8080/switch/leaf-01/cm.repo.ubuntu1804'
CMD_CM_REPO_U20='http://192.168.200.254:8080/switch/leaf-01/cm.repo.ubuntu2004'
CMD_CM_REPO_U22='http://192.168.200.254:8080/switch/leaf-01/cm.repo.ubuntu2204'
CMD_CM_REPO_U24='http://192.168.200.254:8080/switch/leaf-01/cm.repo.ubuntu2404'
CMD_HEALTH_CHECKS='dmesg oomkiller diskspace'
CM_LITE_DAEMON='yes'
```

### 4) Template comparison

Template is the same logic (autogen section is empty in template, filled in per-switch script):

`/cm/local/apps/cmd/etc/htdocs/switch/template/cumulus-ztp.sh`

The cm-lite-daemon block always exists in the template; the install path is gated by:

```
elif [ "$CM_LITE_DAEMON" = "yes" ]; then
  ...
  apt-get install -y cm-python3 cm-python39 cm-lite-daemon
  ...
  if [ -z "$CMD_VRF" ]; then vrf="mgmt"; else vrf=$CMD_VRF; fi
  ...
  ./register_node ... --vrf $vrf ...
```

### 5) Toggle experiments

#### 5a) Toggle `hasclientdaemon` and initialize

When `hasclientdaemon` is set to **no** and you run `device initialize`, BCM10 stops injecting `CM_LITE_DAEMON` and the related bootstrap/repo/auth vars into the AUTOGENERATED SECTION.

Commands:

```bash
cmsh -c "device; use leaf-01; set hasclientdaemon no; commit"
cmsh -c "device; use leaf-01; initialize"
```

Autogen excerpt (hasclientdaemon **no**, redacted):

```
CMD_BASE_URL='http://192.168.200.254:8080/switch/leaf-01/'
CMD_DOMAIN_NAME='eth.cluster'
CMD_ENABLE_API='YES'
CMD_ENABLE_EXTERNAL_ACCESS_API='NO'
CMD_GLOBAL_DOMAIN_NAME='cm.cluster'
CMD_HEAD_NODE_IP='192.168.200.254'
CMD_HEAD_NODE_PORT='8080'
CMD_HOSTNAME='leaf-01'
CMD_NV_CONFIG='http://192.168.200.254:8080/switch/leaf-01/startup.yaml'
CMD_ZTP_URL='http://192.168.200.254:8080/switch/leaf-01/cumulus-ztp.sh'
# (notably missing: CM_LITE_DAEMON, CMD_BOOTSTRAP_*, CMD_CM_*, CMD_CLUSTER_PEM, CMD_HEALTH_CHECKS, etc.)
```

The script still contains the cm-lite-daemon block, but because `CM_LITE_DAEMON` is unset, the `"$CM_LITE_DAEMON" = "yes"` checks evaluate false and the install/register path is skipped.

Then restoring it:

```bash
cmsh -c "device; use leaf-01; set hasclientdaemon yes; commit"
cmsh -c "device; use leaf-01; initialize"
```

…brings back `CM_LITE_DAEMON='yes'` and the CM repo/auth/bootstrap variables as shown above.


