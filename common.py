"""
common.py - Binary HTTP framing, serialization, socket helpers, and hexdump utility.
"""

import struct
from typing import List, Tuple, Optional

# --- Protocol Constants ---
FRAME_HEADER_SIZE = 6  # 4 bytes length + 1 byte type + 1 byte flags
MAX_REQUEST_LENGTH = 65536  # 64 KiB cap for requests

# Frame Types
TYPE_REQUEST = 0x01
TYPE_RESPONSE = 0x02

# Flags
FLAG_CLOSE = 0x01

# Static Header Table (ID -> Name and Name -> ID)
HEADER_ID_TO_NAME = {
    1: ":method",
    2: ":path",
    3: ":status",
    4: "host",
    5: "user-agent",
    6: "accept",
    7: "content-type",
    8: "content-length",
    9: "server",
    10: "connection",
}
HEADER_NAME_TO_ID = {name: hid for hid, name in HEADER_ID_TO_NAME.items()}


# --- Socket Helper ---
def recv_exact(sock, n: int) -> bytes:
    """
    Read exactly n bytes from the socket.
    Loops until all n bytes are received or EOF occurs.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("Connection closed unexpectedly before reading required bytes")
        buf.extend(chunk)
    return bytes(buf)


# --- Header Encoding & Decoding ---
def encode_headers(headers: List[Tuple[str, str]]) -> bytes:
    """
    Encodes a list of (name, value) headers into binary format.
    Format per header:
      - Indexed: [name_id : u8] [value_len : u16] [value]
      - Literal: [0x00] [name_len : u8] [name] [value_len : u16] [value]
    """
    out = bytearray()
    for name, value in headers:
        val_bytes = value.encode("ascii")
        if len(val_bytes) > 65535:
            raise ValueError(f"Header value too long ({len(val_bytes)} bytes)")

        if name in HEADER_NAME_TO_ID:
            name_id = HEADER_NAME_TO_ID[name]
            # [name_id : u8] [value_len : u16]
            out.extend(struct.pack("!BH", name_id, len(val_bytes)))
            out.extend(val_bytes)
        else:
            name_bytes = name.encode("ascii")
            if len(name_bytes) > 255:
                raise ValueError(f"Header name too long ({len(name_bytes)} bytes)")
            # [0x00] [name_len : u8] [name] [value_len : u16] [value]
            out.extend(struct.pack("!BB", 0x00, len(name_bytes)))
            out.extend(name_bytes)
            out.extend(struct.pack("!H", len(val_bytes)))
            out.extend(val_bytes)
    return bytes(out)


def decode_headers(data: bytes, header_count: int) -> Tuple[List[Tuple[str, str]], int]:
    """
    Decodes header_count headers from data.
    Returns (headers_list, bytes_consumed).
    Raises ValueError if data is malformed or truncated.
    """
    headers: List[Tuple[str, str]] = []
    offset = 0

    for _ in range(header_count):
        if offset >= len(data):
            raise ValueError("Truncated header block: expected more headers")

        name_id = data[offset]
        offset += 1

        if name_id != 0:
            # Indexed header
            if name_id not in HEADER_ID_TO_NAME:
                raise ValueError(f"Invalid indexed header ID: {name_id}")
            name = HEADER_ID_TO_NAME[name_id]

            if offset + 2 > len(data):
                raise ValueError("Truncated header: missing value length")
            (val_len,) = struct.unpack("!H", data[offset : offset + 2])
            offset += 2

            if offset + val_len > len(data):
                raise ValueError("Truncated header: missing value bytes")
            value = data[offset : offset + val_len].decode("ascii", errors="replace")
            offset += val_len
        else:
            # Literal header: [0x00] [name_len: u8] [name] [value_len: u16] [value]
            if offset >= len(data):
                raise ValueError("Truncated literal header: missing name length")
            name_len = data[offset]
            offset += 1

            if offset + name_len > len(data):
                raise ValueError("Truncated literal header: missing name bytes")
            name = data[offset : offset + name_len].decode("ascii", errors="replace")
            offset += name_len

            if offset + 2 > len(data):
                raise ValueError("Truncated literal header: missing value length")
            (val_len,) = struct.unpack("!H", data[offset : offset + 2])
            offset += 2

            if offset + val_len > len(data):
                raise ValueError("Truncated literal header: missing value bytes")
            value = data[offset : offset + val_len].decode("ascii", errors="replace")
            offset += val_len

        headers.append((name, value))

    return headers, offset


# --- Frame Encoding & Decoding ---
def encode_frame(frame_type: int, flags: int, headers: List[Tuple[str, str]], body: bytes = b"") -> bytes:
    """
    Constructs a complete frame: 6-byte header + payload (header_count + headers + body).
    """
    if len(headers) > 255:
        raise ValueError("Too many headers (max 255)")

    hdr_payload = encode_headers(headers)
    payload = bytes([len(headers)]) + hdr_payload + body
    length = len(payload)

    frame_header = struct.pack("!IBB", length, frame_type, flags)
    return frame_header + payload


def decode_payload(payload: bytes) -> Tuple[List[Tuple[str, str]], bytes]:
    """
    Parses a payload into (headers, body).
    """
    if len(payload) < 1:
        raise ValueError("Empty payload: missing header_count")

    header_count = payload[0]
    headers, consumed = decode_headers(payload[1:], header_count)
    body = payload[1 + consumed :]
    return headers, body


# --- Hexdump Formatter (-v / -vv) ---
def format_hexdump(data: bytes, direction: str = "", truncate_after: Optional[int] = None) -> str:
    """
    Produces an xxd-style annotated hexdump.
    direction: '>' for client sent, '<' for client received.
    """
    lines = []
    total_len = len(data)
    display_len = total_len
    truncated = False

    if truncate_after is not None and total_len > truncate_after:
        display_len = min(256, total_len)
        truncated = True

    for i in range(0, display_len, 16):
        chunk = data[i : i + 16]
        hex_parts = [f"{b:02x}" for b in chunk]
        # Pad hex display if line is short
        hex_str = " ".join(hex_parts).ljust(48)
        # ASCII display
        ascii_str = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        prefix = f"{direction} " if direction else ""
        lines.append(f"{prefix}{i:08x}: {hex_str}  {ascii_str}")

    if truncated:
        omitted = total_len - display_len
        prefix = f"{direction} " if direction else ""
        lines.append(f"{prefix}(... {omitted} bytes not shown)")

    return "\n".join(lines)
