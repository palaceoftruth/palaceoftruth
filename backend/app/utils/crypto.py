from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


def encrypt_secret(value: str, *, key: str) -> str:
    try:
        fernet = Fernet(key.encode())
    except Exception as exc:  # pragma: no cover - invalid deployment config
        raise ValueError("Invalid sync source credential key configuration") from exc
    return fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str, *, key: str) -> str:
    try:
        fernet = Fernet(key.encode())
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored sync source credential could not be decrypted") from exc
    except Exception as exc:  # pragma: no cover - invalid deployment config
        raise ValueError("Invalid sync source credential key configuration") from exc
