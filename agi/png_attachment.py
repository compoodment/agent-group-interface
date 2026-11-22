"""Bound and check a PNG attachment without decoding it into pixels."""

import os
import stat
import struct
import zlib

MAX_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16_000_000
SIGNATURE = b'\x89PNG\r\n\x1a\n'
BIT_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}


def read(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        size = source.nbytes if isinstance(source, memoryview) else len(source)
        if size > MAX_BYTES:
            raise ValueError('Image attachment exceeds 16 MiB')
        return bytes(source)
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Image attachment must be a regular file')
        if info.st_size > MAX_BYTES:
            raise ValueError('Image attachment exceeds 16 MiB')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Image attachment exceeds 16 MiB')
    return data


def check_size(width, height):
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError('Image attachment needs positive integer dimensions')
    if width * height > MAX_PIXELS:
        raise ValueError('Image attachment exceeds 16 million pixels')


def header(data):
    if len(data) < 33 or data[:16] != SIGNATURE + b'\x00\x00\x00\x0dIHDR':
        raise ValueError('Image attachment must be a PNG')
    width, height, depth, colour, compression, filter_type, interlace = struct.unpack_from('!IIBBBBB', data, 16)
    check_size(width, height)
    if depth not in BIT_DEPTHS.get(colour, ()) or compression or filter_type or interlace not in (0, 1):
        raise ValueError('Invalid PNG image header')
    return width, height


def dimensions(data):
    width, height = header(data)
    offset = len(SIGNATURE)
    payload = memoryview(data)
    image_data = False
    while offset + 12 <= len(data):
        size, kind = struct.unpack_from('!I4s', data, offset)
        end = offset + 8 + size
        if end + 4 > len(data):
            break
        crc, = struct.unpack_from('!I', data, end)
        if zlib.crc32(payload[offset + 4:end]) != crc:
            raise ValueError('Invalid PNG chunk checksum')
        if kind == b'IDAT' and size:
            image_data = True
        if kind == b'IEND':
            if image_data and size == 0 and end + 4 == len(data):
                return width, height
            break
        offset = end + 4
    raise ValueError('Incomplete PNG image attachment')
