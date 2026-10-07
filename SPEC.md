# Binary HTTP Protocol Specification (v1.0)
## 1. Overview
Binary HTTP is a lightweight, binary-framed application layer protocol for file transfer over a single persistent TCP connection. All multi-byte integers are transmitted in network byte order (Big-Endian).
## 2. Frame Header Format
Every message begins with a fixed 6-byte header:
+--------------------------------+---------------+---------------+ | Length (32) | Type (8) | Flags (8) | +--------------------------------+---------------+---------------+

- **Length (32 bits / 4 bytes, unsigned integer)**: Byte length of payload immediately following the 6-byte header. Max 4,294,967,295 bytes.
- **Type (8 bits / 1 byte)**:
  - `0x01`: `REQUEST` (Client -> Server)
  - `0x02`: `RESPONSE` (Server -> Client)
  - `0x03`–`0xFF`: Reserved for future versions.
- **Flags (8 bits / 1 byte)**:
  - Bit 0 (`0x01`): `CLOSE`. Indicates that the sender will close the connection after this frame.
  - Bits 1–7: Reserved; senders MUST set to 0, receivers MUST ignore.
## 3. Payload Layout
The payload layout is identical for both `REQUEST` and `RESPONSE`:
+-------------------+--------------------+ ... +--------------------+-------------------+ | Header Count (u8) | Header 1 (var-len) | ... | Header N (var-len) | Body (var-len) | +-------------------+--------------------+ ... +--------------------+-------------------+

- **Header Count (`u8`)**: 1 byte specifying the number of headers (0–255).
- **Headers**: Sequence of encoded key-value pairs.
- **Body**: The remainder of the payload (`length - bytes_used_by_headers`). A request MUST have an empty body.
## 4. Header Encoding
Headers are encoded in one of two formats:
1. **Indexed Header** (Known header names with ID 1–10):
[name_id : u8 (1..10)] [value_len : u16] [value_bytes]

2. **Literal Header** (Custom / unindexed header names):
[0x00 : u8] [name_len : u8] [name_bytes] [value_len : u16] [value_bytes]

### Static Header Table
| ID | Name | Direction / Role |
|:---|:---|:---|
| 1 | `:method` | REQUEST (e.g., `GET`) |
| 2 | `:path` | REQUEST (e.g., `/index.html`) |
| 3 | `:status` | RESPONSE (e.g., `200`) |
| 4 | `host` | REQUEST |
| 5 | `user-agent` | REQUEST |
| 6 | `accept` | REQUEST |
| 7 | `content-type` | RESPONSE |
| 8 | `content-length` | RESPONSE (MUST match body size) |
| 9 | `server` | RESPONSE |
| 10 | `connection` | Either (`keep-alive` or `close`) |
Values are raw ASCII octets. Max header value length is 65,535 bytes (`u16`).
## 5. Protocol Semantics & Rules
- **Method Support**: Only `GET` is supported. Non-GET methods MUST yield status `405`.
- **Path Mapping & Safety**:
- `:path` MUST start with `/`.
- `/` maps to `/index.html`.
- Paths containing `..` or `NUL` (`\x00`) MUST immediately receive `400 Bad Request`.
- Paths resolving outside the document root (via symlinks or canonicalization) MUST receive `404 Not Found`.
- **Unknown Frame Types**: If a receiver encounters an unknown `type`, it MUST read and discard `length` bytes, send no reply, and keep the connection open.
- **Request Size Cap**: Senders MUST NOT send `REQUEST` frames with `length > 65536` (64 KiB). Receivers encountering this MUST reply with `400 Bad Request` with the `CLOSE` flag set, then close the connection.
- **Connection Lifetime**:
- Connections are persistent by default.
- Senders set the `CLOSE` flag (`0x01`) on their final frame.
- Receivers MUST close after processing a frame with the `CLOSE` flag.
- Servers SHOULD close connections after 30 seconds of inactivity (idle timeout).
- **Error Responses**:
- `400 Bad Request`: Malformed headers, invalid path format, length > 64 KiB.
- `404 Not Found`: File does not exist, resolves to a directory, or escapes root.
- `405 Method Not Allowed`: Method is not `GET`.
- Error responses carry a short plain-text body describing the error.
