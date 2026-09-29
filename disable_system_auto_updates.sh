#!/usr/bin/env bash
set -euo pipefail

cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
EOF

systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service || true
systemctl mask apt-daily.service apt-daily-upgrade.service apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service || true

echo "System automatic updates have been disabled."
