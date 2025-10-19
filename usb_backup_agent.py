#!/usr/bin/env python3
import subprocess
import logging
import yaml
import os

CONFIG_FILE = "/usr/local/bin/backup_config.yaml"

# Load configuration
with open(CONFIG_FILE) as f:
    config = yaml.safe_load(f)

usb_uuid = config["usb"]["uuid"]
backup_root = config["usb"]["mount_point"]
sources = config["sources"]

# Logging
logging.basicConfig(
    filename=config.get("log_file", "/var/log/usb_backup.log"),
    level=getattr(logging, config.get("log_level", "INFO").upper()),
    format='%(asctime)s %(levelname)s: %(message)s'
)

def mount_usb():
    # Check if already mounted
    if os.path.ismount(backup_root):
        logging.info(f"USB already mounted at {backup_root}")
        return True
    # Attempt to mount by UUID
    try:
        os.makedirs(backup_root, exist_ok=True)
        subprocess.run(["mount", "-U", usb_uuid, backup_root], check=True)
        logging.info(f"Mounted USB {usb_uuid} at {backup_root}")
        return True
    except subprocess.CalledProcessError:
        logging.error(f"USB {usb_uuid} not found or failed to mount. Aborting backup.")
        return False

def backup():
    if not mount_usb():
        return
    for src in sources:
        name = src["name"]
        path = src["path"]
        target = os.path.join(backup_root, name)
        os.makedirs(target, exist_ok=True)
        cmd = ["rsync", "-av", "--delete", f"{path}/", f"{target}/"]
        logging.info(f"Starting backup: {path} -> {target}")
        try:
            subprocess.run(cmd, check=True)
            logging.info(f"Backup completed for {name}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Backup failed for {name}: {e}")

if __name__ == "__main__":
    backup()
