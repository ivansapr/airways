"""Authenticated encryption for customer API credentials."""

import json
from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, key):
        self.fernet = Fernet(key)

    def seal(self, client_id, client_secret):
        return self.fernet.encrypt(json.dumps([client_id, client_secret]).encode()).decode()

    def open(self, ciphertext):
        try:
            values = json.loads(self.fernet.decrypt(ciphertext.encode()))
            if not isinstance(values, list) or len(values) != 2 or not all(isinstance(v, str) and v for v in values):
                raise ValueError
            return tuple(values)
        except (InvalidToken, ValueError, TypeError):
            raise ValueError("Stored credentials could not be decrypted") from None
