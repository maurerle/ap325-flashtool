#!/usr/bin/env python3
"""
Multi-device flash tool for the Aruba AP-325 access point.

Runs the full 3-step OpenWrt install per device:
  1. Flash patched APBoot bootloader (fetched via TFTP).
  2. Repartition NAND and tftpboot the OpenWrt initramfs.
  3. Configure a unique IP via serial, then sysupgrade via SCP/SSH from host.

One asyncio task per --port runs the workflow in an infinite loop and restarts
when a new device is detected on the same serial line. The TFTP server is
embedded by default; pass --external-tftp to point at an existing one instead.

Requires root (or CAP_NET_ADMIN, plus CAP_NET_BIND_SERVICE when the embedded
TFTP server is in use).
"""

import argparse
import asyncio
import logging
import platform
import re
import socket
import sys
import time
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Interface, ip_interface
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import Dict, Optional

import serial

# ----- constants ----------------------------------------------------------- #

TFTP_PORT = 69
TFTP_TIMEOUT = 5
TFTP_MAX_RETRIES = 5
SERIAL_TIMEOUT = 5

BAUD_STOCK = 9600
BAUD_PATCHED = 115200

PATCHED_VERSION = "1.5.7.2"

# The bootloader file is always requested under this name by APBoot.
BOOTLOADER_TFTP_NAME = "u-boot.mbn"

# TFTP wire format
TFTP_OPCODES = {1: "RRQ", 2: "WRQ", 3: "DATA", 4: "ACK", 5: "ERROR"}
TFTP_ERRORS = {
    0: "Not Defined",
    1: "File Not Found",
    2: "Access Violation",
    3: "Disk Full or Allocation Exceeded",
    4: "Illegal TFTP operation",
    5: "Unknown Transfer TID",
    6: "File Already Exists",
    7: "No Such User",
}
TFTP_BLOCK_SIZE = 512
TFTP_DATA_PACKET_FULL = TFTP_BLOCK_SIZE + 4  # opcode(2) + block(2) + 512

IS_POSIX = platform.system() in ["Linux", "Darwin", "FreeBSD"]

# ----- TFTP packet helpers (adapted from fritzflash.py, no globals) -------- #


def tftp_get_opcode(data: bytes) -> Optional[str]:
    opcode = int.from_bytes(data[0:2], byteorder="big")
    return TFTP_OPCODES.get(opcode)


def tftp_decode_request(data: bytes):
    header = data[2:].split(b"\x00")
    file = Path(header[0].decode("utf-8"))
    mode = header[1].decode("utf-8").lower()
    return file, mode


def tftp_build_data(block: int, file: Path) -> bytes:
    offset = (block - 1) * TFTP_BLOCK_SIZE
    with file.open("rb") as f:
        f.seek(offset)
        chunk = f.read(TFTP_BLOCK_SIZE)
    return (
        b"\x00\x03"
        + ((block >> 8) & 0xFF).to_bytes(1, "big")
        + (block & 0xFF).to_bytes(1, "big")
        + chunk
    )


def tftp_build_error(code: int) -> bytes:
    msg = TFTP_ERRORS[code].encode("utf-8")
    return (
        b"\x00\x05"
        + ((code >> 8) & 0xFF).to_bytes(1, "big")
        + (code & 0xFF).to_bytes(1, "big")
        + msg
        + b"\x00"
    )


# ----- TFTP server (asyncio) ----------------------------------------------- #


class TftpTransferProtocol(asyncio.DatagramProtocol):
    """One per active transfer. Owns its own ephemeral UDP socket."""

    def __init__(self, client_addr, file: Path, done: asyncio.Future, log):
        self.client_addr = client_addr
        self.file = file
        self.done = done
        self.log = log
        self.block = 0
        self.last_packet = b""
        self.retries = 0
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.timer: Optional[asyncio.TimerHandle] = None

    def connection_made(self, transport):
        self.transport = transport
        self._send_next_block()

    def _send_next_block(self):
        self.block += 1
        self.last_packet = tftp_build_data(self.block, self.file)
        self._send_last()

    def _send_last(self):
        if self.transport is None:
            return
        self.transport.sendto(self.last_packet, self.client_addr)
        self._reschedule_timer()

    def _reschedule_timer(self):
        if self.timer is not None:
            self.timer.cancel()
        loop = asyncio.get_event_loop()
        self.timer = loop.call_later(TFTP_TIMEOUT, self._on_timeout)

    def _on_timeout(self):
        self.retries += 1
        if self.retries > TFTP_MAX_RETRIES:
            if not self.done.done():
                self.done.set_result((False, self.client_addr))
            return
        self._send_last()

    def datagram_received(self, data, addr):
        if addr != self.client_addr:
            if self.transport is not None:
                self.transport.sendto(tftp_build_error(5), addr)
            return
        opcode = tftp_get_opcode(data)
        if opcode != "ACK":
            if self.transport is not None:
                self.transport.sendto(tftp_build_error(4), addr)
            return
        self.retries = 0
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        # Final ACK arrives after the last short packet
        if len(self.last_packet) < TFTP_DATA_PACKET_FULL:
            if not self.done.done():
                self.done.set_result((True, addr))
            return
        self._send_next_block()

    def connection_lost(self, exc):
        if self.timer is not None:
            self.timer.cancel()


class TftpListenerProtocol(asyncio.DatagramProtocol):
    """Bound to UDP/69; spawns a TftpTransferProtocol per request."""

    def __init__(self, files: Dict[str, Path]):
        self.files = files
        self.log = logging.getLogger("tftp")
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        opcode = tftp_get_opcode(data)
        if opcode != "RRQ":
            self.log.warning("ignoring non-RRQ from %s (opcode=%s)", addr, opcode)
            if self.transport is not None:
                self.transport.sendto(tftp_build_error(4), addr)
            return
        rfile, mode = tftp_decode_request(data)
        target = self.files.get(rfile.name.strip())
        if target is None:
            self.log.warning(
                "file %r requested by %s but not in pool (have: %s)",
                rfile.name,
                addr,
                sorted(self.files.keys()),
            )
            if self.transport is not None:
                self.transport.sendto(tftp_build_error(1), addr)
            return
        self.log.info("RRQ %s from %s", rfile.name, addr)
        asyncio.create_task(self._serve(addr, target))

    async def _serve(self, client_addr, file: Path):
        loop = asyncio.get_event_loop()
        done = loop.create_future()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: TftpTransferProtocol(client_addr, file, done, self.log),
            local_addr=("0.0.0.0", 0),
        )
        try:
            ok, who = await done
            if ok:
                self.log.info("transferred %s to %s", file.name, who)
            else:
                self.log.warning("transfer of %s to %s timed out", file.name, who)
        finally:
            transport.close()


async def run_tftp_server(host_ip: IPv4Address, files: Dict[str, Path]):
    log = logging.getLogger("tftp")
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: TftpListenerProtocol(files),
        local_addr=(str(host_ip), TFTP_PORT),
    )
    log.info("listening on %s:%d", host_ip, TFTP_PORT)
    log.info("serving files: %s", sorted(files.keys()))
    try:
        await asyncio.Event().wait()
    finally:
        transport.close()


# ----- host network setup -------------------------------------------------- #


@contextmanager
def set_host_ip(ipinterface: IPv4Interface, network_device: str):
    """Add ipinterface to network_device for the lifetime of the context.

    Differs from fritzflash.py's variant: refuses to silently inherit an IP
    that was already configured (would otherwise be deleted on exit even
    though we didn't add it).
    """
    log = logging.getLogger("net")
    if IS_POSIX:
        start_cmd = [
            "ip",
            "addr",
            "add",
            ipinterface.with_prefixlen,
            "dev",
            network_device,
        ]
        stop_cmd = [
            "ip",
            "addr",
            "delete",
            ipinterface.with_prefixlen,
            "dev",
            network_device,
        ]
    else:
        start_cmd = [
            "netsh",
            "interface",
            "ipv4",
            "add",
            "address",
            network_device,
            str(ipinterface.ip),
            str(ipinterface.netmask),
        ]
        stop_cmd = [
            "netsh",
            "interface",
            "ipv4",
            "delete",
            "address",
            network_device,
            str(ipinterface.ip),
        ]

    result = run(start_cmd, capture_output=True)
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        if "exists" in stderr.lower() or result.returncode == 2:
            raise RuntimeError(
                f"host IP {ipinterface} already configured on {network_device}. "
                f"Remove it first (`ip addr del {ipinterface.with_prefixlen} dev "
                f"{network_device}`) or pass a different --host-ip."
            )
        raise RuntimeError(
            f"failed to add {ipinterface} to {network_device} (rc={result.returncode}): {stderr}"
        )

    log.info("added %s to %s", ipinterface, network_device)
    try:
        if not IS_POSIX:
            time.sleep(5)
        yield
    finally:
        log.info("removing %s from %s", ipinterface, network_device)
        run(stop_cmd, capture_output=True)


# ----- serial helpers (sync; called from asyncio.to_thread) ---------------- #


def _decode(b: bytes) -> str:
    return b.decode("ascii", errors="replace").rstrip()


def serial_wait_prompt(ser: serial.Serial, log) -> list:
    """Read lines until 'apboot> ' prompt. Returns lines seen (without prompt)."""
    res = []
    while True:
        line = ser.readline().strip()
        if not line:
            continue
        log.debug("<<< %s", _decode(line))
        if line.startswith(b"apboot>"):
            break
        res.append(line)
    return res


def serial_run(ser: serial.Serial, log, cmd, wait: bool = True) -> Optional[list]:
    if isinstance(cmd, str):
        cmd = cmd.encode("ascii")
    cmd = cmd.strip()
    ser.reset_input_buffer()
    ser.write(cmd + b"\r\n")
    log.debug(">>> %s", _decode(cmd))
    ser.readline()  # consume echo
    if wait:
        return serial_wait_prompt(ser, log)
    return None


def serial_read_until(ser: serial.Serial, log, needle: bytes, timeout: float) -> bytes:
    """Read raw bytes until `needle` appears or `timeout` elapses. Returns buffered bytes."""
    start = time.monotonic()
    buf = b""
    while time.monotonic() - start < timeout:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            decoded = _decode(chunk)
            if decoded:
                log.debug("<<< %s", decoded)
            if needle in buf:
                return buf
    raise TimeoutError(f"timed out waiting for {needle!r}")


def serial_read_until_pattern(
    ser: serial.Serial, log, pattern: bytes, timeout: float
) -> re.Match:
    start = time.monotonic()
    buf = b""
    rx = re.compile(pattern)
    while time.monotonic() - start < timeout:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            decoded = _decode(chunk)
            if decoded:
                log.debug("<<< %s", decoded)
            m = rx.search(buf)
            if m:
                return m
    raise TimeoutError(f"timed out waiting for pattern {pattern!r}")


def serial_stop_autoboot(
    ser: serial.Serial, log, device_ip: IPv4Address, tftp_ip: IPv4Address
):
    """Interrupt autoboot, then set static IP env. Static-IP variant: no DHCP."""
    log.info("waiting for autoboot prompt")
    buf = b""
    while b"<Enter> to stop autoboot:" not in buf:
        chunk = ser.read()
        if not chunk:
            continue
        buf += chunk
        buf = buf.split(b"\n", 1)[-1]
        if b"Booting OS partition" in buf:
            raise RuntimeError(
                "device is booting OS, missed the autoboot window - power-cycle to retry"
            )

    ser.write(b"\r\n")
    serial_wait_prompt(ser, log)
    serial_run(ser, log, "autoreboot off")
    serial_run(ser, log, f"setenv ipaddr {device_ip}")
    serial_run(ser, log, "setenv netmask 255.255.255.0")
    serial_run(ser, log, f"setenv serverip {tftp_ip}")
    serial_run(ser, log, "setenv autostart no")


def serial_netget(ser: serial.Serial, log, filename: str, load_addr: str = "44000000") -> int:
    log.info("netget %s -> 0x%s", filename, load_addr)
    res = serial_run(ser, log, f"netget {load_addr} {filename}")
    joined = b"\n".join(res or [])
    m = re.search(rb"Bytes transferred = (\d+) \(", joined)
    if not m:
        raise RuntimeError(
            f"netget {filename} produced no transfer-count line. Output:\n"
            f"{joined.decode(errors='replace')}"
        )
    return int(m.group(1))


# ----- step implementations (sync, run inside asyncio.to_thread) ----------- #


def detect_baud(port_path: str, log) -> int:
    """Try 9600 first, then 115200. Returns the rate at which an APBoot banner
    was seen. Blocks indefinitely until a device shows up — keeps alternating
    baud rates with a brief pause between attempts."""
    banner_rx = re.compile(rb"APBoot (?P<version>\S+) \(build (?P<build>\S+)\)")
    candidates = [BAUD_STOCK, BAUD_PATCHED]
    idx = 0
    warned_already_booting = False
    while True:
        baud = candidates[idx % 2]
        log.info("listening for APBoot banner at %d baud", baud)
        try:
            with serial.Serial(
                port=port_path, baudrate=baud, timeout=SERIAL_TIMEOUT, xonxoff=True
            ) as ser:
                end = time.monotonic() + 10
                buf = b""
                while time.monotonic() < end:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    decoded = _decode(chunk)
                    if decoded:
                        log.debug("<<< %s", decoded)
                    if b"Booting OS partition" in buf and not warned_already_booting:
                        log.warning(
                            "device is already booting OS - power-cycle to interrupt"
                        )
                        warned_already_booting = True
                    m = banner_rx.search(buf)
                    if m:
                        version = m.group("version").decode("ascii")
                        log.info(
                            "found APBoot %s at %d baud (build %s)",
                            version,
                            baud,
                            m.group("build").decode("ascii"),
                        )
                        if version == PATCHED_VERSION:
                            log.info(
                                "device appears to already run the patched bootloader; "
                                "continuing with full 3-step flow anyway"
                            )
                        return baud
        except serial.SerialException as e:
            log.error("serial error on %s: %s", port_path, e)
            time.sleep(2)
        idx += 1
        log.info("no banner yet, switching baud and waiting again")


def flash_bootloader_step(
    port_path: str,
    baud_in: int,
    device_ip: IPv4Address,
    tftp_ip: IPv4Address,
    log,
):
    log.info("STEP 1: flashing patched bootloader (%s) at %d baud", BOOTLOADER_TFTP_NAME, baud_in)
    with serial.Serial(
        port=port_path, baudrate=baud_in, timeout=SERIAL_TIMEOUT, xonxoff=True
    ) as ser:
        serial_stop_autoboot(ser, log, device_ip, tftp_ip)
        size = serial_netget(ser, log, BOOTLOADER_TFTP_NAME)
        serial_run(ser, log, "sf probe 0")
        serial_run(ser, log, "sf erase 220000 100000")
        log.warning(
            "writing SPI flash - DO NOT interrupt or power off the device until the prompt returns"
        )
        serial_run(ser, log, f"sf write 44000000 220000 {size:x}")
        serial_run(ser, log, "reset", wait=False)
    log.info("STEP 1 complete; device should reboot into the patched bootloader")


def repartition_and_boot_initramfs_step(
    port_path: str,
    device_ip: IPv4Address,
    tftp_ip: IPv4Address,
    initramfs_name: str,
    log,
):
    log.info("STEP 2: waiting for patched APBoot at %d baud", BAUD_PATCHED)

    # Patched APBoot uses CONFIG_BAUDRATE=115200. If a stock-saveenv'd baudrate
    # was preserved, the device may still be at 9600 - fall back and warn.
    baud = BAUD_PATCHED
    try:
        with serial.Serial(
            port=port_path, baudrate=BAUD_PATCHED, timeout=SERIAL_TIMEOUT, xonxoff=True
        ) as ser:
            serial_read_until_pattern(
                ser, log, rb"APBoot (\S+) \(build (\S+)\)", timeout=15
            )
    except TimeoutError:
        log.warning(
            "no APBoot banner at 115200; trying 9600. If this works, the device's stock "
            "env stored a `baudrate` override. Consider `setenv baudrate 115200; saveenv`."
        )
        baud = BAUD_STOCK

    with serial.Serial(
        port=port_path, baudrate=baud, timeout=SERIAL_TIMEOUT, xonxoff=True
    ) as ser:
        # First half: erase NAND, then reset (board_late_init recreates default
        # aos0/aos1/ubifs volumes which we then reshape below).
        serial_stop_autoboot(ser, log, device_ip, tftp_ip)
        serial_run(ser, log, "nand device 0")
        log.warning("erasing NAND - this takes ~30 seconds")
        serial_run(ser, log, "nand erase.chip")
        serial_run(ser, log, "reset", wait=False)

    # Second half: re-stop autoboot, reshape UBI volumes, tftpboot initramfs.
    log.info("STEP 2 (cont): waiting for APBoot after NAND wipe")
    with serial.Serial(
        port=port_path, baudrate=baud, timeout=SERIAL_TIMEOUT, xonxoff=True
    ) as ser:
        serial_stop_autoboot(ser, log, device_ip, tftp_ip)
        serial_run(ser, log, "ubi part ubifs")
        serial_run(ser, log, "ubi remove ubifs")
        serial_run(ser, log, "ubi create ubifs 1")
        serial_run(ser, log, "ubi create rootfs_data")
        serial_run(ser, log, "setenv autostart yes")
        # tftpboot auto-boots the loaded image when autostart=yes; we don't
        # wait for a prompt because the device will boot the kernel instead.
        log.info("tftpbooting %s - device will boot OpenWrt initramfs", initramfs_name)
        serial_run(ser, log, f"tftpboot {initramfs_name}", wait=False)


def sysupgrade_step(
    port_path: str,
    device_ip: IPv4Address,
    tftp_ip: IPv4Address,
    sysupgrade_path: Path,
    log,
):
    log.info("STEP 3: waiting for OpenWrt initramfs to boot")

    with serial.Serial(
        port=port_path, baudrate=BAUD_PATCHED, timeout=SERIAL_TIMEOUT, xonxoff=True
    ) as ser:
        try:
            serial_read_until(
                ser, log, b"Please press Enter to activate this console", timeout=240
            )
        except TimeoutError as e:
            raise RuntimeError(
                "OpenWrt did not show a console-activation prompt within 4 minutes"
            ) from e
        ser.write(b"\r\n")
        time.sleep(2)
        log.info("configuring device IP %s on br-lan via serial", device_ip)
        # Remove any default 192.168.1.1 OpenWrt may have already configured -
        # multiple parallel devices would otherwise collide on that address.
        ser.write(b"ip addr flush dev br-lan 2>/dev/null\r\n")
        time.sleep(0.5)
        ser.write(f"ip addr add {device_ip}/24 dev br-lan\r\n".encode("ascii"))
        time.sleep(0.5)
        ser.write(b"ip link set br-lan up\r\n")
        time.sleep(1)
        # Drain serial briefly so the log shows any errors from the commands.
        end = time.monotonic() + 2
        while time.monotonic() < end:
            chunk = ser.read(256)
            if chunk:
                decoded = _decode(chunk)
                if decoded:
                    log.debug("<<< %s", decoded)

    # Wait for SSH reachable. If br-lan failed, try eth0 as a fallback.
    if not wait_for_ssh(device_ip, timeout=30, log=log):
        log.warning("device unreachable on br-lan; trying eth0 fallback")
        with serial.Serial(
            port=port_path,
            baudrate=BAUD_PATCHED,
            timeout=SERIAL_TIMEOUT,
            xonxoff=True,
        ) as ser:
            ser.write(b"ip addr flush dev eth0 2>/dev/null\r\n")
            time.sleep(0.5)
            ser.write(f"ip addr add {device_ip}/24 dev eth0\r\n".encode("ascii"))
            time.sleep(0.5)
            ser.write(b"ip link set eth0 up\r\n")
            time.sleep(2)
        if not wait_for_ssh(device_ip, timeout=30, log=log):
            raise RuntimeError(
                f"could not reach {device_ip}:22 from host; check switch and host IP"
            )

    log.info("uploading %s to %s:/tmp/", sysupgrade_path.name, device_ip)
    try:
        scp_upload(device_ip, sysupgrade_path)
        log.info("running sysupgrade on device (will disconnect on reboot)")
        try:
            ssh_run(
                device_ip,
                ["sysupgrade", "-n", f"/tmp/{sysupgrade_path.name}"],
            )
        except CalledProcessError as e:
            # sysupgrade reboots the device, causing SSH to disconnect with a
            # non-zero exit code. Anything non-zero here we treat as success
            # because the alternative (a clean exit) doesn't happen.
            log.info("ssh disconnected (rc=%d) — expected on sysupgrade reboot", e.returncode)
    except Exception as e:
        log.warning("SCP/SSH path failed (%s); falling back to TFTP over serial", e)
        sysupgrade_over_serial(port_path, sysupgrade_path.name, tftp_ip, log)


def sysupgrade_over_serial(
    port_path: str, sysupgrade_name: str, tftp_ip: IPv4Address, log
):
    """Last-resort fallback: drive sysupgrade entirely from the serial console
    using busybox's TFTP client against the configured TFTP server."""
    with serial.Serial(
        port=port_path, baudrate=BAUD_PATCHED, timeout=SERIAL_TIMEOUT, xonxoff=True
    ) as ser:
        ser.write(
            f"tftp -g -r {sysupgrade_name} -l /tmp/sysupgrade.bin {tftp_ip}\r\n".encode("ascii")
        )
        time.sleep(2)
        end = time.monotonic() + 120
        while time.monotonic() < end:
            chunk = ser.read(256)
            if chunk:
                decoded = _decode(chunk)
                if decoded:
                    log.debug("<<< %s", decoded)
                if b"#" in chunk:  # rough shell-prompt detection
                    break
        ser.write(b"sysupgrade -n /tmp/sysupgrade.bin\r\n")
        # sysupgrade will reboot the device. Drain serial until that happens.
        end = time.monotonic() + 180
        while time.monotonic() < end:
            chunk = ser.read(256)
            if chunk:
                decoded = _decode(chunk)
                if decoded:
                    log.debug("<<< %s", decoded)


# ----- network reachability + ssh/scp ------------------------------------- #


def wait_for_ssh(host: IPv4Address, timeout: float, log) -> bool:
    log.info("waiting for ssh on %s (timeout %.0fs)", host, timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((str(host), 22), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _ssh_args(user: str) -> list:
    null_file = "/dev/null" if IS_POSIX else "NUL"
    return [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={null_file}",
        "-o",
        "HostKeyAlgorithms=+ssh-rsa,ssh-dss",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "BatchMode=yes",
    ]


def ssh_run(host: IPv4Address, cmd: list, user: str = "root"):
    args = ["ssh", *_ssh_args(user), f"{user}@{host}", *cmd]
    run(args, capture_output=True).check_returncode()


def scp_upload(host: IPv4Address, file: Path, user: str = "root", target: str = "/tmp/"):
    args = ["scp", *_ssh_args(user), str(file), f"{user}@{host}:{target}"]
    run(args).check_returncode()


# ----- per-port worker ----------------------------------------------------- #


class PortAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['port']}] {msg}", kwargs


async def flash_port_loop(
    port_path: str,
    device_ip: IPv4Address,
    tftp_ip: IPv4Address,
    initramfs_name: str,
    sysupgrade_path: Path,
):
    log = PortAdapter(logging.getLogger("port"), {"port": Path(port_path).name})
    while True:
        try:
            await asyncio.to_thread(
                _run_one_device,
                port_path,
                device_ip,
                tftp_ip,
                initramfs_name,
                sysupgrade_path,
                log,
            )
            log.info("DONE — power-cycle to flash the next device on this port")
        except asyncio.CancelledError:
            log.info("port worker cancelled; exiting")
            raise
        except Exception as e:
            log.error("FAILED: %s", e, exc_info=True)
            log.info("continuing - waiting for the next device on this port")
        await asyncio.sleep(2)


def _run_one_device(
    port_path: str,
    device_ip: IPv4Address,
    tftp_ip: IPv4Address,
    initramfs_name: str,
    sysupgrade_path: Path,
    log,
):
    baud = detect_baud(port_path, log)
    flash_bootloader_step(port_path, baud, device_ip, tftp_ip, log)
    repartition_and_boot_initramfs_step(
        port_path, device_ip, tftp_ip, initramfs_name, log
    )
    sysupgrade_step(port_path, device_ip, tftp_ip, sysupgrade_path, log)


# ----- CLI / main ---------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Privileges: needs root (or CAP_NET_ADMIN) to add a host IP. "
            "Also needs CAP_NET_BIND_SERVICE to bind UDP/69 when the embedded "
            "TFTP server is in use (i.e. when --external-tftp is not set)."
        ),
    )
    parser.add_argument(
        "--bootloader",
        required=True,
        type=Path,
        help="patched APBoot binary (served as u-boot.mbn over TFTP)",
    )
    parser.add_argument(
        "--initramfs",
        required=True,
        type=Path,
        help="OpenWrt initramfs .ari (tftp-booted in Step 2)",
    )
    parser.add_argument(
        "--sysupgrade",
        required=True,
        type=Path,
        help="OpenWrt sysupgrade .bin (uploaded via SCP in Step 3)",
    )
    parser.add_argument(
        "--interface",
        required=True,
        help="host network interface (e.g. eth0) on which to add --host-ip",
    )
    parser.add_argument(
        "--host-ip",
        required=True,
        help="host IP address with prefix, e.g. 192.168.1.1/24",
    )
    parser.add_argument(
        "--port",
        action="append",
        required=True,
        dest="ports",
        help="serial port to drive (repeatable, e.g. --port /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--device-ip-base",
        type=int,
        default=10,
        help="starting host octet for auto-assigned device IPs (default: 10)",
    )
    parser.add_argument(
        "--external-tftp",
        type=IPv4Address,
        default=None,
        metavar="IP",
        help=(
            "use an external TFTP server at this IP instead of starting an "
            "embedded one. You're responsible for serving u-boot.mbn, the "
            "initramfs .ari, and the sysupgrade .bin (under their basenames) "
            "from that server."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def setup_logging(level: str):
    logging.basicConfig(
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )
    # asyncio chatter at DEBUG is overwhelming; keep it quiet unless requested.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def validate_files(args) -> Dict[str, Path]:
    # sysupgrade is always read locally (uploaded via SCP). bootloader and
    # initramfs are only needed locally when we run our own TFTP server.
    must_exist = [args.sysupgrade]
    if args.external_tftp is None:
        must_exist += [args.bootloader, args.initramfs]
    for f in must_exist:
        if not f.is_file():
            raise SystemExit(f"file not found: {f}")
    return {
        BOOTLOADER_TFTP_NAME: args.bootloader.resolve(),
        args.initramfs.name: args.initramfs.resolve(),
        args.sysupgrade.name: args.sysupgrade.resolve(),
    }


def assign_device_ips(args) -> Dict[str, IPv4Address]:
    host_if = ip_interface(args.host_ip)
    network = host_if.network
    host_ip = host_if.ip
    assigned: Dict[str, IPv4Address] = {}
    base = args.device_ip_base
    for i, port in enumerate(args.ports):
        candidate = IPv4Address(int(network.network_address) + base + i)
        if candidate not in network:
            raise SystemExit(
                f"auto-assigned device IP {candidate} for port {port} falls outside "
                f"the {network} subnet; pick a smaller --device-ip-base"
            )
        if candidate == host_ip:
            raise SystemExit(
                f"auto-assigned device IP {candidate} collides with --host-ip"
            )
        assigned[port] = candidate
    return assigned


async def amain(args):
    log = logging.getLogger("main")
    files = validate_files(args)
    port_to_ip = assign_device_ips(args)
    host_if = ip_interface(args.host_ip)
    tftp_ip = args.external_tftp if args.external_tftp is not None else host_if.ip

    log.info("host IP: %s on %s", host_if, args.interface)
    if args.external_tftp is not None:
        log.info("TFTP server: external at %s (embedded server disabled)", tftp_ip)
        log.info(
            "  expected files on that server: %s, %s, %s",
            BOOTLOADER_TFTP_NAME,
            args.initramfs.name,
            args.sysupgrade.name,
        )
    else:
        log.info("TFTP server: embedded at %s:%d", tftp_ip, TFTP_PORT)
    for port, ip in port_to_ip.items():
        log.info("  %s -> device IP %s", port, ip)

    with set_host_ip(host_if, args.interface):
        async with asyncio.TaskGroup() as tg:
            if args.external_tftp is None:
                tg.create_task(run_tftp_server(host_if.ip, files))
            for port, dev_ip in port_to_ip.items():
                tg.create_task(
                    flash_port_loop(
                        port,
                        dev_ip,
                        tftp_ip,
                        args.initramfs.name,
                        files[args.sysupgrade.name],
                    )
                )


def main():
    args = parse_args()
    setup_logging(args.log_level)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        logging.getLogger("main").info("interrupted; cleaning up")
        sys.exit(130)
    except BaseExceptionGroup as eg:
        for e in eg.exceptions:
            logging.getLogger("main").error("%s", e)
        sys.exit(1)
    except RuntimeError as e:
        logging.getLogger("main").error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
