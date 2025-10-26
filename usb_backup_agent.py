#!/usr/bin/env python3
import subprocess
import logging
import yaml
import os

CONFIG_FILE = "./backup_config.yaml"

# Load configuration
with open(CONFIG_FILE) as f:
    config = yaml.safe_load(f)

usb_uuid = config["usb"]["uuid"]
backup_root = config["usb"]["mount_point"]
sources = config["sources"]

# Logging
logging.basicConfig(
    filename=config.get("log_file", "/var/log/usb_backup_agent.log"),
    level=getattr(logging, config.get("log_level", "INFO").upper()),
    format='%(asctime)s %(levelname)s: %(message)s'
)

def mount_usb():
    """Mount USB drive by UUID if not already mounted."""
    if os.path.ismount(backup_root):
        logging.info(f"USB already mounted at {backup_root}")
        return True
    try:
        os.makedirs(backup_root, exist_ok=True)
        subprocess.run(["mount", "-U", usb_uuid, backup_root], check=True)
        logging.info(f"Mounted USB {usb_uuid} at {backup_root}")
        return True
    except subprocess.CalledProcessError:
        logging.error(f"USB {usb_uuid} not found or failed to mount. Aborting backup.")
        return False

def backup():
    """Run backups for all configured sources."""
    if not mount_usb():
        return

    for src in sources:
        name = src["name"]
        path = src["path"]
        exclude = src.get("exclude", [])  # list of rsync exclude patterns
        target = os.path.join(backup_root, name)
        os.makedirs(target, exist_ok=True)

        cmd = ["rsync", "-aAXv", "--delete"]
        for pattern in exclude:
            cmd += ["--exclude", pattern]
        cmd += [f"{path}/", f"{target}/"]

        logging.info(f"Starting backup: {path} -> {target}")
        try:
            # Run rsync and stream stdout/stderr for live logging
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
                for line in proc.stdout:
                    logging.info(line.rstrip())
            if proc.returncode == 0:
                logging.info(f"Backup completed for {name}")
            elif proc.returncode == 23:
                logging.warning(f"Partial backup for {name}, some files skipped")
            else:
                logging.error(f"Backup failed for {name}, return code {proc.returncode}")
        except Exception as e:
            logging.error(f"Backup exception for {name}: {e}")

if __name__ == "__main__":
    backup()
