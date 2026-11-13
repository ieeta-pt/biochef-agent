"""age-x25519 with the node's X25519 key.

pyrage is a binding to the Rust age library, from the author of the rfc8785
package already pinned here, and has wheels for every Python this project
supports. It reads age's own text form of a key, so the key in the PEM file is
written out in that form for the moment it is needed. The bech32 below is that
form, and nothing else uses it.
"""

import pyrage
from cryptography.hazmat.primitives import serialization

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    state = 1
    for value in values:
        top = state >> 25
        state = (state & 0x1ffffff) << 5 ^ value
        for bit, constant in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa,
                                        0x3d4233dd, 0x2a1462b3)):
            if top >> bit & 1:
                state ^= constant
    return state


def _bech32(prefix, data):
    words, accumulator, bits = [], 0, 0
    for byte in data:
        accumulator = accumulator << 8 | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            words.append(accumulator >> bits & 31)
    if bits:
        words.append(accumulator << (5 - bits) & 31)
    expanded = ([ord(c) >> 5 for c in prefix] + [0]
                + [ord(c) & 31 for c in prefix])
    remainder = _polymod(expanded + words + [0] * 6) ^ 1
    check = [remainder >> 5 * (5 - i) & 31 for i in range(6)]
    return prefix + "1" + "".join(_CHARSET[word] for word in words + check)


def _raw(key):
    return key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def recipient(keys):
    """The node's public key in age's form, which is what requesters encrypt to."""
    public = keys.encryption.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _bech32("age", public)


def decrypt(keys, ciphertext):
    """The plaintext, or pyrage.DecryptError."""
    secret = _bech32("age-secret-key-", _raw(keys.encryption)).upper()
    identity = pyrage.x25519.Identity.from_str(secret)
    return pyrage.decrypt(ciphertext, [identity])
