"""Firestore client bootstrap.

Ported from utils/firebase.js. Loads serviceAccountKey.json from the project
root and exposes a single shared `db` Firestore client, same as the Node
version's `db` export.
"""
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEY_PATH = os.path.join(_ROOT, 'serviceAccountKey.json')

if not os.path.isfile(_KEY_PATH):
    print(
        f'Missing serviceAccountKey.json at project root ({_KEY_PATH}).\n'
        'Get it from Firebase Console > Project Settings > Service Accounts > Generate new private key.',
        file=sys.stderr,
    )
    sys.exit(1)

_cred = credentials.Certificate(_KEY_PATH)
firebase_admin.initialize_app(_cred)

db = firestore.client()
