from __future__ import annotations

import base64
import json

from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from .const import LOGGER

SIGN_ALG = "RSA_SHA256"
SIGN_VER = "v1.0"
PRINT_FIELDS_TO_ENCRYPT = ["url", "param"]


def load_signing_key(pem: str) -> RSAPrivateKey | None:
    """Parse a PEM-encoded RSA private key. Returns None on any failure."""
    if not pem:
        return None
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as e:
        LOGGER.error(f"Failed to load slicer signing key: {type(e)} {e}")
        return None


def _is_print_payload(msg: Any) -> bool:
    """Mirror the C++ is_print_payload: the first JSON key must be 'print'."""
    if not isinstance(msg, dict) or not msg:
        return False
    return 'print' in msg

def _rsa_pkcs1v15_encrypt_b64(key: RSAPublicKey, plaintext: bytes) -> str:
    """Blockwise RSA-PKCS#1 v1.5 encryption -> base64.

    Port of rsa_pkcs1v15_encrypt_b64: splits plaintext into chunks of at most
    (key_bytes - 11) so the ciphertext is a concatenation of key-sized blocks
    (matching the stock plugin's url_enc / param_enc form). A zero-length input
    still produces one block.
    """
    key_bytes = (key.key_size + 7) // 8
    if key_bytes <= 11:
        raise ValueError("RSA key too small")
    max_chunk = key_bytes - 11

    out = bytearray()
    offset = 0
    remaining = len(plaintext)
    # Zero-length input still produces one block.
    while True:
        chunk = remaining if remaining < max_chunk else max_chunk
        block = key.encrypt(plaintext[offset:offset + chunk], padding.PKCS1v15())
        out.extend(block)
        offset += chunk
        remaining -= chunk
        if remaining <= 0:
            break

    return base64.b64encode(out).decode("ascii")

def maybe_sign(
    msg: dict,
    cert_id: str,
    key: RSAPrivateKey | None,
    device_key: RSAPublicKey | None = None,
) -> dict:
    """Return the message to publish, signing 'print' payloads.

    Only messages whose first key is 'print' receive a signed envelope; all
    others (and any failure) pass through unchanged. This is a port of
    open-bamboo-networking's maybe_sign/build_envelope.

    When device_key is set, encrypts PRINT_FIELDS_TO_ENCRYPT values with the
    printer device-cert public key (RSA-PKCS#1 v1.5, chunked) into *_enc fields.
    """
    if key is None or not cert_id or not _is_print_payload(msg):
        return msg

    if device_key is None:
        LOGGER.debug("maybe_sign: device_key is None; argument encryption unavailable")

    try:
        if device_key is not None and "print" in msg:
            for field in PRINT_FIELDS_TO_ENCRYPT:
                if field in msg["print"]:
                    field_bytes = msg["print"][field].encode("utf-8")
                    msg["print"][field + "_enc"] = _rsa_pkcs1v15_encrypt_b64(
                        device_key, field_bytes
                    )

        print_dump = json.dumps(msg["print"])
        to_sign = '{ "print": ' + print_dump + '}'
        to_sign_bytes = to_sign.encode("utf-8")

        signature = key.sign(to_sign_bytes, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode("ascii")

        # Build the envelope with a fixed key order matching the reference
        # implementation so the signed 'print' section stays byte-identical.
        return {
            "header": {
                "cert_id": cert_id,
                "payload_len": len(to_sign_bytes),
                "sign_alg": SIGN_ALG,
                "sign_string": sig_b64,
                "sign_ver": SIGN_VER
            },
            "print": json.loads(print_dump)
        }
    except Exception as e:
        LOGGER.error(f"Failed to sign print payload, sending unsigned: {type(e)} {e}")
        return msg
