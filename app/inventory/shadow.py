# Copyright (C) 2026, RTE (http://www.rte-france.com)
# SPDX-License-Identifier: Apache-2.0

"""Password hashing for the accounts inside a guest.

The same story as `grub.py`, one file further from the machine: the root
password a guest is given on its first boot goes into the guest's cloud-init
seed, the seed goes into the inventory, and the inventory goes into git. So
what is written is a hash, the password itself is hashed here and kept nowhere,
and the operator who reads the entry a year later reads a `$6$` string.

The format is SHA-512 crypt, which is what `/etc/shadow` has taken on every
distribution a SEAPATH guest is built from and what cloud-init hands to
`chpasswd -e` untouched. It is computed here rather than by shelling out to
`openssl passwd` or `mkpasswd`, for the reason `grub.py` gives: depending on
another package inside this container to hash one string would be a strange
thing to need. The algorithm is Drepper's, and the two published test vectors
are in the tests.

The work factor is the one thing between a file many people can read and the
root password of a substation guest, so it is well above the 5000 rounds the
format defaults to.
"""

from __future__ import annotations

import hashlib
import secrets

# The crypt alphabet, which is neither base64's nor base64url's: the order is
# `.`, `/`, the digits, then the letters.
_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# What a `$6$` salt is allowed to be: sixteen characters of that alphabet.
_SALT_LENGTH = 16

# The rounds a hash that lives in a git repository is worth. `libxcrypt` takes
# anything from 1000 to 999999999 after `rounds=`, and this is the value the
# Red Hat family has defaulted PAM to for years, so it is a number the shadow
# tooling in a guest meets every day. It costs this service about a third of a
# second, once, on a form submit.
_ROUNDS = 656000

# The byte order the digest is read in when it is encoded. The permutation is
# part of the format rather than anything derivable, and the last group is the
# lone byte 63, which yields two characters instead of four.
_ORDER = (
    (0, 21, 42),
    (22, 43, 1),
    (44, 2, 23),
    (3, 24, 45),
    (25, 46, 4),
    (47, 5, 26),
    (6, 27, 48),
    (28, 49, 7),
    (50, 8, 29),
    (9, 30, 51),
    (31, 52, 10),
    (53, 11, 32),
    (12, 33, 54),
    (34, 55, 13),
    (56, 14, 35),
    (15, 36, 57),
    (37, 58, 16),
    (59, 17, 38),
    (18, 39, 60),
    (40, 61, 19),
    (62, 20, 41),
)


def hash_password(password: str, salt: str | None = None, rounds: int = _ROUNDS) -> str:
    """`$6$rounds=<n>$<salt>$<hash>`, the string `/etc/shadow` holds.

    `salt` is generated when it is left out, which is every call outside the
    tests: a salt chosen here is what keeps two guests given the same password
    from carrying the same line in the inventory.
    """
    if not password:
        raise ValueError("The password must not be empty.")
    if salt is None:
        salt = "".join(secrets.choice(_ALPHABET) for _ in range(_SALT_LENGTH))
    digest = _crypt(password.encode("utf-8"), salt.encode("utf-8"), rounds)
    return f"$6$rounds={rounds}${salt}${digest}"


def _crypt(password: bytes, salt: bytes, rounds: int) -> str:
    """The SHA-512 crypt digest of a password, encoded, without its prefix."""
    # B, the digest of the three parts, whose length decides how much of it is
    # folded back into A and how much of the password is folded in instead.
    intermediate = hashlib.sha512(password + salt + password).digest()
    alternate = hashlib.sha512()
    alternate.update(password + salt)
    alternate.update(intermediate * (len(password) // 64))
    alternate.update(intermediate[: len(password) % 64])
    remaining = len(password)
    while remaining:
        alternate.update(intermediate if remaining & 1 else password)
        remaining >>= 1
    digest = alternate.digest()

    # The two sequences the rounds alternate between, each as long as the
    # value it was derived from.
    password_digest = hashlib.sha512(password * len(password)).digest()
    password_sequence = (password_digest * (len(password) // 64 + 1))[: len(password)]
    salt_digest = hashlib.sha512(salt * (16 + digest[0])).digest()
    salt_sequence = (salt_digest * (len(salt) // 64 + 1))[: len(salt)]

    for index in range(rounds):
        step = hashlib.sha512()
        step.update(password_sequence if index & 1 else digest)
        if index % 3:
            step.update(salt_sequence)
        if index % 7:
            step.update(password_sequence)
        step.update(digest if index & 1 else password_sequence)
        digest = step.digest()

    return _encode(digest)


def _encode(digest: bytes) -> str:
    """The digest in the crypt alphabet, six bits at a time, low bits first."""
    out: list[str] = []
    for first, second, third in _ORDER:
        group = (digest[first] << 16) | (digest[second] << 8) | digest[third]
        for _ in range(4):
            out.append(_ALPHABET[group & 0x3F])
            group >>= 6
    group = digest[63]
    for _ in range(2):
        out.append(_ALPHABET[group & 0x3F])
        group >>= 6
    return "".join(out)
