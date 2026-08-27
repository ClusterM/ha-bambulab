from __future__ import annotations

import os
import re

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .const import LOGGER

_CERT_BLOCK_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


def leaf_pem_from_chain(pem_text: str) -> str | None:
    """Return the first PEM certificate block from a PEM chain (or single cert)."""
    if not pem_text:
        return None
    match = _CERT_BLOCK_RE.search(pem_text)
    if not match:
        return None
    # Normalize to a single trailing newline for stable on-disk compares.
    return match.group(0).strip() + "\n"


def pem_from_der(der: bytes) -> str:
    """Convert a DER-encoded X.509 certificate to PEM text."""
    cert = x509.load_der_x509_certificate(der)
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def save_device_cert(path: str, leaf_pem: str) -> bool:
    """Atomically write leaf_pem to path. Returns True if the file was written/updated."""
    if not path or not leaf_pem:
        return False

    normalized = leaf_pem.strip() + "\n"
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            if existing.strip() == normalized.strip():
                return False

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(normalized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        LOGGER.error(f"Failed to save printer device cert to {path}: {type(e)} {e}")
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except OSError:
            pass
        return False


def load_device_cert(path: str) -> str | None:
    """Load a leaf device cert PEM from disk, or None if missing/unreadable."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return leaf_pem_from_chain(text)
    except Exception as e:
        LOGGER.error(f"Failed to load printer device cert from {path}: {type(e)} {e}")
        return None


def public_key_from_cert_pem(leaf_pem: str) -> RSAPublicKey | None:
    """Extract the RSA public key from a leaf device certificate PEM."""
    if not leaf_pem:
        return None
    try:
        cert = x509.load_pem_x509_certificate(leaf_pem.encode("utf-8"))
        key = cert.public_key()
        if not isinstance(key, RSAPublicKey):
            LOGGER.error(f"Printer device cert public key is not RSA: {type(key)}")
            return None
        return key
    except Exception as e:
        LOGGER.error(f"Failed to parse printer device cert public key: {type(e)} {e}")
        return None
