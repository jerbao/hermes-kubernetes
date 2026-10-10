# Encrypted backups

The runtime includes a pinned rclone binary. vm/configure_drive_backup.py can
authorize your own Google Desktop OAuth client and create an encrypted remote.
Keep OAuth credentials and the encryption recovery configuration outside Git.
Retain the recovery key somewhere independent of both the disposable terminal
and the encrypted backup itself. Backup schedules, roots and remotes belong to
the private instance. Restores target an empty staging area and coordinate
writers before replacing live state. Never commit raw SQLite or backup archives.
