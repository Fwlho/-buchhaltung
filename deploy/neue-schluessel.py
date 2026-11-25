"""Gibt zufällige Schlüssel für die Server-.env aus (braucht nur Python 3, keine Zusatzpakete).

Aufruf auf dem Server im Programmordner:   python3 deploy/neue-schluessel.py >> .env
"""
import base64
import os
import secrets

print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
# Fernet-Schlüssel = 32 Zufallsbytes, URL-sicher Base64-kodiert
print(f"ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}")
print(f"POSTGRES_PASSWORD={secrets.token_hex(24)}")
print(f"SETUP_TOKEN={secrets.token_urlsafe(24)}")
