"""Шифрование секретов пользователя (ключ WB, токен ЧЗ) ключом Fernet из .env."""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from . import config


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = config.FERNET_KEY
    if not key:
        raise RuntimeError("FERNET_KEY не задан в окружении")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")
