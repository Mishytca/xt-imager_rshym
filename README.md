# xt-imager

`xt-imager` flashes an uncompressed image stream to eMMC or UFS through the
U-Boot serial console and TFTP. It is intended for R-Car Gen5 boards.

The host uses two alternating chunk files. While U-Boot writes the current
chunk from RAM, the host prepares the next chunk.

Based on https://github.com/xen-troops/rcar_flash.

## Requirements

- A board with a working U-Boot console.
- A TFTP server accessible from U-Boot.
- A serial connection to the board.
- Python 3 with the `pyserial` module.
- Optional `pigz` for faster, parallel chunk compression.

On Ubuntu, install the host dependencies with:

```sh
sudo apt install python3-serial pigz
```

`pigz` is optional. Without it, the script uses the single-threaded Python
gzip implementation.

## Usage

The image must be supplied through standard input. Use `cat` for an
uncompressed image and `zcat` for a gzip-compressed image. The `--target`
argument is mandatory.

Raw image to UFS:

```sh
cat full.img | ./xt-imager.py --target ufs -s /dev/GEN5_CONSOLE3 -b 1843200
```

Raw image to eMMC:

```sh
cat full.img | ./xt-imager.py --target emmc -s /dev/GEN5_CONSOLE3 -b 1843200
```

Gzip image to UFS:

```sh
zcat full.img.gz | ./xt-imager.py --target ufs -s /dev/GEN5_CONSOLE3 -b 1843200
```

Gzip image to eMMC:

```sh
zcat full.img.gz | ./xt-imager.py --target emmc -s /dev/GEN5_CONSOLE3 -b 1843200
```

For the complete command-line help, run:

```sh
./xt-imager.py --help
```

## Options

- `--target {emmc,ufs}`: required destination storage.
- `-s DEVICE`, `--serial DEVICE`: U-Boot serial console; default
  `/dev/ttyUSB0`.
- `-b RATE`, `--baud RATE`: serial baud rate; default `921600`.
- `-t DIRECTORY`, `--tftp DIRECTORY`: TFTP root for temporary chunks; default
  `/srv/tftp`.
- `--loadaddr ADDRESS`: U-Boot RAM address used by TFTP; default `0x58000000`.
- `--mmcdev NUMBER`: U-Boot MMC device used for eMMC; default `0`.
- `--buffersize BYTES`: uncompressed chunk size; default 512 MiB. It must be
  positive and aligned to 512 bytes. For UFS it must also be aligned to the
  block size reported by U-Boot.
- `--serverip IP`: temporarily set the TFTP server IP in U-Boot.
- `--ipaddr IP`: temporarily set the board IP in U-Boot.

The script uses `env set`, not `env save`, so network and load-address changes
are not stored permanently in the U-Boot environment.

## UFS safety

> [!WARNING]
>
> UFS device 0 contains IPL boot data and must never be overwritten.

UFS flashing is fixed to SCSI device 1. Before writing, the script:

1. runs `scsi scan`;
2. reads the capacity and block size of device 1;
3. selects device 1 with `scsi device 1`;
4. verifies that U-Boot confirms the selected device;
5. requires the user to type `FLASH UFS 1` exactly.

The confirmation is read from `/dev/tty` because standard input is occupied by
the image stream. Image capacity and UFS block alignment are checked one chunk
at a time while the stream is processed.

## Chunk pipeline

The TFTP root contains two temporary files during flashing:

```text
chunk0.bin.gz
chunk1.bin.gz
```

For every chunk, the script:

1. reads uncompressed bytes from standard input;
2. compresses them with `pigz` or Python gzip;
3. transfers the gzip file into U-Boot RAM over TFTP;
4. writes the uncompressed data with `gzwrite mmc` or `gzwrite scsi 1`;
5. checks the transferred size and CRC reported by U-Boot.

After TFTP finishes, the corresponding file can be reused because `gzwrite`
reads the current chunk from board RAM. This allows preparation of the next
chunk to overlap the current device write. Temporary files are removed when
the script exits.

## Progress

The complete input size is unknown for a pipe, so progress is reported as the
number of uncompressed bytes successfully written:

```text
[Progress: 24_442_306_560]
[Total time: 434.7s]
[Image was flashed successfully]
```
