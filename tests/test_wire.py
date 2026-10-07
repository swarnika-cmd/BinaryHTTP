"""
tests/test_wire.py - Protocol conformance tests using raw bytes and sockets.
CRITICAL: This file MUST NOT import common.py.
"""

import os
import sys
import time
import socket
import struct
import subprocess
import unittest

PORT = 9876
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "www"))


def raw_recv_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from socket using raw bytearray."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("Socket closed")
        buf.extend(chunk)
    return bytes(buf)


def raw_read_frame(sock):
    """Parse a single raw frame without common.py."""
    hdr = raw_recv_exact(sock, 6)
    (length, ftype, flags) = struct.unpack("!IBB", hdr)
    payload = raw_recv_exact(sock, length)
    return length, ftype, flags, payload


def extract_header(payload: bytes, target_id: int):
    """Helper to extract a header value by ID from raw payload."""
    count = payload[0]
    offset = 1
    for _ in range(count):
        hid = payload[offset]
        offset += 1
        if hid != 0:
            (vlen,) = struct.unpack("!H", payload[offset : offset + 2])
            offset += 2
            val = payload[offset : offset + vlen].decode("ascii")
            offset += vlen
            if hid == target_id:
                return val
        else:
            nlen = payload[offset]
            offset += 1 + nlen
            (vlen,) = struct.unpack("!H", payload[offset : offset + 2])
            offset += 2 + vlen
    return None


class TestBinaryHTTPWire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create test files
        os.makedirs(ROOT_DIR, exist_ok=True)
        with open(os.path.join(ROOT_DIR, "test.txt"), "wb") as f:
            f.write(b"Hello Binary HTTP!\n")

        # Launch server subprocess
        server_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bserve.py"))
        cls.server_proc = subprocess.Popen(
            [sys.executable, server_py, ROOT_DIR, str(PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.server_proc.terminate()
        cls.server_proc.wait()

    def get_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(("127.0.0.1", PORT))
        return s

    def test_01_normal_file(self):
        """Valid GET request for /test.txt returns 200 and exact content."""
        s = self.get_socket()
        # Hand-crafted GET /test.txt:
        # length=23 (0x17), type=1 (REQUEST), flags=1 (CLOSE)
        # header_count=2
        # id=1 (:method), len=3, "GET"
        # id=2 (:path), len=9, "/test.txt"
        req = bytes.fromhex("00000013" "01" "01" "02" "010003474554" "0200092f746573742e747874")
        s.sendall(req)

        _, ftype, _, payload = raw_read_frame(s)
        self.assertEqual(ftype, 2)  # RESPONSE
        status = extract_header(payload, 3)  # :status
        self.assertEqual(status, "200")
        self.assertTrue(payload.endswith(b"Hello Binary HTTP!\n"))
        s.close()

    def test_02_missing_file_404(self):
        """Request for non-existent file returns 404."""
        s = self.get_socket()
        # GET /notfound.txt
        req = bytes.fromhex("00000017" "01" "01" "02" "010003474554" "02000d2f6e6f74666f756e642e747874")
        s.sendall(req)

        _, _, _, payload = raw_read_frame(s)
        status = extract_header(payload, 3)
        self.assertEqual(status, "404")
        s.close()

    def test_03_path_traversal_400(self):
        """Path with '..' or NUL returns 400 Bad Request."""
        s = self.get_socket()
        # GET /../secret.txt
        req = bytes.fromhex("00000018" "01" "00" "02" "010003474554" "02000e2f2e2e2f7365637265742e747874")
        s.sendall(req)

        _, _, _, payload = raw_read_frame(s)
        status = extract_header(payload, 3)
        self.assertEqual(status, "400")
        s.close()

    def test_04_non_get_method_405(self):
        """Method POST returns 405 Method Not Allowed."""
        s = self.get_socket()
        # POST /test.txt
        req = bytes.fromhex("00000014" "01" "01" "02" "010004504f5354" "0200092f746573742e747874")
        s.sendall(req)

        _, _, _, payload = raw_read_frame(s)
        status = extract_header(payload, 3)
        self.assertEqual(status, "405")
        s.close()

    def test_05_unknown_frame_type_skipping(self):
        """Server skips unknown frame type (0x7F) and processes subsequent request on same socket."""
        s = self.get_socket()
        # Unknown frame: length=20, type=0x7F, flags=0x00, body=20 bytes of 0xAA
        unknown_frame = struct.pack("!IBB", 20, 0x7F, 0x00) + (b"\xaa" * 20)
        # Valid GET /test.txt
        valid_req = bytes.fromhex("00000013" "01" "01" "02" "010003474554" "0200092f746573742e747874")

        # Send both together
        s.sendall(unknown_frame + valid_req)

        # Server should NOT answer the 0x7F frame, but MUST answer the valid request
        _, ftype, _, payload = raw_read_frame(s)
        self.assertEqual(ftype, 2)
        status = extract_header(payload, 3)
        self.assertEqual(status, "200")
        s.close()

    def test_06_fragmented_delivery(self):
        """Request sent in tiny chunks with delays still succeeds."""
        s = self.get_socket()
        req = bytes.fromhex("00000013" "01" "01" "02" "010003474554" "0200092f746573742e747874")

        # Send byte by byte with micro-sleeps
        for b in req:
            s.sendall(bytes([b]))
            time.sleep(0.01)

        _, ftype, _, payload = raw_read_frame(s)
        status = extract_header(payload, 3)
        self.assertEqual(status, "200")
        s.close()

    def test_07_pipelining(self):
        """Multiple back-to-back requests in one send() receive ordered responses."""
        s = self.get_socket()
        req1 = bytes.fromhex("00000013" "01" "00" "02" "010003474554" "0200092f746573742e747874")
        req2 = bytes.fromhex("00000013" "01" "00" "02" "010003474554" "0200092f746573742e747874")
        req3 = bytes.fromhex("00000013" "01" "01" "02" "010003474554" "0200092f746573742e747874")

        # Send 3 requests pipelined in one batch
        s.sendall(req1 + req2 + req3)

        for _ in range(3):
            _, ftype, _, payload = raw_read_frame(s)
            self.assertEqual(ftype, 2)
            self.assertEqual(extract_header(payload, 3), "200")
        s.close()

    def test_08_large_file_transfer(self):
        """Proves 32-bit length handling by transferring a 17 MB file (>16 MiB HTTP/2 cap)."""
        big_path = os.path.join(ROOT_DIR, "big.bin")
        chunk = b"A" * (1024 * 1024)  # 1 MB
        with open(big_path, "wb") as f:
            for _ in range(17):  # 17 MB
                f.write(chunk)

        try:
            s = self.get_socket()
            # GET /big.bin
            req = bytes.fromhex("00000012" "01" "01" "02" "010003474554" "0200082f6269672e62696e")
            s.sendall(req)

            length, ftype, _, payload = raw_read_frame(s)
            self.assertEqual(ftype, 2)
            self.assertEqual(extract_header(payload, 3), "200")
            # Verify body length is 17 MB
            body = payload[payload.find(b"server/1.0") + 10 :]  # After headers
            self.assertTrue(length > 17 * 1024 * 1024)
            s.close()
        finally:
            if os.path.exists(big_path):
                os.remove(big_path)


if __name__ == "__main__":
    unittest.main()
