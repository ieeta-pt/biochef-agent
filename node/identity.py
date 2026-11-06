"""Is a member who the study says they are? (section 7.3)

A study lists each member's public keys next to an LS Login ID token, and the
token is what ties a person to those keys. So the checks are on the token: signed
by LS Login, made for the editor, still valid, about this member, and binding
these keys through its nonce.

The nonce is the check that matters most, and it cannot run yet: how the editor
builds it is not defined. Without it a valid token proves who signed in, not that
these keys are theirs, which is exactly the attack the check exists for. So it
stays closed. A token that passes everything else still counts as not verified,
with that reason recorded, rather than passing because one check is missing.

Everything here records a result and raises nothing about the token: a member who
fails is an ordinary state of a study version, and the page shows why.
"""

import os

import jwt

import passports
from store import repo

NONCE_UNDEFINED = ("The token is valid, but how the editor builds its nonce is not "
                   "defined yet, so these keys cannot be tied to this person")
EXPIRED = ("The token has expired. Only unexpired tokens are accepted until the "
           "federation publishes its list of past LS Login keys")
WRONG_PERSON = "The token names someone other than this member"
NO_KEYS = "The LS Login keys could not be fetched, so the token was not checked"
NOT_VALID = "The token did not pass the LS Login checks"


class IdentityError(Exception):
    """Why a member's token cannot be accepted, in words for the page."""


def nonce_closed(claims, key):
    """The check of 7.3.4 until the editor defines the construction."""
    raise IdentityError(NONCE_UNDEFINED)


class Verifier:
    """Issuer, audience and keys. The keys are fetched when first needed."""

    def __init__(self, issuer, client_id, keyset=None, nonce_check=nonce_closed):
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
