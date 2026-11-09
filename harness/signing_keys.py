"""Claves públicas y plazos de retiro; ningún destino de red procede del JWT."""
import hashlib
import json
import time

import jwt
from cryptography.hazmat.primitives import serialization


def public_jwk(private):
    public = serialization.load_pem_private_key(private, password=None).public_key()
    jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(public))
    kid = hashlib.sha256(public.public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()[:24]
    jwk.update(kid=kid, use='sig', alg='EdDSA')
    return jwk


def parse_public_keys(keys):
    if type(keys) is not list or not 1 <= len(keys) <= 2:
        raise ValueError('Invalid key set')
    result = {}
    for item in keys:
        if (type(item) is not dict or item.get('kty') != 'OKP' or item.get('crv') != 'Ed25519'
                or item.get('alg') != 'EdDSA' or item.get('use') != 'sig'
                or type(item.get('kid')) is not str or not 1 <= len(item['kid']) <= 128
                or item['kid'] in result or set(item) - {'kty','crv','x','alg','use','kid','lab_retire_at'}):
            raise ValueError('Invalid public key')
        deadline = item.get('lab_retire_at')
        if deadline is not None and (type(deadline) is not int or deadline <= 0):
            raise ValueError('Invalid retirement deadline')
        result[item['kid']] = (jwt.PyJWK.from_dict(item, algorithm='EdDSA').key, deadline)
    return result


def select_key(keys, token, now=None):
    now = time.time() if now is None else now
    kid = jwt.get_unverified_header(token).get('kid')
    if type(kid) is not str or kid not in keys:
        raise ValueError('Unknown key')
    key, deadline = keys[kid]
    if deadline is not None and now >= deadline:
        raise ValueError('Retired key')
    return key
