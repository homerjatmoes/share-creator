#!/usr/bin/env python3
"""Share Creator — set up SMB network shares on CachyOS / KDE Plasma."""

from __future__ import annotations

import base64
import getpass
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Share Creator"
APP_ID = "share-creator"
FSTAB_BEGIN = "# BEGIN SHARE-CREATOR"
FSTAB_END = "# END SHARE-CREATOR"
HERE = Path(__file__).resolve().parent

DEFAULT_SHARES = [
    {
        "name": "Krusty-Burger",
        "source": "//192.168.2.50/Main",
        "mount": "/mnt/Krusty-Burger",
        "kind": "Unraid",
    },
    {
        "name": "Kwik-E-Mart",
        "source": "//192.168.2.52/Main",
        "mount": "/mnt/Kwik-E-Mart",
        "kind": "Unraid",
    },
    {
        "name": "Moes-Tavern",
        "source": "//192.168.2.51/DrivePool",
        "mount": "/mnt/Moes-Tavern",
        "kind": "Windows 11",
    },
]


def icon_png_bytes() -> bytes:
    png = HERE / "share-creator.png"
    if png.is_file():
        return png.read_bytes()
    b64_path = HERE / "share-creator.b64"
    if b64_path.is_file():
        return base64.b64decode(b64_path.read_text())
    return b""


def shq(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def uid_gid() -> tuple[int, int, str]:
    uid = os.getuid()
    gid = os.getgid()
    name = getpass.getuser()
    return uid, gid, name


def home_map_dir() -> Path:
    return Path.home() / "Shares"


def default_credentials_path() -> Path:
    return Path.home() / ".smbcredentials"


def read_fstab() -> str:
    path = Path("/etc/fstab")
    if not path.exists():
        return ""
    return path.read_text()


def strip_managed_block(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == FSTAB_BEGIN:
            skipping = True
            continue
        if line.strip() == FSTAB_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def managed_mounts(text: str) -> list[str]:
    mounts: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip() == FSTAB_BEGIN:
            inside = True
            continue
        if line.strip() == FSTAB_END:
            inside = False
            continue
        if inside and line.strip() and not line.strip().startswith("#"):
            parts = line.split()
            if len(parts) >= 2:
                mounts.append(parts[1])
    return mounts


def fstab_line(source: str, mount: str, creds: str, uid: int, gid: int) -> str:
    opts = (
        f"credentials={creds},uid={uid},gid={gid},forceuid,forcegid,"
        "iocharset=utf8,file_mode=0664,dir_mode=0775,vers=3.0,"
        "nofail,_netdev,x-systemd.automount,x-systemd.mount-timeout=30,x-systemd.idle-timeout=300"
    )
    return f"{source}  {mount}  cifs  {opts}  0  0"


def build_fstab(selected: list[dict], creds: str) -> str:
    uid, gid, _ = uid_gid()
    base = strip_managed_block(read_fstab()).rstrip() + "\n\n"
    block = [FSTAB_BEGIN]
    for share in selected:
        block.append(f"# {share['name']}")
        block.append(fstab_line(share["source"], share["mount"], creds, uid, gid))
    block.append(FSTAB_END)
    return base + "\n".join(block) + "\n"


def is_mounted(mount: str) -> bool:
    result = subprocess.run(
        ["findmnt", "-n", "-M", mount],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_root(password: str, script: str) -> subprocess.CompletedProcess[str]:
    # sudo -S reads the password from stdin; the script is an argument, not stdin.
    return subprocess.run(
        ["sudo", "-k", "-S", "-p", "", "bash", "-c", script],
        input=password + "\n",
        capture_output=True,
        text=True,
    )


class ApplyWorker(QThread):
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, payload: dict):
        super().__init__()
        self.payload = payload

    def emit_line(self, text: str) -> None:
        self.log.emit(text)

    def run(self) -> None:
        try:
            ok, summary = self.apply()
        except Exception as exc:  # noqa: BLE001 — show any failure in the log
            self.emit_line(f"ERROR: {exc}")
            self.finished_ok.emit(False, str(exc))
            return
        self.finished_ok.emit(ok, summary)

    def apply(self) -> tuple[bool, str]:
        data = self.payload
        password: str = data["sudo_password"]
        selected: list[dict] = data["shares"]
        username: str = data["username"].strip()
        smb_password: str = data["smb_password"]
        domain: str = data["domain"].strip()
        creds_path = Path(data["creds_path"]).expanduser()
        do_map: bool = data["map_home"]
        do_menu: bool = data["install_menu"]

        if not username:
            raise RuntimeError("SMB username is required.")
        if not smb_password:
            raise RuntimeError("SMB password is required.")
        if "\n" in smb_password or "\n" in username:
            raise RuntimeError("Username and password cannot contain newlines.")
        if not selected:
            self.emit_line("No shares checked. Managed fstab entries will be removed.")

        uid, gid, user = uid_gid()
        self.emit_line(f"User: {user}  uid={uid} gid={gid}")

        # Credentials file is owned by the user, never by root.
        self.emit_line(f"Writing credentials → {creds_path}")
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"username={username}", f"password={smb_password}"]
        if domain:
            lines.append(f"domain={domain}")
        fd, tmp_cred = tempfile.mkstemp(prefix="smbcred-", dir=str(creds_path.parent))
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode())
            os.close(fd)
            os.chmod(tmp_cred, 0o600)
            os.replace(tmp_cred, creds_path)
        finally:
            if os.path.exists(tmp_cred):
                os.remove(tmp_cred)
        os.chmod(creds_path, 0o600)
        self.emit_line("Credentials file mode 600.")

        old_mounts = managed_mounts(read_fstab())
        new_mounts = [s["mount"] for s in selected]
        to_unmount = [m for m in old_mounts if m not in new_mounts]

        fstab_body = build_fstab(selected, str(creds_path))
        fd, tmp_fstab = tempfile.mkstemp(prefix="fstab-share-creator-")
        os.write(fd, fstab_body.encode())
        os.close(fd)
        os.chmod(tmp_fstab, 0o644)

        mount_mk = "\n".join(f"mkdir -p {shq(m)}" for m in new_mounts)
        unmount_sh = "\n".join(
            f"if findmnt -n -M {shq(m)} >/dev/null 2>&1; then umount {shq(m)} || umount -l {shq(m)}; fi"
            for m in to_unmount
        )
        mount_sh = "\n".join(
            f"if ! findmnt -n -M {shq(m)} >/dev/null 2>&1; then mount {shq(m)}; fi"
            for m in new_mounts
        )
        chown_sh = "\n".join(
            f"chown {uid}:{gid} {shq(m)}" for m in new_mounts
        )

        script = f"""
set -euo pipefail
if ! command -v mount.cifs >/dev/null 2>&1; then
  pacman -S --needed --noconfirm cifs-utils
fi
{unmount_sh}
install -m 644 {shq(tmp_fstab)} /etc/fstab
{mount_mk}
{chown_sh}
systemctl daemon-reload || true
{mount_sh}
"""
        self.emit_line("Applying with sudo (packages, folders, fstab, mount)...")
        result = run_root(password, script)
        try:
            os.remove(tmp_fstab)
        except OSError:
            pass
        if result.stdout.strip():
            self.emit_line(result.stdout.strip())
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            self.emit_line(err or "sudo command failed")
            if "incorrect password" in err.lower() or "try again" in err.lower() or "sorry" in err.lower():
                raise RuntimeError("sudo password was rejected.")
            raise RuntimeError("Apply failed. See the log above.")
        self.emit_line("Root steps finished.")

        if do_map:
            link_dir = home_map_dir()
            link_dir.mkdir(parents=True, exist_ok=True)
            selected_names = {s["name"] for s in selected}
            for share in selected:
                link = link_dir / share["name"]
                target = Path(share["mount"])
                if link.is_symlink() or link.exists():
                    if link.is_symlink() and os.path.realpath(link) == str(target):
                        self.emit_line(f"Map exists: {link} → {target}")
                        continue
                    if link.is_symlink():
                        link.unlink()
                    else:
                        self.emit_line(f"Skip map {link}: path exists and is not a symlink")
                        continue
                link.symlink_to(target)
                self.emit_line(f"Mapped {link} → {target}")
            # Drop symlinks for shares we no longer manage.
            if link_dir.is_dir():
                for child in link_dir.iterdir():
                    if child.name in selected_names or not child.is_symlink():
                        continue
                    real = os.path.realpath(child)
                    if real in to_unmount or real in old_mounts:
                        child.unlink()
                        self.emit_line(f"Removed map {child}")

        if do_menu:
            self.emit_line("Installing KDE menu entry...")
            install_menu_entry()
            self.emit_line("KDE menu entry installed. If it is missing, log out and back in once.")

        self.emit_line("")
        self.emit_line("—— Post-install checks ——")
        checks = post_install_checks(selected, creds_path, do_map, do_menu)
        failed = 0
        for ok, message in checks:
            self.emit_line(("OK   " if ok else "FAIL ") + message)
            if not ok:
                failed += 1
        if failed:
            return False, f"Applied with {failed} check(s) failing. See the log."
        return True, "Shares are configured, mounted, and mapped."


def post_install_checks(
    selected: list[dict],
    creds_path: Path,
    do_map: bool,
    do_menu: bool,
) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    mount_cifs = shutil.which("mount.cifs")
    checks.append((bool(mount_cifs), f"cifs-utils (mount.cifs): {mount_cifs or 'not installed'}"))

    if creds_path.is_file():
        mode = stat.S_IMODE(creds_path.stat().st_mode)
        owner = creds_path.stat().st_uid == os.getuid()
        checks.append((mode == 0o600 and owner, f"credentials {creds_path} mode {mode:o}, owner ok={owner}"))
    else:
        checks.append((False, f"credentials file missing: {creds_path}"))

    fstab = read_fstab()
    for share in selected:
        mount = share["mount"]
        src = share["source"]
        path = Path(mount)
        checks.append((path.is_dir(), f"mount point exists: {mount}"))
        in_fstab = src in fstab and mount in fstab and FSTAB_BEGIN in fstab
        checks.append((in_fstab, f"fstab entry: {src} → {mount}"))
        mounted = is_mounted(mount)
        checks.append((mounted, f"mounted now: {mount}"))
        if mounted and path.is_dir():
            probe = path / ".share-creator-write-test"
            try:
                probe.write_text("ok\n")
                probe.unlink()
                checks.append((True, f"writable: {mount}"))
            except OSError as exc:
                checks.append((False, f"not writable: {mount} ({exc})"))
        if do_map:
            link = home_map_dir() / share["name"]
            ok = link.is_symlink() and os.path.realpath(link) == mount
            checks.append((ok, f"home map: {link} → {mount}"))

    if do_menu:
        desktop = Path.home() / ".local/share/applications" / f"{APP_ID}.desktop"
        icon = Path.home() / ".local/share/icons/hicolor/256x256/apps" / f"{APP_ID}.png"
        checks.append((desktop.is_file(), f"KDE desktop file: {desktop}"))
        checks.append((icon.is_file(), f"KDE icon: {icon}"))
    return checks


def install_menu_entry() -> None:
    app_dir = Path.home() / ".local/share" / APP_ID
    app_dir.mkdir(parents=True, exist_ok=True)
    target_script = app_dir / "share_creator.py"
    # Copy this file so the menu launcher does not depend on the git checkout.
    if Path(__file__).resolve() != target_script.resolve():
        shutil.copy2(Path(__file__).resolve(), target_script)
    b64_src = HERE / "share-creator.b64"
    if b64_src.is_file() and HERE.resolve() != app_dir.resolve():
        shutil.copy2(b64_src, app_dir / "share-creator.b64")
    png = icon_png_bytes()
    icon_dir = Path.home() / ".local/share/icons/hicolor/256x256/apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_path = icon_dir / f"{APP_ID}.png"
    if png:
        icon_path.write_bytes(png)
        (app_dir / "share-creator.png").write_bytes(png)

    python = shutil.which("python3") or "/usr/bin/python3"
    desktop_dir = Path.home() / ".local/share/applications"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop = desktop_dir / f"{APP_ID}.desktop"
    desktop.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                f"Name={APP_NAME}",
                "GenericName=SMB Share Setup",
                "Comment=Create credentials, mount points, and mount SMB shares",
                f"Exec={python} {target_script}",
                f"Icon={APP_ID}",
                "Terminal=false",
                "Categories=Network;System;",
                "Keywords=smb;cifs;mount;nas;share;",
                f"StartupWMClass={APP_ID}",
                "",
            ]
        )
    )
    desktop.chmod(0o644)
    subprocess.run(
        ["update-desktop-database", str(desktop_dir)],
        capture_output=True,
        text=True,
    )
    if shutil.which("kbuildsycoca6"):
        subprocess.run(["kbuildsycoca6", "--noincremental"], capture_output=True, text=True)


class ShareRow:
    def __init__(self, spec: dict):
        self.spec = spec
        self.enabled = QCheckBox(spec["name"])
        self.enabled.setChecked(True)
        self.enabled.setToolTip(spec["kind"])
        self.kind = QLabel(spec["kind"])
        self.kind.setStyleSheet("color: palette(mid);")
        self.source = QLineEdit(spec["source"])
        self.mount = QLineEdit(spec["mount"])
        self.source.setPlaceholderText("//server/Share")
        self.mount.setPlaceholderText("/mnt/Name")

    def value(self) -> dict | None:
        if not self.enabled.isChecked():
            return None
        source = self.source.text().strip()
        mount = self.mount.text().strip()
        if not source.startswith("//") or "/" not in source[2:]:
            raise ValueError(f"{self.spec['name']}: share must look like //host/Share")
        if not mount.startswith("/"):
            raise ValueError(f"{self.spec['name']}: mount point must be an absolute path")
        return {
            "name": self.spec["name"],
            "source": source,
            "mount": mount,
            "kind": self.spec["kind"],
        }


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(860, 720)
        self.worker: ApplyWorker | None = None
        png = icon_png_bytes()
        if png:
            pixmap = QPixmap()
            pixmap.loadFromData(png)
            self.setWindowIcon(QIcon(pixmap))
            self._icon = QIcon(pixmap)
        else:
            self._icon = QIcon()

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        icon_label = QLabel()
        if png:
            icon_label.setPixmap(self._icon.pixmap(64, 64))
        title_box = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        subtitle = QLabel("CachyOS SMB credentials, mount points, fstab, and KDE launcher")
        subtitle.setStyleSheet("color: palette(mid);")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addWidget(icon_label)
        header.addLayout(title_box)
        header.addStretch(1)
        layout.addLayout(header)

        shares_box = QGroupBox("Network shares")
        grid = QGridLayout(shares_box)
        headers = ["", "Type", "Share", "Mount point"]
        for col, text in enumerate(headers):
            label = QLabel(text)
            label.setStyleSheet("font-weight: 600;")
            grid.addWidget(label, 0, col)
        self.rows = [ShareRow(spec) for spec in DEFAULT_SHARES]
        for row_i, row in enumerate(self.rows, start=1):
            grid.addWidget(row.enabled, row_i, 0)
            grid.addWidget(row.kind, row_i, 1)
            grid.addWidget(row.source, row_i, 2)
            grid.addWidget(row.mount, row_i, 3)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)
        layout.addWidget(shares_box)

        creds = QGroupBox("SMB credentials")
        form = QFormLayout(creds)
        self.username = QLineEdit(getpass.getuser())
        self.smb_password = QLineEdit()
        self.smb_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.domain = QLineEdit()
        self.domain.setPlaceholderText("optional, usually blank")
        self.creds_path = QLineEdit(str(default_credentials_path()))
        form.addRow("Username", self.username)
        form.addRow("Password", self.smb_password)
        form.addRow("Domain", self.domain)
        form.addRow("Credentials file", self.creds_path)
        layout.addWidget(creds)

        options = QGroupBox("Options")
        opt_layout = QVBoxLayout(options)
        self.map_home = QCheckBox(f"Map selected shares into {home_map_dir()}  (symlink per share)")
        self.map_home.setChecked(True)
        self.install_menu = QCheckBox("Install Share Creator into the KDE application menu")
        self.install_menu.setChecked(True)
        opt_layout.addWidget(self.map_home)
        opt_layout.addWidget(self.install_menu)
        layout.addWidget(options)

        buttons = QHBoxLayout()
        self.check_btn = QPushButton("Run checks")
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setDefault(True)
        self.check_btn.clicked.connect(self.on_check)
        self.apply_btn.clicked.connect(self.on_apply)
        buttons.addWidget(self.check_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_btn)
        layout.addLayout(buttons)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Checks and apply output appear here.")
        self.log.setMinimumHeight(220)
        layout.addWidget(self.log, 1)

        note = QLabel(
            "Apply asks for your sudo password. It is used once for pacman, folders, fstab, and mount — it is not saved."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

    def selected_shares(self) -> list[dict]:
        shares = []
        for row in self.rows:
            value = row.value()
            if value:
                shares.append(value)
        mounts = [s["mount"] for s in shares]
        if len(mounts) != len(set(mounts)):
            raise ValueError("Two shares use the same mount point.")
        return shares

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def on_check(self) -> None:
        self.log.clear()
        try:
            shares = self.selected_shares()
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        creds = Path(self.creds_path.text().strip()).expanduser()
        checks = post_install_checks(shares, creds, self.map_home.isChecked(), self.install_menu.isChecked())
        if not shares:
            self.append_log("No shares are checked.")
        failed = 0
        for ok, message in checks:
            self.append_log(("OK   " if ok else "FAIL ") + message)
            if not ok:
                failed += 1
        self.append_log("")
        self.append_log("All checks passed." if failed == 0 else f"{failed} check(s) failed.")

    def on_apply(self) -> None:
        try:
            shares = self.selected_shares()
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not self.username.text().strip() or not self.smb_password.text():
            QMessageBox.warning(self, APP_NAME, "SMB username and password are required.")
            return
        sudo_pw, ok = self.ask_sudo()
        if not ok:
            return
        if not sudo_pw:
            QMessageBox.warning(self, APP_NAME, "sudo password is required.")
            return
        payload = {
            "shares": shares,
            "username": self.username.text(),
            "smb_password": self.smb_password.text(),
            "domain": self.domain.text(),
            "creds_path": self.creds_path.text().strip(),
            "map_home": self.map_home.isChecked(),
            "install_menu": self.install_menu.isChecked(),
            "sudo_password": sudo_pw,
        }
        self.log.clear()
        self.apply_btn.setEnabled(False)
        self.check_btn.setEnabled(False)
        self.worker = ApplyWorker(payload)
        self.worker.log.connect(self.append_log)
        self.worker.finished_ok.connect(self.on_apply_done)
        self.worker.start()

    def ask_sudo(self) -> tuple[str, bool]:
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox

        dialog = QDialog(self)
        dialog.setWindowTitle("sudo password")
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel(f"Enter the sudo password for {getpass.getuser()} to apply share settings."))
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setFocus()
        box.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return "", False
        return edit.text(), True

    def on_apply_done(self, ok: bool, summary: str) -> None:
        self.apply_btn.setEnabled(True)
        self.check_btn.setEnabled(True)
        self.append_log("")
        self.append_log(summary)
        if ok:
            QMessageBox.information(self, APP_NAME, summary)
        else:
            QMessageBox.warning(self, APP_NAME, summary)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
