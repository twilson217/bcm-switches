#!/usr/bin/env bash
#
# Extracted reference: BCM 11 Cumulus ZTP template cm-lite-daemon installation block.
#
# Source (on BCM head node):
#   /cm/local/apps/cmd/etc/htdocs/switch/template/cumulus-ztp.sh
#
# Notes:
# - This is provided for reference and future BCM 10 parity work (template customization).
# - It is NOT intended to be run directly on its own without the surrounding ZTP script context.
# - It assumes variables like CM_LITE_DAEMON, CMD_CM_REPO_U*, CMD_CM_AUTH_U*, CMD_CM_GPG,
#   CMD_CLUSTER_PEM, CMD_BOOTSTRAP_PEM, CMD_BOOTSTRAP_KEY, CMD_HEAD_NODE_IP, CMD_HOSTNAME,
#   CMD_VRF, CMD_INTERFACE, and DEB_VERSION are already set by BCM's ZTP autogen section.
#
# The code in this repo is provided **as-is**, with **no guarantees**. By using this code, you accept responsibility for all risks that may come from using it.
set -euo pipefail

# --- BEGIN BCM TEMPLATE EXTRACT (cm-lite-daemon) ---

# Assumes DEB_VERSION logic already set: repo/auth variables selected above.

if [ -z "${repo:-}" ]; then
  if [ "${CM_LITE_DAEMON:-NO}" = "YES" ]; then
    echo "!!! unable to determine repository to get cm-lite-daemon from !!!"
  fi
elif [ "${CM_LITE_DAEMON:-NO}" = "YES" ]; then
  target="/etc/apt/sources.list.d/cm.list"
  if [ ! -e "$target.freeze" ]; then
    wget --no-check-certificate -O "$target" "$repo"
  fi
  if [ ! -z "${CMD_CM_GPG:-}" ]; then
    target="/etc/apt/trusted.gpg.d/brightcomputing-archive-cm.gpg"
    if [ ! -e "$target.freeze" ]; then
      wget --no-check-certificate -O "$target" "$CMD_CM_GPG"
    fi
  fi
  if [ ! -z "${CMD_CM_NIGHTLY_CONF:-}" ]; then
    target="/etc/apt/auth.conf.d/cm-nightly.conf"
    if [ ! -e "$target.freeze" ]; then
      wget --no-check-certificate -O "$target" "$CMD_CM_NIGHTLY_CONF"
      if [ "${DEB_VERSION:-}" = "10" ]; then
        perl -pi -e 's#http://##g' "$target"
      fi
    fi
  elif [ ! -z "${auth:-}" ]; then
    target="/etc/apt/auth.conf.d/cm.conf"
    if [ ! -e "$target.freeze" ]; then
      wget --no-check-certificate -O "$target" "$auth"
    fi
  fi

  echo "[start:apt-get]"
  apt-get update -y
  apt-get install -y cm-python3 cm-lite-daemon
  echo "[end:apt-get]"

  echo "[start:cm-lite-daemon]"
  cd "/cm/local/apps/cm-lite-daemon"
  if [ -e "etc/cluster.pem" ]; then
    wget --no-check-certificate -O /tmp/cluster.pem "$CMD_CLUSTER_PEM"
    if diff /tmp/cluster.pem etc/cluster.pem > /dev/null; then
      echo "* keep cluster certificate"
      rm -f /tmp/cluster.pem
    else
      echo "* update cluster certificate"
      mv /tmp/cluster.pem etc/cluster.pem
      rm -f etc/bootstrap.pem
      rm -f etc/bootstrap.key
      rm -f etc/cert.pem
      rm -f etc/cert.key
    fi
  else
    echo "* get cluster certificate"
    wget --no-check-certificate -O etc/cluster.pem "$CMD_CLUSTER_PEM"
  fi
  if [ -e "etc/cert.pem" ]; then
    echo "* cm-lite-daemon already setup"
  else
    echo "* setup cm-lite-daemon for the first time"
    echo "* get bootstrap certificate"
    wget --no-check-certificate -O etc/bootstrap.pem "$CMD_BOOTSTRAP_PEM"
    wget --no-check-certificate -O etc/bootstrap.key "$CMD_BOOTSTRAP_KEY"
    if [ -z "${CMD_VRF:-}" ]; then
      vrf=$(vrf list 2>/dev/null || true | grep mgmt | cut -d" " -f1)
    else
      vrf=$CMD_VRF
    fi
    if [ -z "${CMD_INTERFACE:-}" ]; then
      interface="eth0"
    else
      interface=$CMD_INTERFACE
    fi
    echo "* register cm-lite-daemon with $CMD_HEAD_NODE_IP"
    ./register_node --node "$CMD_HOSTNAME" \
                    --interface $interface \
                    --vrf "$vrf" \
                    --host $CMD_HEAD_NODE_IP \
                    --no-service \
                    --disable-cert-check \
                    --disable-hostname-check
    cd ..
  fi
  service cm-lite-daemon status
  echo "[end:cm-lite-daemon]"
  cd /root
elif [ -e "/cm/local/apps/cm-lite-daemon/etc/cluster.pem" ]; then
  echo "[start:cm-lite-daemon:certificate]"
  cd "/cm/local/apps/cm-lite-daemon"
  curl --insecure -o /tmp/cluster.pem "$CMD_CLUSTER_PEM"
  if diff /tmp/cluster.pem etc/cluster.pem > /dev/null; then
    echo "* keep cluster certificate"
    rm -f /tmp/cluster.pem
  else
    echo "* update cluster certificate"
    now=$(date +%s)
    mv etc/cert.pem etc/cert.pem.$now
    mv etc/cert.key etc/cert.pem.$now
    mv etc/cluster.pem etc/cluster.pem.$now
    mv /tmp/cluster.pem etc/cluster.pem
    rm -f etc/bootstrap.pem
    rm -f etc/bootstrap.key
    curl --insecure -o etc/bootstrap.pem "$CMD_BOOTSTRAP_PEM"
    curl --insecure -o etc/bootstrap.key "$CMD_BOOTSTRAP_KEY"
    if [ -z "${CMD_VRF:-}" ]; then
      vrf=$(vrf list 2>/dev/null || true | grep mgmt | cut -d" " -f1)
    else
      vrf="--vrf $CMD_VRF"
    fi
    if [ -z "${CMD_INTERFACE:-}" ]; then
      interface="eth0"
    else
      interface=$CMD_INTERFACE
    fi
    echo "* get new certificate for cm-lite-daemon from $CMD_HEAD_NODE_IP"
    ./register_node --node "$CMD_HOSTNAME" \
                    --interface $interface \
                    --vrf "$vrf" \
                    --host $CMD_HEAD_NODE_IP \
                    --no-service \
                    --disable-cert-check \
                    --disable-hostname-check
    cd ..
  fi
  service cm-lite-daemon status
  echo "[end:cm-lite-daemon:certificate]"
  cd /root
fi

# --- END BCM TEMPLATE EXTRACT (cm-lite-daemon) ---

