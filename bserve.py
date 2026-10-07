#!/usr/bin/env python3
"""
bserve.py - Binary HTTP multi-threaded static file server.
Usage: python bserve.py <document_root> <port>
"""

import sys
import os
import socket
import struct
import threading
from typing import Dict, Tuple

import common

# MIME Type mapping based on file extensions
MIME_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
DEFAULT_MIME_TYPE = "application/octet-stream"
SERVER_NAME = "bserve/1.0"
IDLE_TIMEOUT_SECONDS = 30.0


def get_content_type(file_path: str) -> str:
    """Derive MIME type from file extension."""
    _, ext = os.path.splitext(file_path.lower())
    return MIME_TYPES.get(ext, DEFAULT_MIME_TYPE)


def build_response(status_code: str, body: bytes, content_type: str = "text/plain", close_conn: bool = False) -> bytes:
    """Constructs a binary RESPONSE frame."""
    flags = common.FLAG_CLOSE if close_conn else 0
    headers = [
        (":status", status_code),
        ("content-type", content_type),
        ("content-length", str(len(body))),
        ("server", SERVER_NAME),
    ]
    if close_conn:
        headers.append(("connection", "close"))
    else:
        headers.append(("connection", "keep-alive"))

    return common.encode_frame(common.TYPE_RESPONSE, flags, headers, body)


def safe_resolve_path(doc_root_real: str, request_path: str) -> Tuple[int, str]:
    """
    Validates and maps a request path to a local filesystem file.
    Returns (status_code, resolved_file_path_or_error_msg).
    Status codes: 200 (OK), 400 (Malformed/Bad Request), 404 (Not Found).
    """
    # Rule 1: Path must start with /
    if not request_path.startswith("/"):
        return 400, "Path must start with '/'"

    # Rule 2: Path must not contain NUL or '..'
    if "\x00" in request_path or ".." in request_path:
        return 400, "Path contains invalid characters or traversal segments"

    # Map '/' to '/index.html'
    rel_path = request_path[1:]
    if rel_path == "" or rel_path.endswith("/"):
        rel_path += "index.html"

    # Normalize system path separators
    system_rel_path = rel_path.replace("/", os.sep)
    candidate_path = os.path.join(doc_root_real, system_rel_path)

    # Resolve symlinks and canonical path
    resolved_path = os.path.realpath(candidate_path)

    # Rule 3: Must stay under document root
    try:
        common_prefix = os.path.commonpath([doc_root_real, resolved_path])
        if common_prefix != doc_root_real:
            return 404, "File not found"
    except ValueError:
        return 404, "File not found"

    # Rule 4: Must exist and be a regular file (not directory)
    if not os.path.exists(resolved_path) or not os.path.isfile(resolved_path):
        return 404, "File not found"

    return 200, resolved_path


def discard_bytes(sock, length: int):
    """Discard unknown frame payload in 4 KiB chunks without allocating memory."""
    remaining = length
    while remaining > 0:
        chunk_size = min(remaining, 4096)
        chunk = common.recv_exact(sock, chunk_size)
        remaining -= len(chunk)


def handle_client(sock: socket.socket, addr: Tuple[str, int], doc_root_real: str):
    """Worker thread handling a single persistent client TCP connection."""
    sock.settimeout(IDLE_TIMEOUT_SECONDS)

    try:
        while True:
            # Step 1: Read fixed 6-byte frame header
            try:
                hdr_bytes = common.recv_exact(sock, common.FRAME_HEADER_SIZE)
            except EOFError:
                # Clean EOF between requests
                break

            (length, frame_type, flags) = struct.unpack("!IBB", hdr_bytes)
            is_client_close = bool(flags & common.FLAG_CLOSE)

            # Step 2: Handle Unknown Frame Types (MUST skip payload and keep connection open)
            if frame_type != common.TYPE_REQUEST:
                discard_bytes(sock, length)
                if is_client_close:
                    break
                continue

            # Step 3: Check Request Length Cap (> 64 KiB -> 400 + CLOSE)
            if length > common.MAX_REQUEST_LENGTH:
                resp = build_response("400", b"Request payload exceeds 64 KiB limit", close_conn=True)
                sock.sendall(resp)
                break

            # Step 4: Read Payload exactly
            try:
                payload = common.recv_exact(sock, length)
            except EOFError:
                # Abrupt EOF during payload
                break

            # Step 5: Parse Request Headers
            try:
                headers_list, body = common.decode_payload(payload)
                headers_dict: Dict[str, str] = dict(headers_list)
            except Exception:
                # Malformed payload structure -> 400 Bad Request (connection stays open)
                resp = build_response("400", b"Malformed request headers", close_conn=is_client_close)
                sock.sendall(resp)
                if is_client_close:
                    break
                continue

            # Step 6: Validate HTTP Method & Path
            method = headers_dict.get(":method")
            path = headers_dict.get(":path")

            if not method or not path:
                resp = build_response("400", b"Missing :method or :path pseudo-header", close_conn=is_client_close)
                sock.sendall(resp)
                if is_client_close:
                    break
                continue

            if method != "GET":
                resp = build_response("405", b"Method Not Allowed (only GET is supported)", close_conn=is_client_close)
                sock.sendall(resp)
                if is_client_close:
                    break
                continue

            # Step 7: Resolve and read file
            status_code, result = safe_resolve_path(doc_root_real, path)
            if status_code == 200:
                try:
                    with open(result, "rb") as f:
                        file_data = f.read()
                    content_type = get_content_type(result)
                    resp = build_response("200", file_data, content_type=content_type, close_conn=is_client_close)
                except Exception as e:
                    resp = build_response("500", f"Internal read error: {e}".encode("utf-8"), close_conn=is_client_close)
            elif status_code == 400:
                resp = build_response("400", result.encode("utf-8"), close_conn=is_client_close)
            else:  # 404
                resp = build_response("404", b"404 Not Found", close_conn=is_client_close)

            sock.sendall(resp)

            # Step 8: If client requested CLOSE, exit connection loop
            if is_client_close:
                break

    except socket.timeout:
        pass  # 30-second idle timeout expired
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <document_root> <port>", file=sys.stderr)
        sys.exit(2)

    doc_root = sys.argv[1]
    try:
        port = int(sys.argv[2])
    except ValueError:
        print(f"Error: Invalid port '{sys.argv[2]}'", file=sys.stderr)
        sys.exit(2)

    if not os.path.isdir(doc_root):
        print(f"Error: Document root '{doc_root}' does not exist or is not a directory", file=sys.stderr)
        sys.exit(2)

    doc_root_real = os.path.realpath(doc_root)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_sock.bind(("0.0.0.0", port))
        server_sock.listen(128)
        print(f"bserve listening on port {port} (root: {doc_root_real})", file=sys.stderr)

        while True:
            client_sock, addr = server_sock.accept()
            t = threading.Thread(target=handle_client, args=(client_sock, addr, doc_root_real), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\nShutting down bserve.", file=sys.stderr)
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()
