Absolutely! Here’s a **clean, professional README.md** for your USB backup service, including setup, configuration, and deployment instructions:

---

# USB Network Backup Service

This service allows automatic backup of network-mounted directories to a USB drive. It supports:

* Automatic detection and mounting of the USB drive by UUID.
* Easy addition of new backup sources via a YAML configuration file.
* Daily automatic backups using a systemd timer.
* Logging of all backup operations.

---

## **Table of Contents**

* [Requirements](#requirements)
* [Installation](#installation)
* [Configuration](#configuration)
* [Deployment](#deployment)
* [Adding New Backup Sources](#adding-new-backup-sources)
* [Logs](#logs)

---

## **Requirements**

* Ubuntu (or any systemd-based Linux)
* Python 3
* `rsync` installed
* Network shares mounted (SMB/NFS) or accessible paths
* USB drive formatted and UUID noted

---

## **Installation**

1. **Clone or copy the repository/scripts to your system**:

```bash
sudo mkdir -p /usr/local/bin
sudo cp usb_backup.py /usr/local/bin/
sudo cp backup_config.yaml /usr/local/bin/
```

2. **Install dependencies**:

```bash
sudo apt update
sudo apt install python3-pip rsync -y
pip3 install pyyaml
```

3. **Make the script executable**:

```bash
sudo chmod +x /usr/local/bin/usb_backup.py
```

---

## **Configuration**

Edit `/usr/local/bin/backup_config.yaml`:

```yaml
usb:
  uuid: "1234-ABCD"          # UUID of your USB drive
  mount_point: "/mnt/usb_backup"

sources:
  - name: share1
    path: /mnt/network_share1
  - name: share2
    path: /mnt/network_share2
```

**Notes:**

* `uuid` — UUID of the USB drive. Find it with `lsblk -f`.
* `mount_point` — where the USB will be mounted.
* `sources` — list of network directories to back up. Each entry has:

  * `name` — used as a folder name on the USB drive.
  * `path` — network-mounted path.

---

## **Deployment**

### **1. Create systemd service**

`/etc/systemd/system/usb-backup.service`:

```ini
[Unit]
Description=USB Backup Service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/usb_backup.py
```

### **2. Create systemd timer**

`/etc/systemd/system/usb-backup.timer`:

```ini
[Unit]
Description=Run USB Backup Daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

### **3. Enable and start the timer**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now usb-backup.timer
```

Check timer status:

```bash
systemctl list-timers usb-backup.timer
```

---

## **Adding New Backup Sources**

1. Open the configuration file:

```bash
sudo nano /usr/local/bin/backup_config.yaml
```

2. Add a new source under `sources:`:

```yaml
  - name: share3
    path: /mnt/other_share
```

3. Save — the next backup run will include the new source automatically.

---

## **Logs**

* All backup operations are logged to:

```
/var/log/usb_backup.log
```

* Includes USB mount operations, backup start/end, and errors.

---

## **Usage**

* **Manual run** (for testing):

```bash
sudo /usr/local/bin/usb_backup.py
```

* **Automatic run** — handled by systemd timer daily.

---

## **Notes & Tips**

* Ensure network shares are mounted before backup, or configure `mount` options in the script if using SMB/NFS auto-mount.
* The USB drive must be connected at the time of backup. The script will abort safely if missing.
* All backups use `rsync --archive --delete` for incremental mirroring.
* Logs will help debug failed backups.