"""Client-side E2E crypto: key derivation, chunk AEAD, DK wrapping.

WHY this exists as one module: the worker and viewer must never hold key
material, so every operation that touches S, K_enc, or a DK lives here, and
only here imports ``cryptography``.

The byte contract is pinned in ``docs/E2E-CONTRACT.md`` (sections 1-3) and
carries WebCrypto-generated test vectors, so a change to any constant or
encoding below is an interop break with the browser viewer, not a refactor.

Two derivation facts carry the whole security argument:

* The pasted ``ezu_``/``ezr_`` key IS the key material (secret S). The wire
  bearer is ``HKDF(S, "ezup/v1/auth")`` and the encryption key is
  ``HKDF(S, "ezup/v1/enc")`` -- domain-separated outputs of one extract, so a
  server that stores ``sha256(bearer)`` and sees every request still has
  nothing that leads back to S or to K_enc. Authentication and decryption
  capability are split by construction, not by policy.

* Chunk nonces are ``BE4(gen) || BE8(offset)`` -- deterministic on purpose.
  Retries and reconciliation re-encrypt the same plaintext at the same
  address and must produce byte-identical ciphertext (the server dedupes on
  sha256). The GCM-fatal case, different plaintext under a repeated nonce,
  is excluded by RULE R1: any path that could re-send different bytes at an
  already-encrypted offset rotates to a fresh DK at a strictly higher ``gen``
  first. That rule lives in ``publish.PublishState.reset()``; this module
  additionally refuses ``gen == 0`` so a plaintext-state generation can never
  encrypt anything.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

GCM_TAG = 16                     # bytes appended by AES-GCM
WRAP_LEN = 60                    # 12 nonce + 32 ct + 16 tag
ENC_SCHEME = "aead-v1"           # sessions.enc value for this contract
HKDF_SALT = b"ezup/v1/salt"
INFO_AUTH = b"ezup/v1/auth"
INFO_ENC = b"ezup/v1/enc"
AAD_CHUNK = b"ezup/v1/chunk"
AAD_WRAP = b"ezup/v1/wrap"
BEARER_PREFIX = "ezw_"
KEY_PREFIXES = ("ezu_", "ezr_")  # device, reader

_KIND_BY_PREFIX = {"ezu_": "device", "ezr_": "reader"}
_PREFIX_BY_KIND = {kind: prefix for prefix, kind in _KIND_BY_PREFIX.items()}
_HEX_DIGITS = frozenset("0123456789abcdef")


class CryptoError(Exception):
    """Any crypto failure a caller should surface: bad key format, failed
    GCM tag, wrong blob length. Wraps ``cryptography``'s InvalidTag so callers
    never import that package."""


@dataclass(frozen=True)
class KeySet:
    """Everything derivable from one pasted key. ``enc_key`` never serializes."""

    kind: str        # "device" (ezu_) or "reader" (ezr_)
    bearer: str      # "ezw_" + hex(K_auth); safe to send, cannot decrypt
    enc_key: bytes   # K_enc, 32 bytes; never sent, never written to disk
    keyid: str       # hex(sha256(bearer))[:16]; public fingerprint

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        # A stray repr in a log or traceback must not leak K_enc.
        return f"KeySet(kind={self.kind!r}, keyid={self.keyid!r})"


def _hkdf(secret: bytes, info: bytes) -> bytes:
    """One 32-byte HKDF-SHA-256 output. A fresh HKDF object per call because
    the primitive is single-use by API design."""
    return HKDF(algorithm=SHA256(), length=32, salt=HKDF_SALT, info=info).derive(secret)


def parse_key(pasted: str) -> KeySet:
    """Derive a KeySet. Raise CryptoError unless ``pasted`` is ezu_/ezr_ + 64
    lowercase hex."""
    if not isinstance(pasted, str) or len(pasted) != 68:
        raise CryptoError(
            "not a pasted key: expected ezu_/ezr_ followed by 64 lowercase hex chars"
        )
    prefix, hex_part = pasted[:4], pasted[4:]
    kind = _KIND_BY_PREFIX.get(prefix)
    # Uppercase hex is rejected rather than normalised: two spellings of one
    # key would derive the same K_auth but present different pasted strings,
    # and "exactly one canonical form" is cheaper than reasoning about aliases.
    if kind is None or not set(hex_part) <= _HEX_DIGITS:
        raise CryptoError(
            "not a pasted key: expected ezu_/ezr_ followed by 64 lowercase hex chars"
        )
    secret = bytes.fromhex(hex_part)  # IKM is the raw 32 bytes, never the ASCII hex
    bearer = BEARER_PREFIX + _hkdf(secret, INFO_AUTH).hex()
    return KeySet(
        kind=kind,
        bearer=bearer,
        enc_key=_hkdf(secret, INFO_ENC),
        # First 16 hex chars of the exact token_sha256 the server stores: a
        # public fingerprint a human can match against a devices row (D2).
        keyid=hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:16],
    )


def generate_key(kind: str) -> tuple[str, KeySet]:
    """Mint a fresh pasted key ('device' -> ezu_, 'reader' -> ezr_) from
    secrets.token_bytes(32). Returns (pasted, keyset); the pasted string is
    printed once by the CLI and exists nowhere else."""
    prefix = _PREFIX_BY_KIND.get(kind)
    if prefix is None:
        raise CryptoError(f"unknown key kind {kind!r}: expected 'device' or 'reader'")
    pasted = prefix + secrets.token_bytes(32).hex()
    return pasted, parse_key(pasted)


def bearer_sha256(pasted_or_bearer: str) -> str:
    """hex(sha256(bearer)): what a mint request registers server-side.
    Accepts a pasted ezu_/ezr_ key (derives the bearer first) or a raw
    ezw_ bearer."""
    if isinstance(pasted_or_bearer, str) and pasted_or_bearer.startswith(BEARER_PREFIX):
        bearer = pasted_or_bearer
    else:
        bearer = parse_key(pasted_or_bearer).bearer
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


def new_data_key() -> bytes:
    """secrets.token_bytes(32)."""
    return secrets.token_bytes(32)


def _check_gen_offset(gen: int, offset: int) -> None:
    # gen == 0 means "plaintext state" everywhere else in the system, so a
    # nonce built from it would collide with nothing today -- but allowing it
    # would let a caller skip the R1 bump and start every session at the same
    # generation. Refusing here makes the rotation rule structural.
    if not 1 <= gen < 2**32:
        raise CryptoError(f"generation {gen} out of range: must be 1..2^32-1")
    if not 0 <= offset < 2**64:
        raise CryptoError(f"offset {offset} out of range for a BE8 nonce field")


def chunk_nonce(gen: int, offset: int) -> bytes:
    """BE4(gen) || BE8(offset), 12 bytes."""
    _check_gen_offset(gen, offset)
    return gen.to_bytes(4, "big") + offset.to_bytes(8, "big")


def chunk_aad(session: str, gen: int, offset: int) -> bytes:
    """AAD_CHUNK || 0x00 || utf8(session) || 0x00 || BE4(gen) || BE8(offset).

    Session ids match the worker's SAFE_ID / the client's _COMPONENT grammar,
    so the utf8 field can never contain a NUL and the framing is unambiguous.
    Binding session+gen+offset into the tag is what stops a malicious store
    from splicing a validly-encrypted chunk into another session or offset.
    """
    _check_gen_offset(gen, offset)
    return (
        AAD_CHUNK + b"\x00" + session.encode("utf-8") + b"\x00"
        + gen.to_bytes(4, "big") + offset.to_bytes(8, "big")
    )


def _key_check(key: bytes, name: str) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise CryptoError(f"{name} must be exactly 32 bytes")


def encrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  plaintext: bytes) -> bytes:
    """AES-256-GCM(dk, chunk_nonce(gen, offset), plaintext, chunk_aad(...)).
    Returns ct || tag: exactly len(plaintext) + GCM_TAG bytes. Deterministic
    by design (see RULE R1) so retries and reconcile reproduce identical
    ciphertext."""
    _key_check(dk, "dk")
    body = AESGCM(bytes(dk)).encrypt(
        chunk_nonce(gen, offset), plaintext, chunk_aad(session, gen, offset)
    )
    # Not a tautology: it pins the "no prefix, no header" wire shape the
    # worker's length + 16 check depends on.
    assert len(body) == len(plaintext) + GCM_TAG
    return body


def decrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  body: bytes) -> bytes:
    """Inverse of encrypt_chunk. Raise CryptoError on tag failure or
    len(body) < GCM_TAG."""
    _key_check(dk, "dk")
    if len(body) < GCM_TAG:
        raise CryptoError(
            f"chunk body is {len(body)} bytes: shorter than a GCM tag, not a ciphertext"
        )
    try:
        return AESGCM(bytes(dk)).decrypt(
            chunk_nonce(gen, offset), bytes(body), chunk_aad(session, gen, offset)
        )
    except InvalidTag as exc:
        raise CryptoError(
            f"chunk at offset {offset} (gen {gen}) failed its GCM tag: the bytes "
            f"are corrupt, resealed, or addressed to a different session"
        ) from exc


def wrap_aad(session: str, recipient_id: str, gen: int) -> bytes:
    """AAD_WRAP || 0x00 || utf8(session) || 0x00 || utf8(recipient_id)
    || 0x00 || BE4(gen).

    ``recipient_id`` is always a server-side devices.id UUID (D4), never a
    keyid. Binding it means a wrap lifted from one recipient's row cannot be
    replayed to another, and binding the session means two sessions' wraps
    cannot be swapped -- both fail the tag, not just a lookup.
    """
    if not 1 <= gen < 2**32:
        raise CryptoError(f"generation {gen} out of range: must be 1..2^32-1")
    return (
        AAD_WRAP + b"\x00" + session.encode("utf-8") + b"\x00"
        + recipient_id.encode("utf-8") + b"\x00" + gen.to_bytes(4, "big")
    )


def wrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
            dk: bytes, *, nonce: bytes | None = None) -> bytes:
    """wrap_nonce || AES-256-GCM(enc_key, wrap_nonce, dk, wrap_aad(...)).
    60 bytes. ``nonce`` is 12 random bytes when None; the parameter exists only
    so tests can pin the vector below.

    Random nonces are safe here where chunk nonces could not be: a K_enc wraps
    at most one 32-byte DK per (session, gen), so the birthday bound on 96
    random bits is nowhere near reachable -- and unlike chunks, a re-wrap is
    never required to be byte-identical to its predecessor.
    """
    _key_check(enc_key, "enc_key")
    _key_check(dk, "dk")
    if nonce is None:
        nonce = secrets.token_bytes(12)
    elif len(nonce) != 12:
        raise CryptoError(f"wrap nonce must be 12 bytes, got {len(nonce)}")
    blob = nonce + AESGCM(bytes(enc_key)).encrypt(
        nonce, bytes(dk), wrap_aad(session, recipient_id, gen)
    )
    assert len(blob) == WRAP_LEN
    return blob


def unwrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
              blob: bytes) -> bytes:
    """Inverse of wrap_dk. Raise CryptoError unless len(blob) == WRAP_LEN and
    the tag verifies."""
    _key_check(enc_key, "enc_key")
    if len(blob) != WRAP_LEN:
        raise CryptoError(
            f"wrapped key blob is {len(blob)} bytes, expected exactly {WRAP_LEN}"
        )
    try:
        return AESGCM(bytes(enc_key)).decrypt(
            bytes(blob[:12]), bytes(blob[12:]), wrap_aad(session, recipient_id, gen)
        )
    except InvalidTag as exc:
        raise CryptoError(
            f"wrapped key for session {session} failed its GCM tag: wrong key, "
            f"wrong recipient, or a tampered wrap"
        ) from exc


__all__ = [
    "AAD_CHUNK",
    "AAD_WRAP",
    "BEARER_PREFIX",
    "CryptoError",
    "ENC_SCHEME",
    "GCM_TAG",
    "HKDF_SALT",
    "INFO_AUTH",
    "INFO_ENC",
    "KEY_PREFIXES",
    "KeySet",
    "WRAP_LEN",
    "bearer_sha256",
    "chunk_aad",
    "chunk_nonce",
    "decrypt_chunk",
    "encrypt_chunk",
    "generate_key",
    "new_data_key",
    "parse_key",
    "unwrap_dk",
    "wrap_aad",
    "wrap_dk",
]
