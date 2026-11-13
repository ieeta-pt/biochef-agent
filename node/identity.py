"""Is a member who the study says they are? (section 7.3)

A study lists each member's public keys next to an LS Login ID token, and the
token is what ties a person to those keys. So the checks are on the token: signed
by LS Login, made for the editor, still valid, about this member, and binding
these keys through its nonce.

The nonce is the check that matters most. Without it a valid token proves who
signed in, not that these keys are theirs. The editor builds an object from the
keys exactly as it sends them to PUT /me/keys, with "v": "biochef.member-keys.v1",
serializes it with RFC 8785, and the nonce is the SHA-256 of those bytes in
base64url without padding. The agent builds the same object from the keys the
study version lists and compares the token's nonce with it as an exact string.
Keys that are not in that form are refused rather than normalised, since a
normalised key would hash to something the editor never signed in for.

This construction was confirmed in ieeta-pt/biochef-infra#2
(https://github.com/ieeta-pt/biochef-infra/pull/2#issuecomment-6826028486), and
docs/identity.md in ieeta-pt/biochef-infra must match it. The test vector is in
tests/test_node_identity.py and in section 7.3.4 of docs/agent-ui/SPEC.md
(ieeta-pt/biochef-agent#106).

Everything here records a result and raises nothing about the token: a member who
fails is an ordinary state of a study version, and the page shows why.
"""

import base64
import hashlib
import os
import re

import jwt
import rfc8785

import passports
from store import repo

MEMBER_KEYS_VERSION = "biochef.member-keys.v1"
MALFORMED_KEYS = ("The member's keys are not in the form their nonce is made from, "
                  "so they cannot be tied to this person")
WRONG_NONCE = ("The token's nonce is not the hash of these keys, so they cannot be "
               "tied to this person")
EXPIRED = ("The token has expired. Only unexpired tokens are accepted until the "
           "federation publishes its list of past LS Login keys")
WRONG_PERSON = "The token names someone other than this member"
NO_KEYS = "The LS Login keys could not be fetched, so the token was not checked"
NOT_VALID = "The token did not pass the LS Login checks"


class IdentityError(Exception):
    """Why a member's token cannot be accepted, in words for the page."""


_JWK_MEMBERS = {"crv", "kty", "x", "y"}
_COORDINATE = re.compile(r"[A-Za-z0-9_-]{43}")
_AGE_RECIPIENT = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")


def _coordinate(value):
    """A P-256 coordinate as RFC 7518 writes it: 32 bytes, base64url, no padding.

    The leading zeros are kept, so the string is always 43 characters, and only
    the one string that encodes those 32 bytes is accepted.
    """
    if not isinstance(value, str) or not _COORDINATE.fullmatch(value):
        raise IdentityError(MALFORMED_KEYS)
    raw = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise IdentityError(MALFORMED_KEYS)
    return value


def member_keys_bytes(signing_key, result_key):
    """The RFC 8785 bytes a member's nonce is the hash of (7.3.4).

    `signing_key` is the public JWK with exactly crv, kty, x and y, and
    `result_key` the age recipient in lowercase. Anything else is refused.
    """
    if not isinstance(signing_key, dict) or set(signing_key) != _JWK_MEMBERS:
        raise IdentityError(MALFORMED_KEYS)
    if signing_key["kty"] != "EC" or signing_key["crv"] != "P-256":
        raise IdentityError(MALFORMED_KEYS)
    if not isinstance(result_key, str) or not _AGE_RECIPIENT.fullmatch(result_key):
        raise IdentityError(MALFORMED_KEYS)
    return rfc8785.dumps({
        "v": MEMBER_KEYS_VERSION,
        "result_key": result_key,
        "signing_key": {"crv": "P-256", "kty": "EC",
                        "x": _coordinate(signing_key["x"]),
                        "y": _coordinate(signing_key["y"])},
    })


def member_keys_nonce(signing_key, result_key):
    """The nonce the editor asks LS Login for: 43 characters of base64url."""
    digest = hashlib.sha256(member_keys_bytes(signing_key, result_key)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def check_nonce(claims, key):
    """The check of 7.3.4: the token's nonce is exactly the hash of these keys."""
    expected = member_keys_nonce(key.signing_key, key.result_key)
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or nonce != expected:
        raise IdentityError(WRONG_NONCE)


class Verifier:
    """Issuer, audience and keys. The keys are fetched when first needed."""

    def __init__(self, issuer, client_id, keyset=None, nonce_check=check_nonce):
        if not issuer or not client_id:
            raise IdentityError("BIOCHEF_LS_LOGIN_ISSUER and "
                                "BIOCHEF_EDITOR_CLIENT_ID are both required")
        self.issuer = issuer
        self.client_id = client_id
        self.nonce_check = nonce_check
        self._keyset = keyset

    @classmethod
    def from_settings(cls):
        return cls((os.getenv("BIOCHEF_LS_LOGIN_ISSUER") or "").strip(),
                   (os.getenv("BIOCHEF_EDITOR_CLIENT_ID") or "").strip())

    @property
    def keyset(self):
        if self._keyset is None:
            try:
                url = passports.jwks_url_for(self.issuer)
            except (passports.PassportError, OSError, ValueError):
                raise IdentityError(NO_KEYS) from None
            self._keyset = passports.KeySet(url)
        return self._keyset


def _claims(token, verifier):
    try:
        return passports.verify(token, verifier.keyset, issuer=verifier.issuer,
                                audience=verifier.client_id)
    except passports.PassportError as failure:
        if isinstance(failure.__cause__, jwt.ExpiredSignatureError):
            raise IdentityError(EXPIRED) from None
        if isinstance(failure.__cause__, (OSError, ValueError)) and not isinstance(
                failure.__cause__, jwt.PyJWTError):
            raise IdentityError(NO_KEYS) from None
        raise IdentityError(NOT_VALID) from None


def check_token(key, verifier):
    """The token's claims, if every check passes. Otherwise IdentityError."""
    claims = _claims(key.binding_token, verifier)
    if claims.get("sub") != key.ls_aai_id:
        raise IdentityError(WRONG_PERSON)
    verifier.nonce_check(claims, key)
    return claims


def check_member(session, key, verifier):
    """Check one member and record the result. Returns whether they verified."""
    try:
        claims = check_token(key, verifier)
    except IdentityError as refused:
        repo.mark_identity_failed(session, key.study_version_id, key.member,
                                  str(refused))
        return False
    repo.mark_identity_verified(session, key.study_version_id, key.member, claims)
    return True


def check_study_version(session, version_id, verifier):
    """Check every member of a version. Returns how many verified."""
    return sum(check_member(session, key, verifier)
               for key in repo.member_keys(session, version_id))
