#!/usr/bin/env python3
"""
bcurl.py - Binary HTTP command-line client.
Usage: python bcurl.py [-v|-vv] <host:port/path> [host:port/path ...]
"""

import sys
import os
import socket
import struct
from typing import List, Tuple, Optional

import common

USER_AGENT = "bcurl/1.0"


def parse_url(url_str: str) -> Tuple[str, int, str]:
    """
    Parses URLs in format 'host:port/path' or 'http://host:port/path'.
    Returns (host, port, path).
    """
    if "://" in url_str:
        url_str = url_str.split("://", 1)[1]

    if "/" in url_str:
        host_port, path_part = url_str.split("/", 1)
        path = "/" + path_part
    else:
        host_port = url_str
        path = "/"

    if ":" not in host_port:
        raise ValueError(f"URL missing port: '{url_str}' (expected host:port/path)")

    host, port_str = host_port.split(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"Invalid port in URL: '{port_str}'")

    return host, port, path


def read_response_frame(sock, verbose: int) -> Tuple[int, List[Tuple[str, str]], bytes]:
    """
    Reads frames until a RESPONSE frame is received (skips unknown frame types).
    Returns (flags, headers, body).
    """
    while True:
        # 1. Read 6-byte frame header
        hdr_bytes = common.recv_exact(sock, common.FRAME_HEADER_SIZE)
        (length, frame_type, flags) = struct.unpack("!IBB", hdr_bytes)

        # 2. Read full payload
        payload = common.recv_exact(sock, length)
        full_frame = hdr_bytes + payload

        if verbose > 0:
            truncate_after = None if verbose >= 2 else 4096
            hexdump_str = common.format_hexdump(full_frame, direction="<", truncate_after=truncate_after)
            print(hexdump_str, file=sys.stderr)

        # 3. If unknown frame type, skip it and continue waiting for RESPONSE
        if frame_type != common.TYPE_RESPONSE:
            if verbose > 0:
                print(f"< (Skipped unknown frame type 0x{frame_type:02x}, length {length})", file=sys.stderr)
            continue

        # 4. Decode payload
        headers, body = common.decode_payload(payload)
        return flags, headers, body


def main():
    args = sys.argv[1:]
    verbose = 0

    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-v":
            verbose = 1
        elif flag == "-vv":
            verbose = 2
        else:
            print(f"Unknown flag: {flag}", file=sys.stderr)
            print("Usage: python bcurl.py [-v|-vv] host:port/path [more URLs...]", file=sys.stderr)
            sys.exit(2)

    if not args:
        print("Usage: python bcurl.py [-v|-vv] host:port/path [more URLs...]", file=sys.stderr)
        sys.exit(2)

    # Parse all target URLs
    targets = []
    for raw_url in args:
        try:
            host, port, path = parse_url(raw_url)
            targets.append((host, port, path))
        except Exception as e:
            print(f"Error parsing URL '{raw_url}': {e}", file=sys.stderr)
            sys.exit(2)

    # Validate that all URLs target the exact same host:port (single connection rule)
    first_host, first_port, _ = targets[0]
    for h, p, _ in targets:
        if h != first_host or p != first_port:
            print(
                f"Error: Multiple URLs must share the same host:port ({first_host}:{first_port} vs {h}:{p})",
                file=sys.stderr,
            )
            sys.exit(2)

    # Open single TCP connection
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect((first_host, first_port))
    except Exception as e:
        print(f"Connection failed to {first_host}:{first_port}: {e}", file=sys.stderr)
        sys.exit(2)

    has_error = False

    try:
        total_urls = len(targets)
        for idx, (host, port, path) in enumerate(targets):
            is_last = (idx == total_urls - 1)
            flags = common.FLAG_CLOSE if is_last else 0

            headers = [
                (":method", "GET"),
                (":path", path),
                ("host", f"{host}:{port}"),
                ("user-agent", USER_AGENT),
            ]

            request_frame = common.encode_frame(common.TYPE_REQUEST, flags, headers, body=b"")

            # Hexdump outgoing request to stderr if -v
            if verbose > 0:
                truncate_after = None if verbose >= 2 else 4096
                dump_str = common.format_hexdump(request_frame, direction=">", truncate_after=truncate_after)
                print(dump_str, file=sys.stderr)

            # Send REQUEST frame
            sock.sendall(request_frame)

            # Receive RESPONSE frame
            resp_flags, resp_headers, body = read_response_frame(sock, verbose)
            resp_headers_dict = dict(resp_headers)

            status_str = resp_headers_dict.get(":status", "")
            if not status_str:
                print("Protocol Error: Missing :status in response", file=sys.stderr)
                sys.exit(2)

            # Validate Content-Length if present
            if "content-length" in resp_headers_dict:
                try:
                    cl = int(resp_headers_dict["content-length"])
                    if cl != len(body):
                        print(
                            f"Protocol Error: Content-Length ({cl}) does not match body size ({len(body)})",
                            file=sys.stderr,
                        )
                        sys.exit(2)
                except ValueError:
                    print("Protocol Error: Malformed Content-Length", file=sys.stderr)
                    sys.exit(2)

            # Handle Status Codes
            if status_str.startswith("2"):
                # Success: Write clean payload body directly to stdout
                sys.stdout.buffer.write(body)
                sys.stdout.buffer.flush()
            else:
                # 4xx or 5xx: Write error message to stderr
                has_error = True
                print(f"Error {status_str}:", file=sys.stderr)
                sys.stderr.buffer.write(body)
                sys.stderr.buffer.write(b"\n")
                sys.stderr.buffer.flush()

    except EOFError:
        print("Protocol Error: Connection closed prematurely by server", file=sys.stderr)
        sys.exit(2)
    except socket.timeout:
        print("Error: Socket timed out", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        sock.close()

    sys.exit(1 if has_error else 0)


if __name__ == "__main__":
    main()
