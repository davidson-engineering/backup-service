#!/bin/bash
# Unlock and mount the LUKS backup drive, run the backup agent, then unmount and
# lock the drive again. Cleanup also runs when a step fails, and only undoes
# what this run did, so a drive you mounted by hand (e.g. to restore files) is
# left as it was.
set -euo pipefail
shopt -s inherit_errexit

# --- Configuration ---
SCRIPT_DIR="/usr/local/bin/usb-backup"
LUKS_DEVICE="/dev/disk/by-uuid/CHANGE-ME"   # UUID of the crypto_LUKS partition, see `lsblk -f`
MAPPER_NAME="backupdisk"
MOUNT_POINT="/mnt/usb_backup"
KEY_ENC_FILE="/root/.backupdisk.key.enc"
CHALLENGE_FILE="/root/.yk_challenge"
YUBI_SERIAL="11115787"           # Serial of the YubiKey to use for unlock
AGENT="$SCRIPT_DIR/usb_backup_agent.py"
CONFIG="$SCRIPT_DIR/backup_config.yaml"
PYTHON="$SCRIPT_DIR/venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
LOCK_FILE="/run/usb-backup.lock"

log() { echo "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"

exec 9>"$LOCK_FILE"
flock -n 9 || die "another backup run is in progress"

OPENED=0
MOUNTED=0
cleanup() {
    local status=$?
    if (( MOUNTED )); then
        log "Unmounting $MOUNT_POINT..."
        umount "$MOUNT_POINT" || { echo "ERROR: failed to unmount $MOUNT_POINT" >&2; status=1; }
    fi
    if (( OPENED )); then
        log "Locking $MAPPER_NAME..."
        cryptsetup close "$MAPPER_NAME" || { echo "ERROR: failed to lock $MAPPER_NAME" >&2; status=1; }
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Prints the password protecting $KEY_ENC_FILE: the SHA-256 of the YubiKey's
# HMAC-SHA1 (slot 2) response to the challenge stored in $CHALLENGE_FILE.
key_file_password() {
    local challenge
    challenge=$(od -An -v -tx1 "$CHALLENGE_FILE" | tr -d ' \n')
    ykman --device "$YUBI_SERIAL" otp calculate 2 "$challenge" | tr -d '\n' | sha256sum | cut -d' ' -f1
}

# --- Unlock ---
if ! cryptsetup status "$MAPPER_NAME" &>/dev/null; then
    [[ -b "$LUKS_DEVICE" ]] || die "backup drive $LUKS_DEVICE not found - is it plugged in?"
    if ykman list --serials | grep -Fx "$YUBI_SERIAL" >/dev/null; then
        log "Unlocking $LUKS_DEVICE with YubiKey $YUBI_SERIAL..."
        password=$(key_file_password)
        # The password and the decrypted key only travel through pipes: never
        # through a command line (visible in ps) or a file.
        printf '%s\n' "$password" \
            | openssl enc -d -aes-256-cbc -pbkdf2 -pass stdin -in "$KEY_ENC_FILE" \
            | cryptsetup open "$LUKS_DEVICE" "$MAPPER_NAME" --key-file=-
        unset password
    elif [[ -t 0 ]]; then
        log "YubiKey $YUBI_SERIAL not found. Prompting for LUKS passphrase..."
        cryptsetup open "$LUKS_DEVICE" "$MAPPER_NAME"
    else
        die "YubiKey $YUBI_SERIAL not found and there is no terminal to prompt for the passphrase"
    fi
    OPENED=1
fi

# --- Mount ---
if mountpoint -q "$MOUNT_POINT"; then
    mounted_from=$(findmnt -n -o SOURCE --mountpoint "$MOUNT_POINT")
    [[ "$mounted_from" == "/dev/mapper/$MAPPER_NAME" ]] \
        || die "$MOUNT_POINT is already mounted from $mounted_from, not /dev/mapper/$MAPPER_NAME"
else
    log "Mounting /dev/mapper/$MAPPER_NAME at $MOUNT_POINT..."
    mkdir -p "$MOUNT_POINT"
    mount "/dev/mapper/$MAPPER_NAME" "$MOUNT_POINT"
    MOUNTED=1
fi

# --- Back up ---
log "Running backup agent..."
"$PYTHON" "$AGENT" --config "$CONFIG" --dest "$MOUNT_POINT"
log "Backup complete."
