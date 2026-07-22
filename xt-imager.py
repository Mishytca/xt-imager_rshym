#!/usr/bin/env python3
import os
import sys
import pathlib
import re
import argparse
from typing import List
from string import printable
import gzip
import serial
import shutil
import struct
import queue
import threading
import subprocess
import time


# UFS device 0 contains IPL boot data required to start the board. It must
# never be selected by this script. The main UFS storage is SCSI device 1.
UFS_DEVICE = 1


def main():
    """Parse command-line options, validate them, and start flashing."""

    description = (
        'Flash an uncompressed image stream to eMMC or UFS through '
        'U-Boot and TFTP.\n\n'
        'The image must be supplied through a pipe. Use cat for a raw image '
        'or zcat for a gzip-compressed image.')
    epilog = (
        'examples for Gen5:\n'
        '  Raw image to UFS:\n'
        '    cat full.img | ./xt-imager.py --target ufs '
        '-s /dev/GEN5_CONSOLE3 -b 1843200\n\n'
        '  Raw image to eMMC:\n'
        '    cat full.img | ./xt-imager.py --target emmc '
        '-s /dev/GEN5_CONSOLE3 -b 1843200\n\n'
        '  Gzip image to UFS:\n'
        '    zcat full.img.gz | ./xt-imager.py --target ufs '
        '-s /dev/GEN5_CONSOLE3 -b 1843200\n\n'
        '  Gzip image to eMMC:\n'
        '    zcat full.img.gz | ./xt-imager.py --target emmc '
        '-s /dev/GEN5_CONSOLE3 -b 1843200\n\n'
        'operation:\n'
        '  The host reads the uncompressed stream in two alternating chunks. '
        'While\n'
        '  U-Boot writes one chunk, the host prepares the next one. Each '
        'prepared\n'
        '  chunk is gzip-compressed, downloaded by U-Boot over TFTP, and '
        'written with\n'
        '  gzwrite. Installing pigz on the host enables parallel chunk '
        'compression.\n\n'
        'UFS safety:\n'
        '  UFS device 0 contains IPL boot data and is never written. UFS '
        'flashing is\n'
        '  fixed to SCSI device 1 and requires typing an explicit '
        'confirmation.\n')
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument(
        '--target',
        required=True,
        choices=('emmc', 'ufs'),
        help='Destination storage: eMMC or protected UFS data device 1')

    parser.add_argument(
        '-s',
        '--serial',
        default='/dev/ttyUSB0',
        metavar='DEVICE',
        help='Serial console connected to U-Boot (default: /dev/ttyUSB0)')

    parser.add_argument(
        '-b',
        '--baud',
        type=int,
        default=921600,
        metavar='RATE',
        help='Serial-console baud rate (default: 921600)')

    parser.add_argument(
        '-t',
        '--tftp',
        type=pathlib.Path,
        default='/srv/tftp',
        metavar='DIRECTORY',
        help='TFTP root for temporary chunks (default: /srv/tftp)')

    parser.add_argument(
        '--loadaddr',
        default='0x58000000',
        metavar='ADDRESS',
        help='U-Boot TFTP load address (default: 0x58000000)')

    parser.add_argument(
        '--mmcdev',
        type=int,
        default=0,
        metavar='NUMBER',
        help='MMC device used with --target emmc (default: 0)')

    parser.add_argument(
        '--buffersize',
        type=int,
        default=512*1024*1024,
        metavar='BYTES',
        help='Uncompressed chunk size (default: 536870912)')

    parser.add_argument(
        '--serverip',
        metavar='IP',
        help='Temporarily set the TFTP server IP in U-Boot')

    parser.add_argument(
        '--ipaddr',
        metavar='IP',
        help='Temporarily set the board IP in U-Boot')
    args = parser.parse_args()

    # A positive, 512-byte-aligned chunk works for normal eMMC writes. UFS is
    # checked again later against the block size reported by the actual device.
    if args.buffersize <= 0 or args.buffersize % 512 != 0:
        parser.error('--buffersize must be a positive, 512-byte aligned value')

    # Without a pipe, reading sys.stdin.buffer would wait indefinitely. Reject
    # this early and show the user how the image must be supplied.
    if sys.stdin.isatty():
        parser.error(
            'image data must be piped through stdin; use cat or zcat '
            'as shown in --help')

    # TFTP can serve a chunk only if its root directory already exists.
    if not os.path.isdir(args.tftp):
        raise NotADirectoryError('-t parameter is not a directory')

    print(f'[Use {args.tftp} as a TFTP root]')
    print('[Reading data from STDIN]')
    do_flash_image(args, args.tftp)


def build_write_command(args, offset):
    """Build u-boot command to write a chunk to the selected device"""

    # eMMC is selected by the user-configurable U-Boot MMC device number.

    if args.target == 'emmc':
        return (
            f'gzwrite mmc {args.mmcdev} '
            f'${{loadaddr}} ${{filesize}} 400000 {offset:X}\r')

    # UFS always uses protected data device 1; device 0 is never accepted.

    if args.target == 'ufs':
        return (
            f'gzwrite scsi {UFS_DEVICE} '
            f'${{loadaddr}} ${{filesize}} 400000 {offset:X}\r')

    raise ValueError(f'Unsupported flashing target: {args.target}')


# Rotate two TFTP files so the host can prepare the next chunk while U-Boot
# writes the current chunk to the target device.
CHUNK_NAMES = ('chunk0.bin.gz', 'chunk1.bin.gz')
# Level 1 favours preparation speed over a smaller transferred file.
COMPRESS_LEVEL = 1
# pigz is optional; Python gzip remains the single-threaded fallback.
COMPRESS_TOOLS = ('pigz',)


def find_tool(candidates):
    """Find the first installed tool from a priority-ordered list.

    Each candidate is searched in the host PATH. Return its full executable
    path when found, or None when none of the candidates are installed.
    """

    # shutil.which() resolves an executable using the host PATH.

    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def build_compress_command(tool):
    """Build command arguments for parallel pigz chunk compression.

    Use the configured fast compression level, write the gzip stream to stdout,
    and request one worker per logical CPU. If Python cannot detect the CPU
    count, use four workers as a conservative fallback.
    """

    return [tool, f'-{COMPRESS_LEVEL}', '-c', '-p',
            str(os.cpu_count() or 4)]


def compress_chunk(data, out_path, tool):
    """Compress one chunk and return its gzip CRC and packed size"""
    # Create or replace the selected temporary file in the TFTP root.

    with open(out_path, 'wb') as f_out:
        # Prefer pigz because it can compress a chunk on multiple CPU cores.

        if tool:
            proc = subprocess.Popen(
                build_compress_command(tool), stdin=subprocess.PIPE,
                stdout=f_out)
            proc.communicate(data)
            if proc.returncode != 0:
                raise RuntimeError(
                    f'Compressor exited with code {proc.returncode}')
        # If pigz is unavailable, use the compatible single-threaded Python
        # gzip implementation. U-Boot receives the same gzip file format.

        else:
            f_out.write(gzip.compress(data, compresslevel=COMPRESS_LEVEL))
    # U-Boot reports the number of bytes downloaded by TFTP. Record the exact
    # compressed size so the main thread can validate that report.

    packed_size = os.path.getsize(out_path)
    # A gzip trailer ends with CRC32 and the uncompressed size. Reading the
    # stored CRC avoids calculating it in a separate pass over the raw chunk.

    with open(out_path, 'rb') as f_in:
        f_in.seek(-8, os.SEEK_END)
        crc = struct.unpack('<I', f_in.read(4))[0]
    return crc, packed_size


<<<<<<< Updated upstream
def prepare_chunks(args, input_stream, tftp_root, free_slots, results, abort,
                   block_size, capacity_bytes, compress_tool): # noqa: C901
=======
def prepare_chunks(args, input_stream, tftp_root, free_slots, results, abort,  # noqa: C901
                   block_size, capacity_bytes, compress_tool):
>>>>>>> Stashed changes
    """Read, validate and compress chunks ahead of U-Boot writes."""
    # Send a prepared chunk, the end marker, or an error to the main thread.
    # The timeout allows this worker to notice an abort while the queue is
    # full.

    def put(item):
        while not abort.is_set():
            try:
                results.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    # Wait until one of the two TFTP files is available for reuse.
    # The main thread returns its slot after U-Boot loads the file into RAM.

    def take_slot():
        while not abort.is_set():
            try:
                return free_slots.get(timeout=0.5)
            except queue.Empty:
                continue
        return None

    # Byte offset on the target device for the next prepared chunk.
    offset = 0
    try:
        while not abort.is_set():
            # Wait for a free chunk filename before reading more input. This
            # keeps memory and temporary-file usage limited to two chunks.

            slot = take_slot()
            if slot is None:
                return
            # stdin contains uncompressed image data produced by cat or zcat.
            data = input_stream.read(args.buffersize)
            # An empty read means EOF. Return the unused slot and finish.
            if not data:
                free_slots.put(slot)
                break
            length = len(data)
            # Validate that the chunk contains complete target-device blocks.
            # This check is especially important for the final UFS chunk.

            if block_size and length % block_size != 0:
                raise ValueError(
                    f'Chunk at offset {offset} has size {length}, '
                    f'which is not aligned to the device block size '
                    f'{block_size}')
            # Since the total input size is unknown, verify UFS capacity one
            # chunk at a time before preparing data beyond the device boundary.

            if capacity_bytes and offset + length > capacity_bytes:
                raise ValueError(
                    f'Image does not fit the device: at least '
                    f'{(offset + length) / 1024**3:.2f} GiB required, '
                    f'device capacity is {capacity_bytes / 1024**3:.2f} GiB')
            # Compress into chunk0 or chunk1 and obtain the values that will
            # later be checked against the TFTP and U-Boot output.

            crc, packed_size = compress_chunk(
                data,
                os.path.join(tftp_root, CHUNK_NAMES[slot]),
                compress_tool)
            # Release the large uncompressed object as soon as it is packed.
            del data
            # Pass offset, raw size, slot, CRC and compressed size to the main
            # thread, which performs the TFTP transfer and gzwrite command.

            if not put((offset, length, slot, crc, packed_size)):
                return
            # The next chunk must be written immediately after this one.
            offset += length
        # On cancellation, the main thread does not need an end marker.
        if abort.is_set():
            return
        # None tells the main thread that stdin ended normally.
        put(None)
    except BaseException as error:
        # Forward preparation failures so flashing cannot report success.
        put(error)


# Require explicit confirmation before the destructive UFS operation
def confirm_ufs_flash(capacity_bytes):
    """Ask the terminal user to confirm destructive UFS flashing."""

    # Requiring the exact device number makes accidental confirmation harder.

    confirmation_text = f'FLASH UFS {UFS_DEVICE}'
    print('')
    print('[WARNING: destructive UFS flashing operation]')
    print(
        '[UFS device 0 is reserved for IPL booting and will not be modified.]')
    print(
        f'[Target: SCSI/UFS device {UFS_DEVICE}, '
        f'{capacity_bytes / 1024**3:.2f} GiB]')
    print(f'[All existing data on UFS device {UFS_DEVICE} may be destroyed.]')
    print(f'[To continue, type exactly: {confirmation_text}]')

    try:
        print('> ', end='', flush=True)
        # stdin carries binary image data, so confirmation must be read from
        # the controlling terminal rather than with input().

        with open('/dev/tty', 'r', encoding='utf-8') as terminal:
            answer = terminal.readline()
        if not answer:
            raise EOFError
        answer = answer.rstrip('\r\n')
    except (EOFError, KeyboardInterrupt, OSError) as error:
        raise RuntimeError(
            'UFS flashing confirmation requires an interactive terminal '
            'and was cancelled') from error

    if answer != confirmation_text:
        raise RuntimeError(
            'UFS flashing confirmation did not match; '
            'flashing was not started')


# Parse UFS capacity information from the u-boot SCSI output
def get_scsi_device_capacity(output, device):
    """Get block count and block size for a SCSI device"""

    # Isolate only the section belonging to the requested SCSI device. This
    # prevents the capacity of device 0 from being mistaken for device 1.

    device_pattern = (
        rf'(?ms)^[ \t]*Device\s+{device}:'
        rf'.*?'
        rf'(?=^[ \t]*Device\s+\d+:|\Z)')

    device_match = re.search(device_pattern, output)

    if not device_match:
        return None

    # U-Boot prints capacity as '(block count x block size)'. These integer
    # values are more reliable for validation than the rounded MB/GB text.

    capacity_match = re.search(
        r'Capacity:.*?\((\d+)\s+x\s+(\d+)\)',
        device_match.group(0),
        re.DOTALL)

    if not capacity_match:
        return None

    return int(capacity_match.group(1)), int(capacity_match.group(2))


# Scan, validate and select the UFS device before flashing
def prepare_ufs_target(conn, uboot_prompt):
    """Detect and select the protected UFS data device."""

    # Ask U-Boot to enumerate all UFS logical units and print their geometry.

    conn_send(conn, 'scsi scan\r')
    scan_output = conn_wait_for_any(conn, [uboot_prompt])
    capacity = get_scsi_device_capacity(scan_output, UFS_DEVICE)
    if capacity is None:
        raise RuntimeError(
            f'Could not determine capacity of UFS device {UFS_DEVICE}')

    # Convert device geometry into an exact byte capacity for incremental
    # bounds checking while the stdin stream is consumed.

    block_count, block_size = capacity
    capacity_bytes = block_count * block_size
    # Reject unfamiliar geometry instead of risking writes with bad alignment.

    if block_size not in (512, 4096):
        raise RuntimeError(
            f'Refusing to use UFS device {UFS_DEVICE}: '
            f'unexpected block size {block_size}')

    # Make device 1 current so the following gzwrite scsi commands target it.

    conn_send(conn, f'scsi device {UFS_DEVICE}\r')
    select_output = conn_wait_for_any(conn, [uboot_prompt])
    # Do not trust the command alone: require U-Boot to confirm the selection.

    selection_succeeded = (
        f'Device {UFS_DEVICE}:' in select_output and
        'is now current device' in select_output)
    if not selection_succeeded:
        raise RuntimeError(
            f'Could not confirm selection of UFS device {UFS_DEVICE}')

    print(
        f'\n[Selected UFS device {UFS_DEVICE}: '
        f'{capacity_bytes / 1024**3:.2f} GiB, block size {block_size}]')
    return block_count, block_size


def do_flash_image(args, tftp_root):
    """Flash stdin data to the selected storage device."""

    # Locate the optional host-side accelerator before opening the serial port.

    compress_tool = find_tool(COMPRESS_TOOLS)
    compressor_name = compress_tool or 'python gzip'
    print(f'[Compressor: {compressor_name}]')
    if compress_tool is None:
        print('[TIP: install pigz on the host to speed up flashing; otherwise '
              'single-threaded Python gzip will be used.]')

    # Open the board console. The timeout is also used by response waits so a
    # silent or disconnected board produces an error instead of hanging.

    conn = serial.Serial(port=args.serial, baudrate=args.baud, timeout=20)
    # Use the binary stdin stream; text decoding would corrupt image bytes.

    input_stream = sys.stdin.buffer
    producer = None
    # The event lets the main and producer threads request cooperative stop.

    abort = threading.Event()
    # Prepared chunk metadata flows to the main thread through results. Slot
    # numbers flow back through free_slots when a TFTP file can be overwritten.

    results = queue.Queue(maxsize=len(CHUNK_NAMES))
    free_slots = queue.Queue()

    try:
        # Wake an existing U-Boot prompt or interrupt autoboot if it is
        # starting.

        uboot_prompt = '=>'
        print('[Waiting for u-boot prompt...]')
        conn_send(conn, '\r')
        conn_wait_for_any(
            conn, [uboot_prompt, 'Hit any key to stop autoboot:'])
        conn_send(conn, '\r')
        conn_wait_for_any(conn, [uboot_prompt])
        print('\n[Connected to u-boot]')

        # eMMC does not require SCSI discovery. UFS fills both values below.

        block_size = None
        capacity_bytes = None
        # UFS requires discovery, alignment validation, and explicit approval.

        if args.target == 'ufs':
            block_count, block_size = prepare_ufs_target(conn, uboot_prompt)
            capacity_bytes = block_count * block_size
            if args.buffersize % block_size != 0:
                raise ValueError(
                    f'Buffer size {args.buffersize} is not aligned '
                    f'to UFS block size {block_size}')
            confirm_ufs_flash(capacity_bytes)

        # Change network variables only for this U-Boot session; env save is
        # not
        # called, so persistent board configuration is left untouched.

        if args.serverip:
            conn_send(conn, f'env set serverip {args.serverip}\r')
            conn_wait_for_any(conn, [uboot_prompt])
        if args.ipaddr:
            conn_send(conn, f'env set ipaddr {args.ipaddr}\r')
            conn_wait_for_any(conn, [uboot_prompt])
        # Tell U-Boot which RAM address will hold each downloaded gzip chunk.

        conn_send(conn, f'env set loadaddr {args.loadaddr}\r')
        conn_wait_for_any(conn, [uboot_prompt])
        print('')

        # Initially both rotating TFTP filenames are free for the producer.

        for slot in range(len(CHUNK_NAMES)):
            free_slots.put(slot)

        # A monotonic clock cannot jump if the host wall clock is corrected.

        flash_started_at = time.monotonic()
        # Start host-side preparation in parallel with U-Boot transfers/writes.

        producer = threading.Thread(
            target=prepare_chunks,
            args=(args, input_stream, tftp_root, free_slots, results, abort,
                  block_size, capacity_bytes, compress_tool),
            daemon=True)
        producer.start()
        bytes_sent = 0

        # Consume prepared chunks in image order and write them synchronously.

        while True:
            # Blocking here is expected when compression is slower than U-Boot.

            item = results.get()
            # None means that the producer reached normal end-of-stream.

            if item is None:
                break
            # Re-raise producer errors in the main thread to trigger cleanup.

            if isinstance(item, BaseException):
                raise item

            # Unpack everything needed to transfer and verify this chunk.

            offset, length, slot, crc, packed_size = item
            chunk_name = CHUNK_NAMES[slot]
            # Download the complete compressed file into U-Boot RAM.

            conn_send(conn, f'tftp ${{loadaddr}} {chunk_name}\r')
            conn_wait_for_any(conn, [f'Bytes transferred = {packed_size}'])
            conn_wait_for_any(conn, [uboot_prompt])

            # U-Boot copied this chunk into RAM. Its TFTP file may now be
            # reused while gzwrite writes the RAM contents to storage.
            free_slots.put(slot)
            # Decompress the RAM buffer and write raw bytes at the image
            # offset.

            conn_send(conn, build_write_command(args, offset))
            conn_wait_for_any(conn, [f'{length} bytes, crc 0x{crc:08x}'])
            print('  [CRC is OK]')
            conn_wait_for_any(conn, [uboot_prompt])
            # Progress reports committed uncompressed bytes, not TFTP bytes.

            bytes_sent += length
            print(f'\n[Progress: {bytes_sent:_}]')
    # Cleanup runs after success, serial/TFTP failure, or producer failure.

    finally:
        # Stop further chunk production before releasing shared resources.

        abort.set()
        if producer is not None:
            if producer.is_alive():
                input_stream.close()
            producer.join(timeout=10)
        # Temporary chunks must not remain in the TFTP root after this run.

        for name in CHUNK_NAMES:
            path = os.path.join(tftp_root, name)
            if os.path.exists(path):
                os.remove(path)
        conn.close()

    if producer is not None and producer.is_alive():
        raise RuntimeError('Chunk preparation thread did not stop cleanly')

    # Report elapsed wall time only after the producer stopped cleanly.

    elapsed = time.monotonic() - flash_started_at
    print(f'[Total time: {elapsed:.1f}s]')
    print('[Image was flashed successfully]')


def conn_wait_for_any(conn, expect: List[str]):
    """ Wait for any of the expected response from u-boot"""

    # Accumulate printable and non-printable serial data for substring
    # matching.

    rcv_str = ''
    # stay in the read loop until any of expected string is received
    # in other words - all expected substrings are not in received buffer
    while all([x not in rcv_str for x in expect]):
        # Read one byte at a time because U-Boot responses have no fixed
        # length.

        data = conn.read(1)

        # pyserial returns empty bytes when its configured timeout expires.

        if not data:
            raise TimeoutError(
                f'Timeout waiting for {expect} from the device')

        # Convert the byte for display and expected-text matching.

        rcv_char = chr(data[0])

        # Echo readable console output while retaining all bytes in rcv_str.

        if (rcv_char in printable or rcv_char == '\b'):
            print(rcv_char, end='', flush=True)

        rcv_str += rcv_char

    # Return captured output for parsing SCSI device information
    return rcv_str


def conn_send(conn, data):
    """ Send the string to the u-boot"""

    # U-Boot commands are ASCII and include their terminating carriage return.

    conn.write(data.encode('ascii'))


# Execute the CLI only when launched as a program, not when imported by tests.

if __name__ == '__main__':
    main()
