#!/bin/sh
# Run inside a disposable Romulus QEMU guest.
set -eu
. /etc/os-release
test "${OPENBMC_TARGET_MACHINE:-}" = romulus
systemctl is-active --quiet xyz.openbmc_project.State.Host@0.service
systemctl is-active --quiet xyz.openbmc_project.State.Chassis@0.service

# These OpenPOWER board services target physical devices outside the G1 model.
for unit in avsbus-disable@0.service avsbus-enable@0.service cfam_override@0.service \
    fsi-scan.service op-cfam-reset.service vrm-control@0.service start_host@0.service; do
    directory="/run/systemd/system/$unit.d"
    mkdir -p "$directory"
    printf '[Unit]\nConditionPathExists=/run/bmc-g1-real-openpower-hardware\n' \
        > "$directory/90-bmc-g1-simulation.conf"
done
systemctl daemon-reload
systemctl mask --runtime --now org.openbmc.control.Power@0.service
