#!/bin/bash
# Wrapper to unlock USB, run Python backup, then optionally unmount
set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="/usr/local/bin/usb-backup"
cd "$SCRIPT_DIR" || exit 1

LUKS_DEVICE="/dev/sda3"
MAPPER_NAME="backupdisk"
MOUNT_POINT="/mnt/usb_backup"
KEY_ENC_FILE="/root/.backupdisk.key.enc"
CHALLENGE_FILE="/root/.yk_challenge"
YUBI_SERIAL="11115787"           # Serial of the YubiKey to use for unlock
PYTHON_VENV="$SCRIPT_DIR/venv"
PY_BACKUP_SCRIPT="$SCRIPT_DIR/usb_backup.py"
LOG_FILE="/var/log/usb_backup.log"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG_FILE"; }

# --- Unlock LUKS ---
if ! cryptsetup status "$MAPPER_NAME" &>/dev/null; then
    if ykman list | grep -q "Serial: $YUBI_SERIAL"; then
        KEY_INDEX=$(ykman list | grep -n "Serial: $YUBI_SERIAL" | head -n1 | cut -d: -f1)
        log "Unlocking $LUKS_DEVICE with YubiKey $YUBI_SERIAL (index $KEY_INDEX)..."

        RESP_HEX=$(sudo cat "$CHALLENGE_FILE" | ykchalresp -nkey "$KEY_INDEX" -2 -H | tr -d '\n')
        PASS=$(printf "%s" "$RESP_HEX" | sha256sum | awk '{print $1}')

        TMP_KEY="/tmp/backupkey.bin"
        sudo openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:"$PASS" -in "$KEY_ENC_FILE" -out "$TMP_KEY"
        sudo chmod 600 "$TMP_KEY"

        sudo cryptsetup open "$LUKS_DEVICE" "$MAPPER_NAME" --key-file="$TMP_KEY"
    else
        log "YubiKey $YUBI_SERIAL not found. Prompting for LUKS passphrase..."
        sudo cryptsetup open "$LUKS_DEVICE" "$MAPPER_NAME"
    fi
fi

# --- Mount ---
if ! mountpoint -q "$MOUNT_POINT"; then
    log "Mounting $MAPPER_NAME..."
    sudo mkdir -p "$MOUNT_POINT"
    sudo mount "/dev/mapper/$MAPPER_NAME" "$MOUNT_POINT"
fi

# --- Activate Python virtual environment ---
if [[ -d "$PYTHON_VENV" ]]; then
    log "Activating Python virtual environment..."
    source "$PYTHON_VENV/bin/activate"
fi

# --- Run Python backup service ---
log "Running Python backup service..."
python "$PY_BACKUP_SCRIPT"
deactivate || true

# --- Unmount and close (optional) ---
log "Unmounting and closing $MAPPER_NAME..."
sudo umount "$MOUNT_POINT"
sudo cryptsetup close "$MAPPER_NAME"
log "Backup complete."
