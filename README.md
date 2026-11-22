# Share Creator

Python GUI for CachyOS / KDE Plasma that sets up the SMB network shares:

| Share | Default mount | Host |
| --- | --- | --- |
| Krusty-Burger | `/mnt/Krusty-Burger` | `//192.168.2.50/Main` (Unraid) |
| Kwik-E-Mart | `/mnt/Kwik-E-Mart` | `//192.168.2.52/Main` (Unraid) |
| Moes-Tavern | `/mnt/Moes-Tavern` | `//192.168.2.51/DrivePool` (Windows 11) |

Each preset share has a checkbox. A **Custom** row at the bottom is unchecked until you fill in a name, `//host/Share`, and mount point. Apply will:

1. Ask for your sudo password (used once, not saved).
2. Write `~/.smbcredentials` as mode `600`.
3. Install `cifs-utils` if `mount.cifs` is missing.
4. Create the mount-point folders and chown them to you.
5. Rewrite only the `# BEGIN SHARE-CREATOR` block in `/etc/fstab`.
6. Mount the checked shares.
7. Map them as symlinks in `~/Shares/` (optional).
8. Install a KDE application-menu launcher (optional).
9. Run post-install checks (package, permissions, fstab, mount, write test, menu).

Unchecking a share and applying removes it from the managed fstab block and unmounts it. The mount-point directory is left in place.

## Install on CachyOS

```bash
sudo pacman -S --needed python-pyqt6 cifs-utils
git clone https://github.com/homerjatmoes/share-creator.git
cd share-creator
python share_creator.py
```

On the first Apply, leave **Install Share Creator into the KDE application menu** checked. That copies the app to `~/.local/share/share-creator/`, installs the icon, writes `~/.local/share/applications/share-creator.desktop`, and refreshes the KDE menu cache.

## Files

- `share_creator.py` — the GUI
- `share-creator.png` — application icon
- `share-creator.b64` — same icon, so the menu installer still works if the PNG is missing
