# Build Phases
 
- **Phase 1: The Protocol Specification (`SPEC.md`)**: Define the contract before coding.
- **Phase 2: The Protocol Core (`common.py`)**: `recv_exact`, binary framing, header indexing, and hexdumping.
- **Phase 3: The Server (`bserve.py`)**: TCP sockets, threading, request parsing, path validation, and 30s timeouts.
- **Phase 4: The Client (`bcurl.py`)**: Single persistent connection, multi-URL pipelining, and `-v` formatting.
- **Phase 5: Conformance Testing (`tests/test_wire.py`)**: Proving the protocol with raw hex-byte assertions.
- **Phase 6: Annotated Hexdump (`HEXDUMP.md`)**: Capturing live wire traffic and byte-by-byte breakdown.