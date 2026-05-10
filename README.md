# ap325-flashtool

Multi-device flash tool for the Aruba AP-325 (and AP-324) access points.

Drives the full OpenWrt install over serial console plus an embedded TFTP
server. Flashes one device or many in parallel on a shared switch, with each
serial port running an infinite loop so a fresh device can be flashed simply
by power-cycling and plugging in the next one.

The AP-325's stock APBoot enforces RSA signatures even on the default NAND
boot, so a patched bootloader has to be flashed over the serial console before
OpenWrt can run. See [`lukasstockner/ap325-apboot-openwrt`][apboot] for the
patched bootloader source and prebuilt binary.

## Supported hardware

- **Aruba AP-325** — dual-radio 802.11a/b/g/n/ac access point, dual GbE,
  Qualcomm IPQ8068 SoC, 512 MiB RAM, 128 MiB NAND.
- **Aruba AP-324** — same hardware as the AP-325 but with external antennas.
  The same OpenWrt image works on both.

## Prerequisites

- Python 3.11 or newer (uses `asyncio.TaskGroup`).
- `pyserial` (`pip install pyserial`).
- `ip`, `ssh`, `scp`, `ping` available on `$PATH`. Linux/macOS/FreeBSD or
  Windows (Windows uses `netsh`).
- Root or `CAP_NET_BIND_SERVICE` + `CAP_NET_ADMIN` — the tool binds UDP/69
  for TFTP and adds an IP to a network interface.
- A USB-to-serial adapter wired to the AP's RJ45 console port (Cisco pinout).
- The AP's wired ethernet on the same L2 segment as your host — either
  directly, or via a switch when flashing multiple devices.

## Files you'll need

- `u-boot.mbn` — the patched APBoot. Build from [the bootloader
  repo][apboot] or grab a release.
- `openwrt-ipq806x-generic-aruba_ap-325-initramfs.ari` — OpenWrt initramfs
  image (booted into RAM for the install).
- `openwrt-ipq806x-generic-aruba_ap-325-squashfs-sysupgrade.bin` — OpenWrt
  sysupgrade image (written to NAND).

Both OpenWrt images come from a regular OpenWrt build for the `ipq806x/generic`
target with the `aruba_ap-325` profile.

## Quick start (single device)

```bash
sudo python3 flash_aruba_ap325.py \
    --bootloader u-boot.mbn \
    --initramfs   openwrt-ipq806x-generic-aruba_ap-325-initramfs.ari \
    --sysupgrade  openwrt-ipq806x-generic-aruba_ap-325-squashfs-sysupgrade.bin \
    --interface   eth0 \
    --host-ip     192.168.1.1/24 \
    --port        /dev/ttyUSB0
```

Then power on the AP. The tool waits for the APBoot banner, walks through the
three flash steps, and prints `[ttyUSB0] DONE` when the device is rebooting
into the freshly installed OpenWrt.

## Multi-device

Pass `--port` once per serial adapter. The tool auto-assigns each device a
unique IP in the host's subnet (`.10`, `.11`, … by default; override with
`--device-ip-base`):

```bash
sudo python3 flash_aruba_ap325.py \
    --bootloader u-boot.mbn \
    --initramfs   openwrt-ipq806x-generic-aruba_ap-325-initramfs.ari \
    --sysupgrade  openwrt-ipq806x-generic-aruba_ap-325-squashfs-sysupgrade.bin \
    --interface   eth0 \
    --host-ip     192.168.1.1/24 \
    --port        /dev/ttyUSB0 \
    --port        /dev/ttyUSB1 \
    --port        /dev/ttyUSB2
```

All workers run in parallel. The TFTP server handles concurrent transfers
natively, so there is no per-device serialization in the network path.

Each worker stays in an infinite loop. After a device finishes (or fails),
power-cycle a fresh AP on the same serial line and the worker picks it up
without restarting the tool. Stop the tool with Ctrl-C.

## What it actually does

For each device the worker performs three steps:

1. **Flash the patched APBoot.** Waits for the stock APBoot banner (auto-
   detects 9600 vs. 115200 baud), interrupts autoboot, sets static IP env,
   pulls `u-boot.mbn` over TFTP into RAM, and writes it to SPI (`sf erase`,
   `sf write`). The device resets into the new bootloader.
2. **Repartition NAND and tftpboot the initramfs.** `nand erase.chip` and
   `reset`, then reshape the UBI volumes (`ubifs` shrunk to one LEB and a
   new `rootfs_data` volume to hold the overlay), then `tftpboot` the
   initramfs `.ari` with `autostart=yes` so APBoot auto-boots it.
3. **Sysupgrade from the running OpenWrt.** Waits for the OpenWrt console-
   activation prompt, configures the device's unique IP on `br-lan` over
   serial (with `eth0` fallback), then `scp`s the sysupgrade image to
   `/tmp/` and runs `sysupgrade -n`. If SSH refuses (e.g. dropbear without
   passwordless root), the tool falls back to driving `busybox tftp` plus
   `sysupgrade` over the serial console.

The only destructive moment is the single `sf write` of the bootloader.
The tool prints a `DO NOT interrupt` banner before that line; everything
else is recoverable by power-cycling.

## Flash layout reference

The AP-325 has both SPI flash (4 MiB, holds the bootloaders) and NAND flash
(128 MiB, holds the OS).

### SPI flash

```
0x000000-0x020000 sbl1
0x020000-0x040000 mibib
0x040000-0x080000 sbl2
0x080000-0x100000 sbl3
0x100000-0x110000 ddrconfig
0x110000-0x120000 ssd
0x120000-0x1a0000 tz
0x1a0000-0x220000 rpm
0x220000-0x320000 appsbl       <- patched APBoot lives here
0x320000-0x330000 appsblenv
0x330000-0x370000 art
0x370000-0x380000 panicdump
0x380000-0x390000 certificate
0x390000-0x3a0000 mfginfo
0x3a0000-0x3b0000 flashcache
0x3b0000-0x400000 aosspare
```

### NAND flash, after OpenWrt install

```
aos0    (32 MiB MTD, UBI)   -> volume `aos0`:    OpenWrt kernel + initrd
aos1    (32 MiB MTD, UBI)   -> volume `aos1`:    OpenWrt root squashfs
ubifs   (64 MiB MTD, UBI)   -> volume `ubifs`:        dummy (1 LEB) for APBoot
                            -> volume `rootfs_data`:  UBIFS overlay
```

The `ubifs` volume exists only because APBoot's `board_late_init` recreates
default volumes whenever it doesn't find them, and `ubifs` is one of the
names it expects. The actual rootfs overlay lives in `rootfs_data`.

## Reverting to stock

The patched bootloader stays compatible with the original Aruba firmware.
Boot the OpenWrt initramfs, dump the original `aos0` and `aos1` UBI volumes
beforehand if you want a backup, then later: wipe NAND from the patched
APBoot console, let it recreate the default partitions, and flash the
stock `aos0`/`aos1` back.

## Troubleshooting

- **`host IP ... already configured`** — remove the IP and rerun
  (`sudo ip addr del 192.168.1.1/24 dev eth0`), or pass a different
  `--host-ip`. The tool refuses to silently inherit an IP it didn't set up,
  because the cleanup at shutdown would otherwise delete an IP that wasn't
  ours to delete.
- **No APBoot banner appears** — the tool alternates between 9600 and
  115200 indefinitely. If neither works: check the console wiring (RJ45,
  Cisco pinout), confirm the USB-serial adapter is at the right
  `/dev/ttyUSB*`, and try a manual `screen /dev/ttyUSB0 9600` to sanity-
  check the cable.
- **`device is booting OS, missed the autoboot window`** — the AP booted
  too fast; power-cycle. If this happens consistently, the device may have
  `autoreboot` saved to env — the tool runs `autoreboot off` on each pass
  but only after stopping autoboot.
- **SSH/SCP refused after initramfs boot** — the tool falls back to
  `busybox tftp` over serial automatically. If you want SSH to work,
  rebuild the initramfs with passwordless root enabled in dropbear.
- **Tool exits with `RTNETLINK answers: Operation not permitted`** — run
  as root, or `setcap cap_net_admin,cap_net_bind_service+ep` on the python
  interpreter.

## Credits

- Patched APBoot and the install process: [Lukas Stockner][apboot].
- TFTP server and host-IP-setup patterns originally adapted from
  `fritzflash` in [`freifunk-darmstadt/fritz-tools`][fritz-tools].

[apboot]: https://github.com/lukasstockner/ap325-apboot-openwrt
[fritz-tools]: https://github.com/freifunk-darmstadt/fritz-tools
