## BCM11 agent notes for other agent (Test 4 failure analysis)

### What failed
- In BCM11 lab (run `20251226-210326-ac1529f0`) Test 4 failed on **"Validate ZTP recovery outcomes"**.
- The per-step log for "Validate ZTP recovery outcomes" was missing in this run directory (it was a custom step that didn’t always emit a step log). I patched `test-loop.py` locally to always write that step result to the run logger.

### Key observation (explains your grep)
- Your command `grep ZTP /cm/local/apps/cmd/etc/htdocs/switch/*/startup.yaml` returning **no output** is expected with the *current* Test 4 flow.
- Test 4 sets the marker on the **switch-side** `/etc/nvue.d/startup.yaml` via NVUE, then rebuilds the switches.
- Test 4 **does not re-run ZTP staging after adding the marker**, so BCM’s staged `startup.yaml` under `/cm/local/apps/cmd/etc/htdocs/switch/<sw>/startup.yaml` remains whatever was staged earlier (without the marker).
- After rebuild, ZTP applies the *old* staged file, so the marker will not come back and validation fails.

### Evidence from BCM11 Test 4 logs
- Step **"Set eth0 description marker (ZTP Works!)"** succeeded and verified the marker in the switch’s `/etc/nvue.d/startup.yaml` before rebuild:
  - `.logs/test-loop/20251226-210326-ac1529f0/36_online_Test_4_Set_eth0_description_marker_ZTP_Works.log` shows `verify startup.yaml marker` => `OK` for all switches.
- Step **"ztp-preflight.py (config-only)"** confirms BCM staged configs exist *at that time*, but it does not mean the staged file contains the marker.

### Root cause hypothesis (high confidence)
- **Test 4 marker semantics were backwards** for “prove ZTP worked”. If we set the marker on the switch before rebuild, seeing it later doesn’t prove ZTP.
- The intended proof is: marker exists **only** in BCM staged config, and appears on the switch **only after** ZTP applies it.

### Recommended fix (matches intended proof)
- Patch the marker **only in BCM** by editing each staged file:
  - `/cm/local/apps/cmd/etc/htdocs/switch/<sw>/startup.yaml`
  - under `- set: interface: eth0:` add/replace: `description: ZTP Works!`
- Do **not** touch the switch before rebuild.
- After rebuild, validate marker in `/etc/nvue.d/startup.yaml` on the switch.

### Implementation note
- I updated `test-loop.py` so the “Set eth0 description marker” step now patches BCM-staged `startup.yaml` in-place (no switch-side change).
- Also ensured `validate_ztp_recovery()` always writes a step log (it was missing in some runs).

### BCM10 note
- User saw BCM10 also fail Test 4 (and sometimes fail the old switch-side marker step). With the marker moved to BCM-staged config, we avoid that pre-rebuild switch dependency and make the test’s proof condition correct.
