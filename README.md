# Booty

A [Decky Loader](https://decky.xyz) plugin for dual booting a Steam Deck without leaving Game Mode.

## What it does

- **Switch to another OS** from Steam's power menu or the Quick Access panel. Booty sets UEFI `BootNext` and
  restarts, so the other OS starts once and SteamOS stays the default. No boot menu, no rEFInd.
- **Set up a second OS**:
  1. Pick Bazzite (Deck edition), Fedora KDE, Ubuntu LTS, or Windows 11 (paste Microsoft's download link or
     use an ISO you already have).
  2. Pick where it goes and how much space it gets. On an SD card with Steam games you can keep the games:
     Booty shrinks the games partition and leaves free space for the OS. Or erase a drive. The internal drive
     is never changed automatically.
  3. Booty downloads the ISO, checks it against the official SHA-256, writes the installer to a USB stick
     (offering to back up the stick's files first), makes the space, and restarts into the installer.
- **Finishes the install** the next time SteamOS starts:
  - restores the boot entry the Deck's firmware drops for OSes on the SD card;
  - keeps SteamOS first in the boot order;
  - hides the GRUB menu that Linux installers turn on, and replaces their broken "SteamOS Holo" entries with
    one that starts SteamOS's real loader.
- **Back to SteamOS buttons** inside the other OS: a desktop shortcut on Windows, an app on Linux.
- **USB backups**: files on an installer stick are kept in `~/Booty Backups` and can be copied back later.

## Security

The backend runs as root (it needs `efibootmgr` and raw disk access). Anything that can reach Steam's local
debug port can call it, and it works with drives that may come from anywhere, so:

- No code from another drive is ever run. GRUB is fixed by editing its text files, not by chrooting.
- Foreign filesystems are mounted `nosuid,nodev,noexec` inside a root-only folder, and every read and write
  inside them refuses symlinks, so a crafted drive can't redirect root to files on the Deck.
- Root only reads files the `deck` user owns (ISOs), parses ISOs and restores backups *as* the `deck` user,
  and never writes into a user-owned folder by path: work happens in root-only `/home/.booty` and finished
  files are handed over with an atomic rename.
- Startup repairs only touch drives Booty set up (or that you pointed it at with "Find installed OSes"), and
  new boot entries never take over the boot order.
- Downloads are checked against the publisher's SHA-256. The Rufus UEFI:NTFS image used for Windows sticks is
  pinned to Rufus v4.11 and checked by hash.

## Install

In Decky: gear icon → **Developer** → **Install Plugin from URL**, and enter

    https://github.com/Tman600/Booty/releases/latest/download/Booty.zip

## Status

Tested on a Steam Deck LCD (SteamOS 3.9) with Ubuntu 26.04.1 installed to the SD card: setup, downloads with
checksum checks, shrinking the SD card's games partition, writing a Linux installer, boot-entry repair, the
GRUB clean-up, the power-menu item, and the Back to SteamOS button. Not yet tested on hardware: booting the
other OS from "Switch to", Windows installer sticks, USB restore, and Fedora/Bazzite installs. Back up your
games and saves before changing partitions.

## Build

    pnpm install
    pnpm run build
    python package.py        # -> out/Booty.zip

## Credits

UEFI:NTFS by Pete Batard (https://github.com/pbatard/uefi-ntfs), downloaded at runtime when building a
Windows installer.
