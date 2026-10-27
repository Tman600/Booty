import asyncio
import glob
import hashlib
import json
import os
import pwd
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time

import aiohttp
import decky

INSTALLER_LABEL = "Booty Installer"
OLD_INSTALLER_LABELS = ("Dual Boot Installer",)
# SteamOS's own partitions on the internal drive; never touched when looking for other OSes.
STEAMOS_PARTLABELS = {"esp", "efi-A", "efi-B", "rootfs-A", "rootfs-B", "var-A", "var-B", "home"}
# Pinned to a release and checked by hash, because it ends up on a boot partition.
UEFI_NTFS_URL = "https://raw.githubusercontent.com/pbatard/rufus/v4.11/res/uefi/uefi-ntfs.img"
UEFI_NTFS_SHA256 = "25d6bb709b8c952799c0af3de29356b33aa64fcbd9a98b3625ac0e806ee49c7b"
# Root-only working area on the same filesystem as the user's home, so finished files can be renamed into
# place. Root never writes into a user-owned directory by path while it's working.
STAGING = "/home/.booty"
CHUNK = 4 * 1024 * 1024
GB = 1000 ** 3
# Room left free on a games partition after shrinking it, so Steam can still update and save.
KEEP_MARGIN = 15 * GB

DEFAULT_SETTINGS = {
    "show_in_power_menu": True,
    "keep_steamos_default": True,
    "hidden": [],
    "names": {},
    # Partition-table IDs of drives Booty set up; startup repairs only touch these.
    "managed": [],
}

BOOT_ID = re.compile(r"^[0-9A-Fa-f]{4}$")

# Download sources. Each resolver returns (url, sha256 or None, filename).
CATALOG = [
    {
        "id": "bazzite",
        "name": "Bazzite (Steam Deck edition)",
        "blurb": "Gaming Linux with the same Steam Game Mode, plus a full desktop.",
        "write": "raw",
    },
    {
        "id": "fedora-kde",
        "name": "Fedora KDE",
        "blurb": "A general-purpose Linux desktop, same KDE as SteamOS desktop mode.",
        "write": "raw",
    },
    {
        "id": "ubuntu",
        "name": "Ubuntu LTS",
        "blurb": "The most common Linux desktop, with long-term support.",
        "write": "raw",
    },
    {
        "id": "windows",
        "name": "Windows 11",
        "blurb": "Paste a download link from Microsoft, or pick an ISO already on the Deck.",
        "write": "windows",
    },
]


def run(cmd, check=True, **kw):
    decky.logger.info("run: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(res.stderr or res.stdout).strip()}")
    return res


def http_session(**kw):
    # Decky's bundled Python has no CA store of its own, so use the system bundle.
    cafile = next((p for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem")
                   if os.path.exists(p)), None)
    ctx = ssl.create_default_context(cafile=cafile)
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                 headers={"User-Agent": "decky-booty"}, **kw)


def deck_ids():
    pw = pwd.getpwnam(decky.DECKY_USER)
    return pw.pw_uid, pw.pw_gid


# ---------- safe file handling ----------
# Booty runs as root, but anything that can reach Steam's local debug port can call it, and it reads and
# writes drives that may come from anywhere. So: root never follows a symlink it didn't make, never reads a
# file the user couldn't read themselves, and never writes outside the mount or folder it means to.

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_dir_fd(base, rel, create=False, uid=None, gid=None, mode=0o755):
    """fd for base/rel, walking one component at a time and refusing symlinks anywhere below base."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for comp in [c for c in rel.split("/") if c]:
            if comp in (".", ".."):
                raise ValueError("bad path")
            try:
                nfd = os.open(comp, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(comp, mode, dir_fd=fd)
                nfd = os.open(comp, DIR_FLAGS, dir_fd=fd)
                if uid is not None:
                    os.fchown(nfd, uid, gid)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def safe_write(base, rel, data, mode=0o644, uid=None, gid=None, xattr=None):
    """Write base/rel without following symlinks. An existing file or symlink at rel is replaced."""
    head, name = os.path.split(rel)
    dfd = open_dir_fd(base, head, create=True, uid=uid, gid=gid)
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        ffd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        with os.fdopen(ffd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            os.fchmod(ffd, mode)
            if uid is not None:
                os.fchown(ffd, uid, gid)
            if xattr:
                try:
                    os.setxattr(ffd, *xattr)
                except OSError:
                    pass
    finally:
        os.close(dfd)


def safe_read(base, rel, limit=1 << 20):
    """Read base/rel as text without following symlinks; None if it isn't a regular file."""
    head, name = os.path.split(rel)
    try:
        dfd = open_dir_fd(base, head)
    except (OSError, ValueError):
        return None
    try:
        ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    with os.fdopen(ffd, "rb") as f:
        if not stat.S_ISREG(os.fstat(ffd).st_mode):
            return None
        return f.read(limit).decode("utf-8", "replace")


def open_user_file(path):
    """Open a file the deck user owns (an ISO they picked), refusing symlinks and anything root-only."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != deck_ids()[0]:
        os.close(fd)
        raise ValueError("Booty can only use ISO files that belong to you")
    return fd


def user_dir_fd(rel):
    """fd for a folder in the user's home, created and owned by them, never a symlink."""
    uid, gid = deck_ids()
    fd = open_dir_fd(decky.DECKY_USER_HOME, rel, create=True, uid=uid, gid=gid)
    if os.fstat(fd).st_uid != uid:
        os.close(fd)
        raise RuntimeError(f"~/{rel} isn't owned by you")
    return fd


def staging_dir():
    os.makedirs(STAGING, mode=0o700, exist_ok=True)
    st = os.lstat(STAGING)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
        raise RuntimeError(f"{STAGING} isn't a root-owned folder")
    os.chmod(STAGING, 0o700)
    return STAGING


def publish_to_user(src, rel_dir, name):
    """Hand a finished file or folder from staging to the user: chown it, then rename it into ~/rel_dir/name."""
    uid, gid = deck_ids()
    run(["chown", "-R", "-P", f"{uid}:{gid}", src])
    dfd = user_dir_fd(rel_dir)
    try:
        os.rename(src, name, dst_dir_fd=dfd)
    finally:
        os.close(dfd)
    return os.path.join(decky.DECKY_USER_HOME, rel_dir, name)


def as_user(cmd):
    """Run a command as the deck user (for copying their own files)."""
    uid, gid = deck_ids()
    return ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--"] + cmd


def download_dir():
    """The user's Booty downloads folder (listing only; root never writes into it by path)."""
    os.close(user_dir_fd("Downloads/Booty"))
    return os.path.join(decky.DECKY_USER_HOME, "Downloads", "Booty")


def part_path(disk, n):
    # /dev/sda -> /dev/sda1, /dev/mmcblk0 or /dev/nvme0n1 -> ...p1
    return f"{disk}p{n}" if disk[-1].isdigit() else f"{disk}{n}"


class Plugin:
    job = None
    job_task = None
    cancel = False

    # ---------- settings ----------

    # Settings live in the root-only folder: they decide which drives Booty edits at startup.

    def _load_settings(self):
        txt = safe_read(staging_dir(), "settings.json")
        if txt is None:  # older versions kept them in Decky's settings folder
            txt = safe_read(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        try:
            loaded = json.loads(txt) if txt else {}
        except ValueError:
            loaded = {}
        s = dict(DEFAULT_SETTINGS)
        for k in DEFAULT_SETTINGS:
            if k in loaded and type(loaded[k]) is type(DEFAULT_SETTINGS[k]):
                s[k] = loaded[k]
        return s

    def _save_settings(self, s):
        safe_write(staging_dir(), "settings.json", json.dumps(s, indent=2), mode=0o600)

    async def get_settings(self):
        return self._load_settings()

    async def set_setting(self, key, value):
        s = self._load_settings()
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting {key}")
        ok = {
            "show_in_power_menu": lambda v: isinstance(v, bool),
            "keep_steamos_default": lambda v: isinstance(v, bool),
            "hidden": lambda v: isinstance(v, list) and all(isinstance(x, str) and BOOT_ID.match(x) for x in v),
            "names": lambda v: isinstance(v, dict) and all(BOOT_ID.match(k) and isinstance(n, str) and len(n) <= 40
                                                           for k, n in v.items()),
            "managed": lambda v: isinstance(v, list) and all(isinstance(x, str) and len(x) <= 64 for x in v),
        }[key]
        if not ok(value):
            raise ValueError(f"bad value for {key}")
        s[key] = value
        self._save_settings(s)
        if key == "keep_steamos_default" and value:
            self._ensure_steamos_first()
        return s

    # ---------- boot entries ----------

    def _boot_entries(self):
        out = run(["efibootmgr", "-v"]).stdout
        current = (re.search(r"^BootCurrent:\s*([0-9A-Fa-f]{4})", out, re.M) or [None, None])[1]
        order = (re.search(r"^BootOrder:\s*(\S+)", out, re.M) or [None, ""])[1].split(",")
        entries = []
        for m in re.finditer(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+([^\t\n]+)\t?([^\n]*)$", out, re.M):
            bid, active, label, path = m.group(1), m.group(2) == "*", m.group(3).strip(), m.group(4)
            low = path.lower()
            if "\\efi\\steamos\\" in low:
                kind = "steamos"
            elif label == INSTALLER_LABEL or label in OLD_INSTALLER_LABELS:
                kind = "installer"
            elif "\\efi\\microsoft\\" in low or label.lower().startswith("windows"):
                kind = "windows"
            elif "\\efi\\" not in low:
                kind = "firmware"
            else:
                kind = "os"
            entries.append({"id": bid, "label": label, "active": active, "kind": kind,
                            "current": bid == current, "order": order.index(bid) if bid in order else 99})
        return entries, current, order

    async def get_entries(self):
        entries, current, _ = self._boot_entries()
        s = self._load_settings()
        for e in entries:
            e["name"] = s["names"].get(e["id"], e["label"])
            e["switchable"] = e["kind"] in ("os", "windows") and not e["current"] and e["id"] not in s["hidden"]
        return {"entries": entries, "current": current}

    def _create_entry(self, disk, num, label, loader):
        """Add a firmware boot entry without letting it take over the boot order. Returns its id."""
        before, _, order = self._boot_entries()
        run(["efibootmgr", "-q", "-C", "-d", disk, "-p", str(num), "-L", label, "-l", loader])
        after = self._boot_entries()[0]
        new = [e["id"] for e in after if e["id"] not in {b["id"] for b in before}]
        if new:
            run(["efibootmgr", "-q", "-o", ",".join([b for b in order if b] + new)])
        return new[0] if new else None

    def _ensure_steamos_first(self):
        entries, _, order = self._boot_entries()
        steam = [e["id"] for e in entries if e["kind"] == "steamos"]
        if not steam or (order and order[0] == steam[0]):
            return
        new = [steam[0]] + [b for b in order if b and b != steam[0]]
        run(["efibootmgr", "-o", ",".join(new)])
        decky.logger.info("SteamOS moved back to the front of BootOrder")

    async def switch_to(self, boot_id):
        if not isinstance(boot_id, str) or not BOOT_ID.match(boot_id):
            raise ValueError("bad boot entry id")
        entries, _, _ = self._boot_entries()
        target = next((e for e in entries if e["id"] == boot_id), None)
        if not target or target["kind"] not in ("os", "windows", "installer"):
            raise ValueError("That boot entry can't be switched to")
        run(["efibootmgr", "-n", boot_id])
        decky.logger.info("BootNext=%s (%s), rebooting", boot_id, target["label"])
        asyncio.get_event_loop().call_later(1.5, lambda: subprocess.Popen(["systemctl", "reboot"]))
        return True

    # ---------- catalog / downloads ----------

    async def get_catalog(self):
        return CATALOG

    async def _resolve(self, session, os_id):
        if os_id == "bazzite":
            url = "https://download.bazzite.gg/bazzite-deck-stable-amd64.iso"
            async with session.get(url + "-CHECKSUM") as r:
                sha = (await r.text()).split()[0]
            return url, sha, "bazzite-deck-stable-amd64.iso"
        if os_id == "fedora-kde":
            async with session.get("https://fedoraproject.org/releases.json") as r:
                rel = await r.json(content_type=None)
            isos = [x for x in rel if x.get("arch") == "x86_64" and x.get("variant") == "KDE"
                    and x.get("link", "").endswith(".iso") and x.get("version", "").isdigit()]
            best = max(isos, key=lambda x: int(x["version"]))
            return best["link"], best.get("sha256"), best["link"].rsplit("/", 1)[1]
        if os_id == "ubuntu":
            async with session.get("https://changelogs.ubuntu.com/meta-release-lts") as r:
                meta = await r.text()
            dists = re.findall(r"^Dist:\s*(\S+)\n(?:.*\n)*?Supported:\s*1", meta, re.M)
            dist = dists[-1]
            base = f"https://releases.ubuntu.com/{dist}/"
            async with session.get(base + "SHA256SUMS") as r:
                sums = await r.text()
            rows = re.findall(r"^([0-9a-f]{64}) \*?(ubuntu-([\d.]+)-desktop-amd64\.iso)$", sums, re.M)
            sha, name, _ = max(rows, key=lambda x: [int(p) for p in x[2].split(".")])
            return base + name, sha, name
        raise ValueError(f"no download source for {os_id}")

    def _emit_job(self):
        return decky.emit("job", self.job)

    async def _set_job(self, **kw):
        self.job = {**(self.job or {}), **kw}
        await self._emit_job()

    async def get_job(self):
        return self.job

    async def cancel_job(self):
        self.cancel = True
        return True

    async def clear_job(self):
        if self.job and self.job.get("phase") in ("done", "error", "cancelled"):
            self.job = None
        return True

    def _busy(self):
        return self.job_task is not None and not self.job_task.done()

    async def start_download(self, os_id, url=None):
        if self._busy():
            raise RuntimeError("Another download or write is already running")
        self.cancel = False
        self.job = {"type": "download", "os": os_id, "phase": "starting", "done": 0, "total": 0, "message": ""}
        self.job_task = asyncio.get_event_loop().create_task(self._download(os_id, url))
        return self.job

    async def _download(self, os_id, url):
        tmp = None
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
            async with http_session(timeout=timeout) as session:
                if url:
                    if not url.startswith("https://"):
                        raise ValueError("The link must start with https://")
                    sha, name = None, re.sub(r"[^\w.\-]", "_", url.split("?")[0].rsplit("/", 1)[-1]) or "windows.iso"
                    if not name.lower().endswith(".iso"):
                        name += ".iso"
                else:
                    await self._set_job(phase="resolving", message="Finding the latest version")
                    url, sha, name = await self._resolve(session, os_id)
                if name in (".iso", "") or name.startswith("."):
                    name = "download.iso"
                tmp = os.path.join(staging_dir(), name + ".part")
                await self._set_job(phase="downloading", message=name)
                h = hashlib.sha256()
                async with session.get(url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    done, last = 0, 0.0
                    with open(tmp, "wb") as f:  # staging is root-only, so this path can't be redirected
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            if self.cancel:
                                raise asyncio.CancelledError()
                            f.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            if time.monotonic() - last > 0.5:
                                last = time.monotonic()
                                await self._set_job(done=done, total=total)
                await self._set_job(done=done, total=total or done, phase="verifying")
                got = h.hexdigest()
                if sha and got.lower() != sha.lower():
                    raise RuntimeError("The download is corrupted (checksum mismatch). Try again.")
                done_path = tmp[: -len(".part")]
                os.replace(tmp, done_path)
                dfd = user_dir_fd("Downloads/Booty")
                try:  # an older copy of the same ISO is replaced
                    os.unlink(name, dir_fd=dfd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(dfd)
                dest = publish_to_user(done_path, "Downloads/Booty", name)
                await self._set_job(phase="done", verified=bool(sha), sha256=got, file=dest,
                                    message="Verified against the official checksum" if sha else "Downloaded (no official checksum to compare)")
        except asyncio.CancelledError:
            await self._set_job(phase="cancelled", message="Cancelled")
        except Exception as e:
            decky.logger.exception("download failed")
            await self._set_job(phase="error", message=str(e))
        finally:
            if tmp and os.path.exists(tmp) and (self.job or {}).get("phase") != "done":
                os.remove(tmp)

    async def list_isos(self):
        out = []
        for d in (download_dir(), os.path.join(decky.DECKY_USER_HOME, "Downloads")):
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n)
                if n.lower().endswith(".iso") and os.path.isfile(p):
                    out.append({"path": p, "name": n, "size": os.path.getsize(p)})
        return out

    async def detect_iso(self, path):
        """'windows' if the ISO holds a Windows install image, otherwise 'raw' (bootable hybrid ISO)."""
        os.close(open_user_file(path))
        # Parse the ISO as the user, not root: it's untrusted input and they can read it anyway.
        res = run(as_user(["bsdtar", "-tf", path]), check=False)
        names = res.stdout.lower()
        if "sources/install.wim" in names or "sources/install.esd" in names:
            return "windows"
        return "raw"

    # ---------- drives ----------

    def _system_disks(self):
        disks = set()
        for mnt in ("/", "/home", "/var", "/esp", "/efi", "/boot"):
            src = run(["findmnt", "-no", "SOURCE", mnt], check=False).stdout.strip()
            if src.startswith("/dev/"):
                pk = run(["lsblk", "-no", "PKNAME", src], check=False).stdout.strip()
                disks.add("/dev/" + pk if pk else src)
        return disks

    async def list_drives(self):
        data = json.loads(run(["lsblk", "-J", "-b", "-o",
                               "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,VENDOR,LABEL,FSTYPE,MOUNTPOINTS"]).stdout)
        system = self._system_disks()
        drives = []
        for d in data.get("blockdevices", []):
            if d.get("type") != "disk" or d["name"].startswith(("zram", "loop")):
                continue
            if d["path"] in system:
                continue
            mounts = [m for c in (d.get("children") or [d]) for m in (c.get("mountpoints") or []) if m]
            has_games = any(os.path.isdir(os.path.join(m, "steamapps")) for m in mounts)
            kind = "SD card" if d["name"].startswith("mmcblk") else ("USB drive" if d.get("tran") == "usb" else "Drive")
            drives.append({"path": d["path"], "size": d["size"], "kind": kind,
                           "model": " ".join(x for x in (d.get("vendor"), d.get("model")) if x).strip() or kind,
                           "labels": [c.get("label") for c in (d.get("children") or []) if c.get("label")],
                           "mounts": mounts, "has_games": has_games})
        return drives

    # ---------- writing ----------

    async def start_write(self, iso_path, device, size, mode, avoid=None):
        if self._busy():
            raise RuntimeError("Another download or write is already running")
        if avoid and avoid == device:
            raise ValueError("That's the drive you're installing the OS onto. Pick a different one for the installer.")
        drives = {d["path"]: d for d in await self.list_drives()}
        d = drives.get(device)
        if not d:
            raise ValueError("That drive isn't available (it may be the system drive or was unplugged)")
        if int(size) != int(d["size"]):
            raise ValueError("The drive changed since it was selected. Pick it again.")
        try:
            fd = open_user_file(iso_path)
        except FileNotFoundError:
            raise ValueError("The ISO file is missing")
        iso_size = os.fstat(fd).st_size
        os.close(fd)
        if iso_size > d["size"]:
            raise ValueError("The drive is too small for this ISO")
        if mode not in ("raw", "windows"):
            raise ValueError("unknown write mode")
        self.cancel = False
        self.job = {"type": "write", "phase": "preparing", "device": device, "done": 0,
                    "total": iso_size, "message": "Unmounting the drive", "mode": mode}
        self.job_task = asyncio.get_event_loop().create_task(self._write(iso_path, device, mode))
        return self.job

    def _unmount_all(self, device):
        out = run(["lsblk", "-nrpo", "NAME,MOUNTPOINTS", device]).stdout
        for line in out.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip():
                run(["umount", "-l", parts[0]], check=False)

    async def _write(self, iso, device, mode):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._unmount_all, device)
            if mode == "raw":
                await self._write_raw(iso, device)
            else:
                await self._write_windows(iso, device)
            await self._set_job(phase="syncing", message="Finishing up")
            await loop.run_in_executor(None, lambda: run(["sync"]))
            run(["partprobe", device], check=False)
            await self._set_job(phase="done", message="The installer drive is ready")
        except asyncio.CancelledError:
            await self._set_job(phase="cancelled", message="Cancelled. The drive is incomplete; write it again before using it.")
        except Exception as e:
            decky.logger.exception("write failed")
            await self._set_job(phase="error", message=str(e))

    async def _write_raw(self, iso, device):
        src_fd = open_user_file(iso)
        total = os.fstat(src_fd).st_size
        await self._set_job(phase="writing", message="Writing the installer", total=total)
        loop = asyncio.get_event_loop()
        state = {"done": 0}

        def copy():
            with os.fdopen(src_fd, "rb") as src, open(device, "r+b", buffering=0) as dst:
                while True:
                    if self.cancel:
                        raise asyncio.CancelledError()
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    dst.write(buf)
                    state["done"] += len(buf)
                os.fsync(dst.fileno())

        fut = loop.run_in_executor(None, copy)
        while not fut.done():
            await self._set_job(done=state["done"])
            await asyncio.sleep(0.5)
        await fut
        await self._set_job(done=total)

    async def _write_windows(self, iso, device):
        loop = asyncio.get_event_loop()
        await self._set_job(phase="partitioning", message="Creating partitions")
        img = os.path.join(staging_dir(), "uefi-ntfs.img")
        if not os.path.exists(img):
            async with http_session() as s, s.get(UEFI_NTFS_URL) as r:
                r.raise_for_status()
                data = await r.read()
            if hashlib.sha256(data).hexdigest() != UEFI_NTFS_SHA256:
                raise RuntimeError("The UEFI:NTFS boot image didn't match its expected checksum")
            with open(img, "wb") as f:
                f.write(data)
        elif hashlib.sha256(open(img, "rb").read()).hexdigest() != UEFI_NTFS_SHA256:
            os.remove(img)
            raise RuntimeError("The cached UEFI:NTFS boot image was changed; try again")

        def partition():
            run(["wipefs", "-a", device])
            run(["sgdisk", "-Z", device], check=False)
            run(["sgdisk", "-n", "1:0:-2M", "-t", "1:0700", "-c", "1:WINSTALL",
                 "-n", "2:0:0", "-t", "2:EF00", "-c", "2:UEFI_NTFS", device])
            run(["partprobe", device])
            run(["udevadm", "settle"])
            run(["mkfs.exfat", "-n", "WINSTALL", part_path(device, 1)])
            with open(img, "rb") as src, open(part_path(device, 2), "r+b") as dst:
                dst.write(src.read())
                os.fsync(dst.fileno())

        await loop.run_in_executor(None, partition)
        mnt = tempfile.mkdtemp(prefix="booty-")
        try:
            uid, gid = deck_ids()
            run(["mount", "-t", "exfat", "-o", f"nosuid,nodev,noexec,uid={uid},gid={gid},umask=022",
                 part_path(device, 1), mnt])
            os.close(open_user_file(iso))
            await self._set_job(phase="writing", message="Copying Windows files", done=0)
            proc = await asyncio.create_subprocess_exec(*as_user(["bsdtar", "-xf", iso, "-C", mnt]),
                                                        stderr=asyncio.subprocess.PIPE)
            base = shutil.disk_usage(mnt).used
            while proc.returncode is None:
                if self.cancel:
                    proc.kill()
                    await proc.wait()
                    raise asyncio.CancelledError()
                await self._set_job(done=max(0, shutil.disk_usage(mnt).used - base))
                try:
                    await asyncio.wait_for(proc.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            if proc.returncode != 0:
                raise RuntimeError("Copying the Windows files failed: " + (await proc.stderr.read()).decode()[-300:])
            if not os.path.exists(os.path.join(mnt, "efi", "boot", "bootx64.efi")) and \
                    not os.path.exists(os.path.join(mnt, "EFI", "BOOT", "BOOTX64.EFI")):
                raise RuntimeError("That ISO doesn't look like a Windows installer (no EFI boot file)")
        finally:
            await loop.run_in_executor(None, lambda: (run(["sync"]), run(["umount", mnt], check=False)))
            os.rmdir(mnt)

    async def boot_installer(self, device):
        """Create a one-time boot entry for the installer drive's EFI partition and reboot into it."""
        if device not in {d["path"] for d in await self.list_drives()}:
            raise ValueError("That drive isn't available")
        out = json.loads(run(["lsblk", "-J", "-o", "NAME,PATH,FSTYPE,PARTTYPE,PARTLABEL", device]).stdout)
        parts = (out["blockdevices"][0].get("children") or [])
        esp = next((p for p in parts if (p.get("parttype") or "").lower() in
                    ("c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef")), None) \
            or next((p for p in parts if p.get("fstype") == "vfat"), None)
        if not esp:
            raise RuntimeError("Couldn't find a boot partition on that drive")
        num = open(f"/sys/class/block/{esp['name']}/partition").read().strip()
        for e in self._boot_entries()[0]:
            if e["kind"] == "installer":
                run(["efibootmgr", "-q", "-b", e["id"], "-B"], check=False)
        # Linux ISOs and the UEFI:NTFS partition both use the removable-media loader path.
        new = self._create_entry(device, num, INSTALLER_LABEL, "\\EFI\\BOOT\\BOOTX64.EFI")
        if not new:
            raise RuntimeError("Couldn't create the installer boot entry")
        return await self.switch_to(new)

    # ---------- finding installed OSes and restoring their boot entries ----------

    ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    LOADER_NAMES = {"ubuntu": "Ubuntu", "fedora": "Fedora", "bazzite": "Bazzite", "debian": "Debian",
                    "opensuse": "openSUSE", "arch": "Arch Linux", "microsoft": "Windows Boot Manager"}

    def _other_esps(self):
        data = json.loads(run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,PARTTYPE,PARTUUID,PKNAME,MOUNTPOINTS"]).stdout)
        system = self._system_disks()
        out = []

        def walk(nodes):
            for n in nodes:
                if n.get("type") == "part" and (n.get("parttype") or "").lower() == self.ESP_GUID \
                        and ("/dev/" + (n.get("pkname") or "")) not in system:
                    out.append(n)
                walk(n.get("children") or [])

        walk(data.get("blockdevices", []))
        return out

    def _loaders_on(self, esp):
        """[(label, '\\EFI\\dir\\file.efi')] for each OS boot loader on an EFI partition."""
        found = []
        mnt, ours = self._mount_any({**esp, "fstype": "vfat"})
        try:
            efi = next((os.path.join(mnt, d) for d in os.listdir(mnt)
                        if d.lower() == "efi" and not os.path.islink(os.path.join(mnt, d))), None)
            for d in sorted(os.listdir(efi)) if efi else []:
                if d.lower() == "boot" or os.path.islink(os.path.join(efi, d)) or not re.match(r"^[\w.\- ]{1,32}$", d):
                    continue
                sub = os.path.join(efi, d)
                if d.lower() == "microsoft":
                    sub = next((os.path.join(sub, x) for x in os.listdir(sub) if x.lower() == "boot"), sub)
                files = {f.lower(): f for f in os.listdir(sub)} if os.path.isdir(sub) else {}
                for want in ("shimx64.efi", "grubx64.efi", "bootmgfw.efi"):
                    if want in files:
                        rel = os.path.relpath(os.path.join(sub, files[want]), mnt).replace("/", "\\")
                        found.append((self.LOADER_NAMES.get(d.lower(), d), "\\" + rel))
                        break
        finally:
            if ours:
                self._umount(mnt)
        return found

    def _repair_entries_sync(self, only_managed=False):
        managed = set(self._load_settings()["managed"])
        out = run(["efibootmgr", "-v"]).stdout.lower()
        existing = set(re.findall(r"hd\(\d+,gpt,([0-9a-f-]+),[^)]*\)/?(\\[^\s]+)", out))
        found, created = [], []
        for esp in self._other_esps():
            if only_managed and self._disk_id("/dev/" + esp["pkname"]) not in managed:
                continue
            try:
                loaders = self._loaders_on(esp)
            except RuntimeError:
                continue
            num = open(f"/sys/class/block/{esp['name']}/partition").read().strip()
            disk = "/dev/" + esp["pkname"]
            if loaders and not only_managed:
                self._remember_managed(disk)  # the user asked Booty to look after this drive
            for label, loader in loaders:
                found.append({"name": label, "partition": esp["path"], "loader": loader})
                if (esp["partuuid"].lower(), loader.lower()) in existing:
                    continue
                self._create_entry(disk, num, label, loader)
                created.append(label)
                self._remember_managed(disk)
                decky.logger.info("recreated boot entry %s -> %s %s", label, esp["path"], loader)
        return {"found": found, "created": created}

    async def repair_entries(self):
        def work():
            res = self._repair_entries_sync()
            res["tidied"] = [t["name"] for t in self._tidy_grub_sync() if t["ok"]]
            return res
        return await asyncio.get_event_loop().run_in_executor(None, work)

    # ---------- tidy GRUB in newly installed Linux OSes ----------
    # Linux installers run os-prober, find SteamOS's A/B root partitions, add "SteamOS Holo" entries that
    # can't boot (SteamOS starts through its own loader), and switch GRUB to a visible menu. Booty turns
    # os-prober off, hides the menu again, and adds one working SteamOS entry that chainloads SteamOS's loader.

    GRUB_SETTINGS = {"GRUB_DISABLE_OS_PROBER": "true", "GRUB_TIMEOUT": "0", "GRUB_TIMEOUT_STYLE": "hidden"}
    GRUB_ENTRY_FILE = "etc/grub.d/39_booty_steamos"

    def _steamos_esp_uuid(self):
        for p in self._parts_of_system_disk():
            if p.get("partlabel") == "esp" and p.get("fstype") == "vfat":
                return p.get("uuid")
        return None

    def _parts_of_system_disk(self):
        data = json.loads(run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,PARTLABEL,UUID,PKNAME"]).stdout)
        system = self._system_disks()
        out = []

        def walk(nodes):
            for n in nodes:
                if n.get("type") == "part" and ("/dev/" + (n.get("pkname") or "")) in system:
                    out.append(n)
                walk(n.get("children") or [])

        walk(data.get("blockdevices", []))
        return out

    OSPROBER_BEGIN = "### BEGIN /etc/grub.d/30_os-prober ###"
    OSPROBER_END = "### END /etc/grub.d/30_os-prober ###"
    BOOTY_BEGIN = "### BEGIN /etc/grub.d/39_booty_steamos ###"
    BOOTY_END = "### END /etc/grub.d/39_booty_steamos ###"

    @staticmethod
    def _grub_root(mnt):
        """Path (relative to mnt) of the OS that holds /etc/default/grub; handles btrfs subvolumes."""
        if os.path.lexists(os.path.join(mnt, "ostree")) or glob.glob(os.path.join(mnt, "*", "ostree")):
            return None  # Bazzite/Silverblue manage GRUB themselves
        for rel in [""] + sorted(os.listdir(mnt)):
            if rel and os.path.islink(os.path.join(mnt, rel)):
                continue
            if safe_read(mnt, os.path.join(rel, "etc/default/grub")) is not None:
                return rel
        return None

    def _steamos_menuentry(self, esp_uuid):
        return "\n".join([
            "menuentry 'SteamOS' --class steamos {",
            "    insmod part_gpt",
            "    insmod fat",
            "    insmod chain",
            f"    search --no-floppy --fs-uuid --set=root {esp_uuid}",
            "    chainloader /EFI/steamos/steamcl.efi",
            "}",
        ])

    def _grub_plan(self, mnt, root, esp_uuid):
        """{relative path: new text} for every file that needs changing (empty when already tidy)."""
        changes = {}
        dpath = os.path.join(root, "etc/default/grub")
        text = safe_read(mnt, dpath)
        new = text
        for key, val in self.GRUB_SETTINGS.items():
            line = f"{key}={val}"
            if re.search(rf"^\s*#?\s*{key}=.*$", new, re.M):
                new = re.sub(rf"^\s*#?\s*{key}=.*$", line, new, count=1, flags=re.M)
            else:
                new = new.rstrip("\n") + f"\n{line}\n"
        if new != text:
            changes[dpath] = new
        # So the OS's own update-grub keeps the SteamOS entry from now on.
        entry = "\n".join(["#!/bin/sh", "# Added by Booty: start SteamOS through its own boot loader.",
                           "exec tail -n +4 $0", self._steamos_menuentry(esp_uuid), ""])
        epath = os.path.join(root, self.GRUB_ENTRY_FILE)
        if safe_read(mnt, epath) != entry:
            changes[epath] = entry
        # Fix the current grub.cfg directly (no code from the drive is run).
        for cfg in ("boot/grub/grub.cfg", "boot/grub2/grub.cfg"):
            cpath = os.path.join(root, cfg)
            cur = safe_read(mnt, cpath, limit=4 << 20)
            if cur is None:
                continue
            fixed = cur
            if self.OSPROBER_BEGIN in fixed and self.OSPROBER_END in fixed:
                a = fixed.index(self.OSPROBER_BEGIN) + len(self.OSPROBER_BEGIN)
                b = fixed.index(self.OSPROBER_END)
                fixed = fixed[:a] + "\n" + fixed[b:]
            if self.BOOTY_BEGIN not in fixed:
                block = f"{self.BOOTY_BEGIN}\n{self._steamos_menuentry(esp_uuid)}\n{self.BOOTY_END}\n\n"
                anchor = "### BEGIN /etc/grub.d/40_custom ###"
                fixed = fixed.replace(anchor, block + anchor, 1) if anchor in fixed else fixed + "\n" + block
            if fixed != cur:
                changes[cpath] = fixed
        return changes

    def _disk_id(self, disk):
        return run(["lsblk", "-dno", "PTUUID", disk], check=False).stdout.strip().lower()

    def _remember_managed(self, disk):
        pid = self._disk_id(disk)
        s = self._load_settings()
        if pid and pid not in s["managed"]:
            s["managed"] = s["managed"] + [pid]
            self._save_settings(s)

    def _tidy_grub_sync(self, only_managed=False):
        esp_uuid = self._steamos_esp_uuid()
        if not esp_uuid or not re.match(r"^[0-9A-Fa-f-]{4,36}$", esp_uuid):
            return []
        managed = set(self._load_settings()["managed"])
        results = []
        for part in self._other_os_partitions():
            if part.get("fstype") not in ("ext4", "btrfs", "xfs"):
                continue
            disk = "/dev/" + (part.get("pkname") or "")
            if only_managed and self._disk_id(disk) not in managed:
                continue
            try:
                mnt, ours = self._peek(part)
            except RuntimeError:
                continue
            try:
                root = self._grub_root(mnt)
                found = self._identify(mnt)
                plan = self._grub_plan(mnt, root, esp_uuid) if root is not None else {}
            finally:
                if ours:
                    self._umount(mnt)
            if not plan:
                continue
            name = found[1] if found else part["path"]
            res = {"name": name, "partition": part["path"], "ok": False, "detail": ""}
            try:
                mnt = self._open_for_work(part)
            except RuntimeError as e:
                res["detail"] = str(e)
                results.append(res)
                continue
            try:
                root = self._grub_root(mnt)
                plan = self._grub_plan(mnt, root, esp_uuid)
                dpath = os.path.join(root, "etc/default/grub")
                if dpath in plan and safe_read(mnt, dpath + ".booty-backup") is None:
                    safe_write(mnt, dpath + ".booty-backup", safe_read(mnt, dpath))
                for rel, text in plan.items():
                    if rel.endswith(self.GRUB_ENTRY_FILE):
                        mode = 0o755
                    elif rel.endswith("grub.cfg"):
                        mode = 0o600
                    else:
                        mode = 0o644
                    safe_write(mnt, rel, text, mode=mode, uid=0, gid=0)
                res.update(ok=True, detail="Boot menu hidden; SteamOS entry added")
                self._remember_managed(disk)
                decky.logger.info("tidied GRUB for %s on %s (%s)", name, part["path"], ", ".join(plan))
            except Exception as e:
                res["detail"] = str(e)
                decky.logger.exception("GRUB tidy failed for %s", part["path"])
            finally:
                run(["sync"], check=False)
                self._umount(mnt)
            results.append(res)
        return results

    # ---------- USB backups: keep a stick's files before it becomes an installer ----------

    @staticmethod
    def _backup_root():
        """~/Booty Backups (for listing). Root only writes there by renaming finished folders in."""
        os.close(user_dir_fd("Booty Backups"))
        return os.path.join(decky.DECKY_USER_HOME, "Booty Backups")

    def _data_parts(self, device):
        return [p for p in self._parts(device) if p.get("fstype") in ("exfat", "vfat", "ntfs", "ext4", "btrfs", "xfs")]

    def _mount_any(self, part, rw=False):
        """Use an existing mount if there is one, else mount it ourselves. Returns (path, mounted_by_us)."""
        existing = [m for m in (part.get("mountpoints") or []) if m]
        if existing:
            return existing[0], False
        return self._mount(part, rw), True

    async def inspect_drive(self, device):
        drives = {d["path"]: d for d in await self.list_drives()}
        if device not in drives:
            raise ValueError("That drive isn't available")

        def work():
            parts, total = [], 0
            for p in self._data_parts(device):
                try:
                    mnt, ours = self._mount_any(p)
                except RuntimeError:
                    continue
                try:
                    st = os.statvfs(mnt)
                    used = (st.f_blocks - st.f_bfree) * st.f_frsize
                    names = sorted(n for n in os.listdir(mnt) if n not in ("System Volume Information", "lost+found"))
                    files = sum(len(fs) for _, _, fs in os.walk(mnt))
                finally:
                    if ours:
                        self._umount(mnt)
                parts.append({"path": p["path"], "label": p.get("label") or "", "fstype": p["fstype"],
                              "used": used, "files": files, "top": names[:8]})
                total += used if files else 0
            return {"parts": parts, "used": total, "files": sum(p["files"] for p in parts),
                    "free_home": shutil.disk_usage(decky.DECKY_USER_HOME).free}

        return await asyncio.get_event_loop().run_in_executor(None, work)

    async def _rsync(self, src, dst, total, base_done, extra=(), user=False):
        """Copy src/ into dst/ with rsync (symlinks are skipped, not followed), reporting progress."""
        cmd = ["rsync", "-rt", "--no-links", "--info=progress2", "--no-inc-recursive", *extra, src.rstrip("/") + "/", dst]
        proc = await asyncio.create_subprocess_exec(
            *(as_user(cmd) if user else cmd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buf = b""
        last = 0.0
        while True:
            if self.cancel:
                proc.kill()
                await proc.wait()
                raise asyncio.CancelledError()
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf = (buf + chunk)[-4096:]
            m = re.findall(rb"([\d,]+)\s+\d+%", buf)
            if m and time.monotonic() - last > 0.5:
                last = time.monotonic()
                await self._set_job(done=base_done + int(m[-1].replace(b",", b"")), total=total)
        await proc.wait()
        if proc.returncode not in (0, 24):  # 24: some source files vanished, harmless
            raise RuntimeError("Copying failed: " + (await proc.stderr.read()).decode()[-300:])

    async def start_backup(self, device, size):
        if self._busy():
            raise RuntimeError("Another job is already running")
        drives = {d["path"]: d for d in await self.list_drives()}
        d = drives.get(device)
        if not d or int(size) != int(d["size"]):
            raise ValueError("That drive isn't available or changed. Pick it again.")
        info = await self.inspect_drive(device)
        if info["used"] > info["free_home"] - 5 * GB:
            raise RuntimeError(f"Not enough room on the Deck: the drive has {info['used'] // GB} GB of files and "
                               f"{info['free_home'] // GB} GB is free")
        self.cancel = False
        self.job = {"type": "backup", "phase": "copying", "device": device, "done": 0, "total": info["used"],
                    "message": "Copying files to the Deck"}
        self.job_task = asyncio.get_event_loop().create_task(self._backup(d, info))
        return self.job

    async def _backup(self, d, info):
        label = next((p["label"] for p in info["parts"] if p["label"]), "") or d["model"]
        name = re.sub(r"[^\w\- ]", "_", f"{label} {time.strftime('%Y-%m-%d %H%M%S')}").strip("_ .")[:80] or "backup"
        self._backup_root()
        dest = os.path.join(staging_dir(), "backup-" + name)
        loop = asyncio.get_event_loop()
        try:
            os.makedirs(dest)
            # Copy from a private read-only mount, so nothing can change the files while root reads them.
            await loop.run_in_executor(None, self._unmount_all, d["path"])
            done, saved = 0, []
            many = len([p for p in info["parts"] if p["files"]]) > 1
            for p in info["parts"]:
                if not p["files"]:
                    continue
                part = next(x for x in self._data_parts(d["path"]) if x["path"] == p["path"])
                mnt, ours = await loop.run_in_executor(None, lambda: (self._mount(part, rw=False), True))
                sub = re.sub(r"[^\w\- ]", "_", p["label"] or os.path.basename(p["path"])) if many else ""
                target = os.path.join(dest, sub) if sub else dest
                os.makedirs(target, exist_ok=True)
                try:
                    await self._rsync(mnt, target, info["used"], done)
                finally:
                    if ours:
                        await loop.run_in_executor(None, self._umount, mnt)
                done += p["used"]
                saved.append({"dir": sub, "label": p["label"], "fstype": p["fstype"]})
            manifest = {"name": name, "model": d["model"], "kind": d["kind"], "size": d["size"],
                        "parts": saved, "bytes": info["used"], "files": info["files"],
                        "created": time.strftime("%Y-%m-%d %H:%M")}
            with open(os.path.join(dest, ".booty-backup.json"), "w") as f:
                json.dump(manifest, f, indent=2)
            await loop.run_in_executor(None, publish_to_user, dest, "Booty Backups", name)
            await self._set_job(phase="done", done=info["used"], backup=name,
                                message=f"Saved {info['files']} files to Booty Backups/{name}")
        except asyncio.CancelledError:
            run(["rm", "-rf", "--", dest], check=False)
            await self._set_job(phase="cancelled", message="Backup cancelled; nothing on the drive was changed")
        except Exception as e:
            decky.logger.exception("backup failed")
            await self._set_job(phase="error", message=f"Backup failed, so nothing was erased: {e}")

    async def list_backups(self):
        """Backups by folder name; the manifest only supplies display details."""
        out = []
        root = self._backup_root()
        for n in sorted(os.listdir(root), reverse=True):
            if n.startswith(".") or os.path.islink(os.path.join(root, n)):
                continue
            txt = safe_read(root, os.path.join(n, ".booty-backup.json"), limit=64 << 10)
            if txt is None:
                continue
            try:
                m = json.loads(txt)
            except ValueError:
                continue
            out.append({"name": n, "model": str(m.get("model", ""))[:80], "kind": str(m.get("kind", ""))[:20],
                        "size": int(m.get("size") or 0), "bytes": int(m.get("bytes") or 0),
                        "files": int(m.get("files") or 0), "created": str(m.get("created", ""))[:20],
                        "label": str(next((x.get("label") for x in m.get("parts") or [] if x.get("label")), ""))})
        return out

    async def start_restore(self, name, device, size):
        if self._busy():
            raise RuntimeError("Another job is already running")
        backups = {b["name"]: b for b in await self.list_backups()}
        b = backups.get(name)
        if not b:
            raise ValueError("That backup is missing")
        drives = {d["path"]: d for d in await self.list_drives()}
        d = drives.get(device)
        if not d or int(size) != int(d["size"]):
            raise ValueError("That drive isn't available or changed. Pick it again.")
        if b["bytes"] > d["size"] * 0.97:
            raise ValueError("That drive is too small for this backup")
        self.cancel = False
        self.job = {"type": "restore", "phase": "formatting", "device": device, "done": 0, "total": b["bytes"],
                    "message": "Formatting the drive"}
        self.job_task = asyncio.get_event_loop().create_task(self._restore(b, d))
        return self.job

    async def _restore(self, b, d):
        loop = asyncio.get_event_loop()
        label = re.sub(r"[^A-Za-z0-9_\- ]", "", b.get("label") or "")[:11] or "USB"
        device = d["path"]

        def fmt():
            self._unmount_all(device)
            run(["wipefs", "-a", device])
            run(["sgdisk", "-Z", device], check=False)
            run(["sgdisk", "-o", "-n", "1:0:0", "-t", "1:0700", "-c", f"1:{label}", device])
            run(["partprobe", device], check=False)
            run(["udevadm", "settle"], check=False)
            run(["mkfs.exfat", "-n", label, part_path(device, 1)])

        mnt = None
        try:
            await loop.run_in_executor(None, fmt)
            uid, gid = deck_ids()
            mnt = tempfile.mkdtemp(prefix="booty-restore-")
            await loop.run_in_executor(None, lambda: run(
                ["mount", "-t", "exfat", "-o", f"nosuid,nodev,noexec,uid={uid},gid={gid},umask=022",
                 part_path(device, 1), mnt]))
            await self._set_job(phase="copying", message="Copying your files back")
            src = os.path.join(self._backup_root(), b["name"])
            # The backup lives in the user's home, so copy it as the user: root never reads it.
            await self._rsync(src, mnt, b["bytes"], 0, extra=("--exclude", ".booty-backup.json"), user=True)
            await self._set_job(phase="syncing", message="Finishing up")
            await loop.run_in_executor(None, lambda: run(["sync"]))
            await self._set_job(phase="done", done=b["bytes"], message=f"Restored {b['files']} files to {d['model']}")
        except asyncio.CancelledError:
            await self._set_job(phase="cancelled", message="Restore cancelled. The backup is still on the Deck.")
        except Exception as e:
            decky.logger.exception("restore failed")
            await self._set_job(phase="error", message=f"Restore failed (the backup is still on the Deck): {e}")
        finally:
            if mnt:
                await loop.run_in_executor(None, self._umount, mnt)

    # ---------- install target: make room for the new OS ----------

    def _parts(self, disk):
        data = json.loads(run(["lsblk", "-J", "-b", "-o",
                               "NAME,PATH,TYPE,START,SIZE,FSTYPE,PARTTYPE,PARTUUID,PARTLABEL,LABEL,MOUNTPOINTS", disk]).stdout)
        return [c for c in (data["blockdevices"][0].get("children") or []) if c.get("type") == "part"]

    def _games_partition(self, disk):
        """The drive's only partition, if it's an ext4 Steam library we can shrink in place."""
        parts = self._parts(disk)
        if len(parts) != 1 or parts[0].get("fstype") != "ext4":
            return None
        return parts[0]

    @staticmethod
    def _used_bytes(part):
        mounts = [m for m in (part.get("mountpoints") or []) if m]
        if mounts:
            st = os.statvfs(mounts[0])
            return (st.f_blocks - st.f_bfree) * st.f_frsize
        out = run(["dumpe2fs", "-h", part["path"]], check=False).stdout
        bc = int((re.search(r"^Block count:\s+(\d+)", out, re.M) or [0, 0])[1])
        free = int((re.search(r"^Free blocks:\s+(\d+)", out, re.M) or [0, 0])[1])
        bs = int((re.search(r"^Block size:\s+(\d+)", out, re.M) or [0, 4096])[1])
        return (bc - free) * bs

    async def list_install_targets(self):
        targets = []
        for d in await self.list_drives():
            games = self._games_partition(d["path"])
            t = {**d, "modes": ["erase"], "max_erase": d["size"] - GB, "max_keep": 0, "used": 0}
            if games:
                used = self._used_bytes(games)
                t["used"] = used
                t["max_keep"] = max(0, d["size"] - used - KEEP_MARGIN)
                if t["max_keep"] >= 20 * GB:
                    t["modes"].insert(0, "keep")
            targets.append(t)
        return targets

    def _shrink_ext4_partition(self, disk, part, new_bytes, progress):
        """Shrink an unmounted ext4 partition (filesystem first, then the partition) to new_bytes.
        The partition keeps its start, type, GUID and name, so nothing that refers to it changes."""
        new_bytes -= new_bytes % (1024 * 1024)
        progress("Checking the filesystem")
        res = run(["e2fsck", "-f", "-y", part["path"]], check=False)
        if res.returncode not in (0, 1):
            raise RuntimeError("The filesystem check found problems, so Booty stopped before changing anything")
        progress("Shrinking (this can take several minutes; keep the Deck plugged in)")
        run(["resize2fs", part["path"], f"{new_bytes // 1024}K"])
        sector = int(open(f"/sys/block/{os.path.basename(disk)}/queue/logical_block_size").read())
        start = int(part["start"])
        end = start + new_bytes // sector - 1
        progress("Updating the partition table")
        run(["sgdisk", "-d", "1", "-n", f"1:{start}:{end}", "-t", f"1:{part['parttype']}",
             "-u", f"1:{part['partuuid']}", "-c", f"1:{part.get('partlabel') or 'primary'}", disk])
        run(["partprobe", disk], check=False)
        run(["udevadm", "settle"], check=False)
        res = run(["e2fsck", "-f", "-n", part["path"]], check=False)
        if res.returncode != 0:
            raise RuntimeError("The filesystem check after shrinking reported a problem: " + res.stdout[-300:])

    def _erase_layout(self, disk, os_bytes, progress):
        """New GPT: an ext4 storage partition first (if any space is left), then free space for the OS."""
        progress("Erasing the drive")
        run(["wipefs", "-a", disk])
        run(["sgdisk", "-Z", disk], check=False)
        run(["sgdisk", "-o", disk])
        size = int(open(f"/sys/block/{os.path.basename(disk)}/size").read()) * 512
        storage = size - os_bytes - 2 * 1024 * 1024
        if storage >= 4 * GB:
            progress("Creating the storage partition")
            run(["sgdisk", "-n", f"1:0:+{storage // (1024 * 1024)}M", "-t", "1:8300", "-c", "1:Storage", disk])
            run(["partprobe", disk], check=False)
            run(["udevadm", "settle"], check=False)
            run(["mkfs.ext4", "-F", "-L", "Storage", part_path(disk, 1)])
        run(["partprobe", disk], check=False)

    @staticmethod
    def _steamos_event(action, part_path_):
        # SteamOS's own hook: unmounts/mounts the card and tells Steam the library went away or came back.
        script = "/usr/lib/hwsupport/block-device-event.sh"
        if os.path.exists(script):
            run([script, action, os.path.basename(part_path_)], check=False)

    async def start_prepare(self, device, size, mode, os_bytes, installer_device=None):
        if self._busy():
            raise RuntimeError("Another job is already running")
        targets = {t["path"]: t for t in await self.list_install_targets()}
        t = targets.get(device)
        if not t or int(size) != int(t["size"]):
            raise ValueError("That drive isn't available or changed. Pick it again.")
        if installer_device and installer_device == device:
            raise ValueError("The installer can't go on the same drive you're installing to")
        os_bytes = int(os_bytes)
        if mode not in t["modes"]:
            raise ValueError("That option isn't available for this drive")
        limit = t["max_keep"] if mode == "keep" else t["max_erase"]
        if os_bytes < 20 * GB or os_bytes > limit:
            raise ValueError(f"Pick between 20 GB and {limit // GB} GB")
        self.cancel = False
        self.job = {"type": "prepare", "phase": "preparing", "device": device, "done": 0, "total": 0,
                    "message": "Getting the drive ready", "os_bytes": os_bytes, "mode": mode}
        self.job_task = asyncio.get_event_loop().create_task(self._prepare(device, mode, os_bytes))
        return self.job

    async def _prepare(self, device, mode, os_bytes):
        loop = asyncio.get_event_loop()
        msgs = []

        def progress(m):
            msgs.append(m)

        def work():
            if mode == "keep":
                part = self._games_partition(device)
                self._steamos_event("remove", part["path"])
                self._unmount_all(device)
                if any(m for m in (self._games_partition(device).get("mountpoints") or []) if m):
                    raise RuntimeError("The SD card is busy. Quit any game running from it and try again.")
                try:
                    self._shrink_ext4_partition(device, part, int(part["size"]) - os_bytes, progress)
                finally:
                    self._steamos_event("add", part["path"])
            else:
                for p in self._parts(device):
                    if any(m for m in (p.get("mountpoints") or []) if m):
                        self._steamos_event("remove", p["path"])
                self._unmount_all(device)
                self._erase_layout(device, os_bytes, progress)
                for p in self._parts(device):
                    self._steamos_event("add", p["path"])

        try:
            fut = loop.run_in_executor(None, work)
            while not fut.done():
                if msgs:
                    await self._set_job(phase="working", message=msgs[-1])
                await asyncio.sleep(0.5)
            await fut
            await loop.run_in_executor(None, self._remember_managed, device)
            await self._set_job(phase="done", message=f"{os_bytes // GB} GB of free space is ready for the new OS")
        except Exception as e:
            decky.logger.exception("prepare failed")
            await self._set_job(phase="error", message=str(e))

    # ---------- "Back to SteamOS" buttons inside the other OSes ----------

    def _other_os_partitions(self):
        """Unmounted partitions that could hold another OS (never SteamOS's own partitions)."""
        data = json.loads(run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,PARTLABEL,MOUNTPOINTS,PKNAME"]).stdout)
        system = self._system_disks()
        out = []

        def walk(nodes):
            for n in nodes:
                mounts = [m for m in (n.get("mountpoints") or []) if m]
                auto = len(mounts) == 1 and mounts[0].startswith("/run/media/")
                if n.get("type") == "part" and n.get("fstype") in ("ntfs", "ext4", "btrfs", "xfs") \
                        and (not mounts or auto):
                    on_system = ("/dev/" + (n.get("pkname") or "")) in system
                    if not (on_system and (n.get("partlabel") or "") in STEAMOS_PARTLABELS):
                        out.append({**n, "automount": mounts[0] if auto else None})
                walk(n.get("children") or [])

        walk(data.get("blockdevices", []))
        return out

    def _mount(self, part, rw):
        # Mount inside a root-only folder so nothing else can reach the files while Booty works on them.
        mnt = os.path.join(tempfile.mkdtemp(prefix="booty-os-"), "m")
        os.mkdir(mnt, 0o700)
        fs = part["fstype"]
        opts = ["rw" if rw else "ro", "nosuid", "nodev", "noexec"]
        if fs == "btrfs":
            opts.append("subvolid=5")  # top level, so every subvolume (root, home, @home...) is visible
        cmd = ["mount", "-o", ",".join(opts)]
        if fs == "ntfs":
            cmd += ["-t", "ntfs3"]
        elif fs in ("exfat", "vfat"):
            cmd += ["-t", fs]
        res = run(cmd + [part["path"], mnt], check=False)
        if res.returncode != 0:
            self._rm_mountpoint(mnt)
            raise RuntimeError((res.stderr or res.stdout).strip() or "mount failed")
        return mnt

    @staticmethod
    def _rm_mountpoint(mnt):
        for d in (mnt, os.path.dirname(mnt)) if os.path.basename(mnt) == "m" else (mnt,):
            try:
                os.rmdir(d)
            except OSError:
                pass

    def _umount(self, mnt):
        run(["umount", mnt], check=False)
        self._rm_mountpoint(mnt)

    def _peek(self, part):
        """Read-only view of a partition: SteamOS's automount if it has one, else a private mount.
        Returns (path, mounted_by_us)."""
        if part.get("automount"):
            return part["automount"], False
        return self._mount(part, rw=False), True

    def _open_for_work(self, part):
        """Private read-write mount. If SteamOS automounted it (it does that to Linux roots on the SD card),
        release that first through SteamOS's own hook so Steam forgets it; it stays unmounted afterwards."""
        if part.get("automount"):
            self._steamos_event("remove", part["path"])
            still = run(["findmnt", "-rno", "TARGET", part["path"]], check=False).stdout.strip()
            if still:
                run(["umount", part["path"]], check=False)
            if run(["findmnt", "-rno", "TARGET", part["path"]], check=False).stdout.strip():
                raise RuntimeError(f"{part['path']} is in use")
        return self._mount(part, rw=True)

    @staticmethod
    def _identify(mnt):
        """('windows', 'Windows') or ('linux', pretty name) or None."""
        if os.path.isdir(os.path.join(mnt, "Windows", "System32")):
            return "windows", "Windows"
        pats = ["etc/os-release", "usr/lib/os-release", "*/etc/os-release", "*/usr/lib/os-release",
                "ostree/deploy/*/deploy/*/usr/lib/os-release", "*/ostree/deploy/*/deploy/*/usr/lib/os-release"]
        for p in pats:
            for f in glob.glob(os.path.join(mnt, p)):
                rel = os.path.relpath(f, mnt)
                txt = safe_read(mnt, rel)
                if txt is None:  # Ubuntu's etc/os-release is a symlink; follow it only inside the mount
                    real = os.path.realpath(f)
                    if real.startswith(os.path.realpath(mnt) + os.sep):
                        txt = safe_read(mnt, os.path.relpath(real, os.path.realpath(mnt)))
                if txt is None:
                    continue
                m = re.search(r'^PRETTY_NAME="?([^"\n]+)', txt, re.M) or re.search(r'^NAME="?([^"\n]+)', txt, re.M)
                return "linux", (m.group(1) if m else "Linux")
        return None

    @staticmethod
    def _linux_homes(mnt):
        pats = ["home/*", "@home/*", "*/home/*", "ostree/deploy/*/var/home/*", "*/ostree/deploy/*/var/home/*",
                "var/home/*", "*/var/home/*"]
        homes = {}
        for p in pats:
            for d in glob.glob(os.path.join(mnt, p)):
                rel = os.path.relpath(d, mnt)
                try:
                    fd = open_dir_fd(mnt, rel)
                except (OSError, ValueError):
                    continue  # a symlink somewhere in the path
                st = os.fstat(fd)
                os.close(fd)
                if 1000 <= st.st_uid < 60000:
                    homes[rel] = (st.st_uid, st.st_gid)
        return homes

    def _write_windows_button(self, mnt):
        steam_desc = "SteamOS"
        ps1 = "\r\n".join([
            "# Booty: restart into SteamOS once, then Windows stays available from SteamOS's power menu.",
            "$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(",
            "    [Security.Principal.WindowsBuiltInRole]::Administrator)",
            "if (-not $admin) {",
            "    Start-Process powershell -Verb RunAs -ArgumentList \"-NoProfile -ExecutionPolicy Bypass -File `\"$PSCommandPath`\"\"",
            "    exit",
            "}",
            "$blocks = (bcdedit /enum firmware | Out-String) -split \"\\r?\\n\\s*\\r?\\n\"",
            f"$entry = $blocks | Where-Object {{ $_ -match 'description\\s+{steam_desc}' }} | Select-Object -First 1",
            "if (-not $entry) { Read-Host 'Booty could not find the SteamOS boot entry. Press Enter to close'; exit 1 }",
            "$id = [regex]::Match($entry, 'identifier\\s+(\\{[0-9a-fA-F-]+\\})').Groups[1].Value",
            "bcdedit /set '{fwbootmgr}' bootsequence $id | Out-Null",
            "shutdown /r /t 0",
            "",
        ])
        cmd = "\r\n".join([
            "@echo off",
            "rem Booty: restart into SteamOS",
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%ProgramData%\\Booty\\back-to-steamos.ps1"',
            "",
        ])
        safe_write(mnt, "ProgramData/Booty/back-to-steamos.ps1", ps1)
        safe_write(mnt, "Users/Public/Desktop/Back to SteamOS.cmd", cmd)
        return "Added \"Back to SteamOS\" to the Windows desktop"

    def _write_linux_button(self, mnt, steam_id):
        homes = self._linux_homes(mnt)
        if not homes:
            raise RuntimeError("No user account yet. Start it once, finish setup and log in, then try again.")
        desktop_file = "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=Back to SteamOS",
            "Comment=Restart into SteamOS (added by Booty)",
            "Icon=system-reboot",
            f'Exec=sh -c "pkexec efibootmgr -n {steam_id} && systemctl reboot"',
            "Terminal=false",
            "Categories=System;",
            "",
        ])
        selinux = any(os.path.exists(os.path.join(mnt, p, "selinux", "config"))
                      for p in ("etc", "root/etc")) or glob.glob(os.path.join(mnt, "*", "ostree"))
        label = ("security.selinux", b"unconfined_u:object_r:user_home_t:s0\x00") if selinux else None
        for home, (uid, gid) in homes.items():
            for sub in (".local/share/applications", "Desktop"):
                if sub == "Desktop":
                    try:
                        os.close(open_dir_fd(mnt, os.path.join(home, sub)))
                    except (OSError, ValueError):
                        continue  # no Desktop folder (or not a real one)
                safe_write(mnt, os.path.join(home, sub, "back-to-steamos.desktop"), desktop_file,
                           mode=0o755, uid=uid, gid=gid, xattr=label)
        return f"Added \"Back to SteamOS\" to the app menu for {len(homes)} account(s)"

    async def add_return_buttons(self):
        """Find other installed OSes and add a 'Back to SteamOS' button to each. Returns one result per OS."""
        entries = self._boot_entries()[0]
        steam = next((e["id"] for e in entries if e["kind"] == "steamos"), None)
        if not steam:
            raise RuntimeError("SteamOS boot entry not found")
        loop = asyncio.get_event_loop()

        def work():
            results = []
            for part in self._other_os_partitions():
                try:
                    mnt, ours = self._peek(part)
                except RuntimeError:
                    continue
                try:
                    found = self._identify(mnt)
                finally:
                    if ours:
                        self._umount(mnt)
                if not found:
                    continue
                kind, name = found
                res = {"name": name, "partition": part["path"], "ok": False, "detail": ""}
                try:
                    mnt = self._open_for_work(part)
                except RuntimeError as e:
                    res["detail"] = ("Windows is hibernated (Fast Startup). In Windows, hold Shift while choosing "
                                     "Shut down, or turn off Fast Startup, then try again.") \
                        if kind == "windows" else f"Couldn't open it for writing: {e}"
                    results.append(res)
                    continue
                try:
                    res["detail"] = self._write_windows_button(mnt) if kind == "windows" \
                        else self._write_linux_button(mnt, steam)
                    res["ok"] = True
                except Exception as e:
                    res["detail"] = str(e)
                finally:
                    run(["sync"], check=False)
                    self._umount(mnt)
                results.append(res)
            return results

        return await loop.run_in_executor(None, work)

    # ---------- lifecycle ----------

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        try:
            for e in self._boot_entries()[0]:
                if e["kind"] == "installer" and not e["current"]:
                    run(["efibootmgr", "-q", "-b", e["id"], "-B"], check=False)
            # The Deck's firmware drops boot entries for OSes it can't see at power-on (often ones on the
            # SD card), so put back any that are missing.
            res = await self.loop.run_in_executor(None, lambda: self._repair_entries_sync(only_managed=True))
            if res["created"]:
                decky.logger.info("restored boot entries: %s", ", ".join(res["created"]))
            # Hide the boot menu and fix SteamOS entries a newly installed Linux added to its GRUB.
            await self.loop.run_in_executor(None, lambda: self._tidy_grub_sync(only_managed=True))
            if self._load_settings()["keep_steamos_default"]:
                self._ensure_steamos_first()
        except Exception:
            decky.logger.exception("startup boot checks failed")
        decky.logger.info("Booty ready")

    async def _unload(self):
        self.cancel = True
