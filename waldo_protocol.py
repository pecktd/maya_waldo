"""Wire protocol shared by the tracker process and the Maya panel.

Deliberately stdlib-only: this module is imported by both the tracker
(system Python 3.12) and Maya 2024's bundled Python 3.10.

Frame layout on the wire::

    [4 bytes big-endian: header length]
    [header length bytes: utf-8 JSON]
    [jpeg_len bytes: raw JPEG, only if header["jpeg_len"] > 0]

The JSON header always carries the gesture state; the JPEG is the webcam
preview and may be omitted (jpeg_len 0) to save bandwidth.
"""

import json
import socket
import struct

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5599

_LEN = struct.Struct(">I")


def send_message(sock, header, jpeg=None):
    """Send one header/JPEG pair on a connected socket."""
    header = dict(header)
    header["jpeg_len"] = len(jpeg) if jpeg else 0
    blob = json.dumps(header).encode("utf-8")

    parts = [_LEN.pack(len(blob)), blob]
    if jpeg:
        parts.append(jpeg)
    sock.sendall(b"".join(parts))


def _recv_exactly(sock, count):
    """Read exactly `count` bytes, or return None if the peer hung up."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock):
    """Read one message. Returns (header, jpeg_bytes_or_None), or None on EOF."""
    raw_len = _recv_exactly(sock, _LEN.size)
    if raw_len is None:
        return None

    (blob_len,) = _LEN.unpack(raw_len)
    blob = _recv_exactly(sock, blob_len)
    if blob is None:
        return None
    header = json.loads(blob.decode("utf-8"))

    jpeg = None
    if header.get("jpeg_len"):
        jpeg = _recv_exactly(sock, header["jpeg_len"])
        if jpeg is None:
            return None

    return header, jpeg


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Listening socket for the Maya side to accept the tracker's connection."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    return server
