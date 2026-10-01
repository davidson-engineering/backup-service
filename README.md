# USB Network Backup Service

Daily, versioned backups of network shares (or any directories) to a
LUKS-encrypted USB drive, unlocked unattended with a YubiKey.

## How it works

A systemd timer starts `usb-backup.service` once a day, which runs
`usb-backup-runner.sh`:

1. **Unlock.** The runner asks the YubiKey (picked by serial) for its HMAC-SHA1
   response to a challenge stored on the host, derives a password from it,
   decrypts the LUKS key file with that password and opens the drive. The key
   only ever travels through pipes, never through a temporary file or a command
   line. If the YubiKey is missing and the runner was started from a terminal,
   it prompts for the LUKS passphrase instead.
2. **Mount** the drive at `/mnt/usb_backup`.
3. **Back up.** `usb_backup_agent.py` takes a snapshot of every source listed in
   `backup_config.yaml`.
4. **Unmount and lock** the drive again, also when an earlier step failed.

Each snapshot is a complete copy of the source in its own folder:

```
/mnt/usb_backup/
  share1/
    2026-09-29T020000Z/
    2026-09-30T020000Z/
  share2/
    ...
```

Files that did not change since the previous snapshot are hard links to it
(`rsync --link-dest`), so an extra snapshot only costs the space of what
changed. The newest `keep` snapshots per source are kept. Files deleted or
damaged on a share therefore stay recoverable from older snapshots.

Safety checks:

* A source that should be a mount point but is not (for example a network share
  that failed to mount and left an empty directory behind) is skipped, so its
  last good snapshots are never replaced by an empty one.
* The agent refuses to write unless the backup drive is actually mounted.
* Any failure makes the run exit non-zero, so systemd marks
  `usb-backup.service` as failed.

## Requirements

* Ubuntu or another systemd-based Linux
* Python 3.10+ with PyYAML, `rsync`, `cryptsetup`, `ykman` (yubikey-manager)
* A USB drive with a LUKS partition containing an ext4 file system (snapshots
  need hard links; ACLs and extended attributes are preserved too)
* A YubiKey with slot 2 programmed for HMAC-SHA1 challenge-response

```bash
sudo apt install rsync cryptsetup python3-yaml yubikey-manager
```

## Setup

### 1. Prepare the drive (skip if it already has LUKS + ext4)

This destroys everything on the partition, so double-check the device name.

```bash
sudo cryptsetup luksFormat /dev/sdX3          # asks for a fallback passphrase
sudo cryptsetup open /dev/sdX3 backupdisk
sudo mkfs.ext4 -L usb-backup /dev/mapper/backupdisk
sudo cryptsetup close backupdisk
lsblk -f                                      # note the UUID of the crypto_LUKS partition
```

### 2. Enroll the YubiKey

If slot 2 is not set up for challenge-response yet (this overwrites slot 2):

```bash
ykman otp chalresp --generate 2
```

Do not add `--touch`: the backup runs unattended, so nobody is there to touch
the key.

Then, as root, create the challenge and the encrypted LUKS key file. The
password derivation below must stay identical to `key_file_password` in
`usb-backup-runner.sh`.

```bash
sudo -i
umask 077
DEVICE=/dev/disk/by-uuid/<LUKS UUID from lsblk -f>
SERIAL=<YubiKey serial, see: ykman list --serials>

head -c 32 /dev/urandom > /root/.yk_challenge

# Random LUKS key, added as a new key slot (asks for the existing passphrase).
# It lives in RAM (/dev/shm) only until it is encrypted.
head -c 64 /dev/urandom > /dev/shm/backupdisk.key
cryptsetup luksAddKey "$DEVICE" /dev/shm/backupdisk.key

PASS=$(ykman --device "$SERIAL" otp calculate 2 "$(od -An -v -tx1 /root/.yk_challenge | tr -d ' \n')" \
       | tr -d '\n' | sha256sum | cut -d' ' -f1)
printf '%s\n' "$PASS" | openssl enc -aes-256-cbc -pbkdf2 -pass stdin \
    -in /dev/shm/backupdisk.key -out /root/.backupdisk.key.enc
rm /dev/shm/backupdisk.key
unset PASS
exit
```

### 3. Install

```bash
sudo install -d /usr/local/bin/usb-backup
sudo install -m 755 usb_backup_agent.py /usr/local/bin/usb-backup/
sudo install -m 644 backup_config.yaml /usr/local/bin/usb-backup/
sudo install -m 755 usb-backup-runner.sh /usr/local/bin/
sudo install -m 644 usb-backup.service usb-backup.timer /etc/systemd/system/
```

Set `LUKS_DEVICE` and `YUBI_SERIAL` at the top of
`/usr/local/bin/usb-backup-runner.sh`, then list your sources in
`/usr/local/bin/usb-backup/backup_config.yaml` (see below).

The runner uses `/usr/local/bin/usb-backup/venv/bin/python` if that venv
exists (install `requirements.txt` into it), and the system `python3`
otherwise.

### 4. Test run, then enable the timer

```bash
sudo systemctl daemon-reload
sudo systemctl start usb-backup.service       # waits until the backup finishes
journalctl -u usb-backup.service -n 50
sudo systemctl enable --now usb-backup.timer
systemctl list-timers usb-backup.timer
```

## Configuration

`/usr/local/bin/usb-backup/backup_config.yaml`:

```yaml
keep: 30                       # snapshots to keep per source (default 30)

sources:
  - name: share1               # folder name on the drive: letters, digits, . _ -
    path: /mnt/network_share1  # absolute path to back up
    exclude:                   # optional rsync exclude patterns
      - "*.tmp"
      - ".cache"

  - name: documents
    path: /srv/documents
    require_mount: false       # plain directory, not its own mount point
```

`require_mount` defaults to `true`: the path must be a mount point, so a share
that failed to mount is skipped instead of being backed up as empty. Set it to
`false` for a plain directory, including a subdirectory of a mounted share.

Unknown keys are rejected, so a typo makes the run fail rather than being
silently ignored. A new source is picked up by the next run.

## Restoring files

```bash
sudo cryptsetup open /dev/disk/by-uuid/<LUKS UUID> backupdisk   # LUKS passphrase
sudo mount /dev/mapper/backupdisk /mnt/usb_backup
ls /mnt/usb_backup/share1/                                      # one folder per snapshot, times in UTC
cp -a /mnt/usb_backup/share1/2026-09-30T020000Z/path/to/file ~/
sudo umount /mnt/usb_backup
sudo cryptsetup close backupdisk
```

If the timer fires while you have the drive mounted, the backup still runs, and
the runner leaves the drive mounted and unlocked as it found it.

## Logs and failure alerts

All output goes to the journal:

```bash
systemctl status usb-backup.service      # shows "failed" after an unsuccessful run
journalctl -u usb-backup.service         # full output of every run
```

To be notified when a run fails, hook a unit of your own into `OnFailure=`:

```ini
# /etc/systemd/system/usb-backup.service.d/alert.conf
[Unit]
OnFailure=usb-backup-alert.service

# /etc/systemd/system/usb-backup-alert.service
[Service]
Type=oneshot
ExecStart=<your notifier: mail, ntfy, ...> "usb-backup failed on %H, see journalctl -u usb-backup"
```

## Upgrading from the old mirror layout

Earlier versions kept a single mirror in `/mnt/usb_backup/<name>/`. Turn each
mirror into the first snapshot so the next run hard-links against it instead
of copying everything again:

```bash
cd /mnt/usb_backup
for name in share1 share2; do
    mv "$name" "$name.old" && mkdir "$name" && mv "$name.old" "$name/2026-01-01T000000Z"
done
```

## Security notes

* Unattended unlock means the YubiKey stays plugged into the host. The
  encryption protects the drive when it is away from the host (lost, stolen on
  its own, stored offsite), not against someone who has the host and the key.
* `/root/.yk_challenge` and `/root/.backupdisk.key.enc` must stay readable by
  root only.

## Development

```bash
python3 -m pytest tests          # needs GNU rsync, so on macOS use a container:
docker run --rm -v "$PWD":/src -w /src python:3.12 \
    sh -c 'apt-get update -qq && apt-get install -qq -y rsync >/dev/null && pip install -q -r requirements.txt pytest && pytest tests'
shellcheck usb-backup-runner.sh
```

CI runs both on every push and pull request.
