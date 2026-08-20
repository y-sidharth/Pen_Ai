#!/usr/bin/env python3
"""Create a short-lived, one-time approval token for a safe agent command."""
import os
import sys
import json
import hmac
import hashlib
import base64
import secrets
import time
import re
from getpass import getpass

from security import setup_credential, verify_verifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
AUTH_DIR = os.path.join(BASE_DIR, 'auth')
SECRET_PATH = os.path.join(AUTH_DIR, 'secret.key')
TOKEN_PATH = os.path.join(AUTH_DIR, 'allowlist.token')

if len(sys.argv) != 2 or not re.fullmatch(r'[A-Z2-9]{16}', sys.argv[1]):
    print('Usage: approve_action.py <16-character approval nonce>')
    sys.exit(2)
nonce = sys.argv[1]

os.makedirs(AUTH_DIR, exist_ok=True)

if not os.path.exists(CONFIG_PATH):
    print('config.json not found. Start the agent once to initialise it.')
    sys.exit(3)
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    cfg = json.load(f)

# Do not retain credentials from the previous weaker format.
if 'approval_pin_hash' in cfg:
    cfg.pop('approval_pin_hash', None)
    print('Removed legacy approval PIN verifier. A new approval PIN is required.')
if 'password' in cfg:
    cfg.pop('password', None)

# load or create secret
if os.path.exists(SECRET_PATH):
    with open(SECRET_PATH, 'rb') as sf:
        secret = base64.b64decode(sf.read(), validate=True)
    if len(secret) < 32:
        print('Approval secret is invalid. Remove auth/secret.key and set up approval again.')
        sys.exit(4)
else:
    secret = secrets.token_bytes(32)
    secret_tmp = SECRET_PATH + '.tmp'
    with open(secret_tmp, 'wb') as sf:
        sf.write(base64.b64encode(secret))
    os.replace(secret_tmp, SECRET_PATH)

if not cfg.get('approval_pin_verifier'):
    cfg['approval_pin_verifier'] = setup_credential('approval PIN')
    temp_config = CONFIG_PATH + '.tmp'
    with open(temp_config, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.replace(temp_config, CONFIG_PATH)
    print('Approval PIN setup complete.')

# prompt for PIN
pin = getpass('Enter approval PIN to approve nonce %s: ' % (nonce,))
if not pin:
    print('Empty PIN; aborting.')
    sys.exit(3)

# verify
stored = cfg.get('approval_pin_verifier')
if not stored:
    print('No stored approval PIN; aborting.')
    sys.exit(4)
if not verify_verifier(pin, stored):
    print('Incorrect PIN. Approval denied.')
    sys.exit(6)

# create signed token
ts = int(time.time())
payload = f"{nonce}|{ts}".encode('utf-8')
sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
token_tmp = TOKEN_PATH + '.tmp'
with open(token_tmp, 'w', encoding='utf-8') as tf:
    tf.write(f"{nonce}|{ts}|{sig}")
os.replace(token_tmp, TOKEN_PATH)
print('Approval successful. One-time token written to', TOKEN_PATH)
sys.exit(0)
