import builtins
import io
import gzip
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
import zlib


SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "xt-imager.py"
SPEC = importlib.util.spec_from_file_location("xt_imager", SCRIPT_PATH)

assert SPEC is not None
assert SPEC.loader is not None

xt_imager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(xt_imager)


def test_build_write_command_for_emmc():
    args = SimpleNamespace(target="emmc", mmcdev=2)

    command = xt_imager.build_write_command(args, 0x2000)

    assert command == (
        "gzwrite mmc 2 ${loadaddr} ${filesize} 400000 2000\r"
    )


def test_build_write_command_for_ufs_uses_device_one():
    args = SimpleNamespace(target="ufs", mmcdev=0)

    command = xt_imager.build_write_command(args, 0x4000)

    assert command == (
        "gzwrite scsi 1 ${loadaddr} ${filesize} 400000 4000\r"
    )
    assert "scsi 0" not in command


def test_compress_chunk_python_fallback_is_gzip_compatible(tmp_path):
    payload = b"chunk payload" * 100
    output = tmp_path / "chunk.bin.gz"

    crc, packed_size = xt_imager.compress_chunk(payload, output, None)

    assert gzip.decompress(output.read_bytes()) == payload
    assert packed_size == output.stat().st_size
    assert crc == zlib.crc32(payload) & 0xffffffff


def test_prepare_chunks_rotates_two_files_and_preserves_offsets(tmp_path):
    payload = b"AAAABBBB"
    args = SimpleNamespace(buffersize=4)
    input_stream = io.BytesIO(payload)
    free_slots = xt_imager.queue.Queue()
    results = xt_imager.queue.Queue(maxsize=2)
    abort = xt_imager.threading.Event()

    for slot in range(2):
        free_slots.put(slot)

    producer = xt_imager.threading.Thread(
        target=xt_imager.prepare_chunks,
        args=(args, input_stream, tmp_path, free_slots, results, abort,
              4, len(payload), None),
    )
    producer.start()

    first = results.get(timeout=2)
    first_data = gzip.decompress(
        (tmp_path / xt_imager.CHUNK_NAMES[first[2]]).read_bytes())
    free_slots.put(first[2])

    second = results.get(timeout=2)
    second_data = gzip.decompress(
        (tmp_path / xt_imager.CHUNK_NAMES[second[2]]).read_bytes())
    free_slots.put(second[2])

    assert results.get(timeout=2) is None
    producer.join(timeout=2)

    assert not producer.is_alive()
    assert (first[0], first[1], first_data) == (0, 4, b"AAAA")
    assert (second[0], second[1], second_data) == (4, 4, b"BBBB")


def test_get_scsi_device_capacity_parses_requested_device():
    output = """
Device 0:
Capacity: 384.0 MB (98304 x 4096)
Device 1:
Capacity: 131072.0 MB (33554432 x 4096)
=>
"""

    capacity = xt_imager.get_scsi_device_capacity(output, 1)

    assert capacity == (33554432, 4096)


def test_prepare_ufs_target_rejects_unknown_block_size(monkeypatch):
    scan_output = "Device 1:\nCapacity: 1.0 MB (256 x 2048)\n=>"
    monkeypatch.setattr(xt_imager, "conn_send", lambda conn, data: None)
    monkeypatch.setattr(
        xt_imager,
        "conn_wait_for_any",
        lambda conn, expect: scan_output,
    )

    with pytest.raises(RuntimeError, match="unexpected block size 2048"):
        xt_imager.prepare_ufs_target(object(), "=>")


def test_confirm_ufs_flash_accepts_exact_confirmation(monkeypatch):
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *args, **kwargs: io.StringIO("FLASH UFS 1\n"),
    )

    xt_imager.confirm_ufs_flash(capacity_bytes=8192)


def test_confirm_ufs_flash_rejects_wrong_confirmation(monkeypatch):
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *args, **kwargs: io.StringIO("yes\n"),
    )

    with pytest.raises(RuntimeError, match="did not match"):
        xt_imager.confirm_ufs_flash(capacity_bytes=8192)


class FakeConnection:
    def read(self, size):
        return b""


def test_conn_wait_for_any_times_out():
    with pytest.raises(TimeoutError, match="Timeout waiting"):
        xt_imager.conn_wait_for_any(FakeConnection(), ["=>"])
