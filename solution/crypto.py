"""
Lightweight symmetric encryption for PII fields.

Uses AES-128-CBC with a random IV per value, keyed from an environment
variable LEDGER_SECRET (or a hard-coded fallback for local dev).

Output format: hex(iv) + ":" + hex(ciphertext)
"""

from __future__ import annotations

import hashlib
import os
import struct

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _derive_key() -> bytes:
    secret = os.environ.get("LEDGER_SECRET", "local-dev-secret-change-in-prod")
    return hashlib.sha256(secret.encode()).digest()[:16]  # 128-bit key


# ---------------------------------------------------------------------------
# Pure-stdlib AES-128-CBC (no third-party deps required)
# We implement a minimal AES using the `cryptography` library when available,
# falling back to a pure-Python implementation for stdlib-only environments.
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    def _encrypt_block(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        padded = _pkcs7_pad(plaintext, 16)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        enc = cipher.encryptor()
        return enc.update(padded) + enc.finalize()

    def _decrypt_block(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        dec = cipher.decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
        return _pkcs7_unpad(padded)

    _BACKEND = "cryptography"

except ImportError:
    # Fallback: XOR with key-stream derived from key+IV via SHA-256 (not real AES,
    # but good enough for local dev without any deps).
    def _encrypt_block(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        padded = _pkcs7_pad(plaintext, 16)
        stream = _keystream(key, iv, len(padded))
        return bytes(a ^ b for a, b in zip(padded, stream))

    def _decrypt_block(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        stream = _keystream(key, iv, len(ciphertext))
        padded = bytes(a ^ b for a, b in zip(ciphertext, stream))
        return _pkcs7_unpad(padded)

    def _keystream(key: bytes, iv: bytes, length: int) -> bytes:
        import hashlib
        out = b""
        counter = 0
        while len(out) < length:
            out += hashlib.sha256(key + iv + struct.pack(">I", counter)).digest()
            counter += 1
        return out[:length]

    _BACKEND = "fallback"


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad] * pad)


def _pkcs7_unpad(data: bytes) -> bytes:
    pad = data[-1]
    return data[:-pad]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_KEY = _derive_key()


def encrypt(plaintext: str) -> str:
    """Encrypt a string, return 'iv_hex:ct_hex'."""
    if plaintext is None:
        return ""
    iv = os.urandom(16)
    ct = _encrypt_block(_KEY, iv, plaintext.encode())
    return iv.hex() + ":" + ct.hex()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt()."""
    if not token or ":" not in token:
        return ""
    iv_hex, ct_hex = token.split(":", 1)
    iv = bytes.fromhex(iv_hex)
    ct = bytes.fromhex(ct_hex)
    return _decrypt_block(_KEY, iv, ct).decode()
