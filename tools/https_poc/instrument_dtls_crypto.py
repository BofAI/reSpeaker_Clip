"""Generate a pinned, build-local TLS record-layer timing copy; SDK is unchanged."""
import argparse
import hashlib
import json
from pathlib import Path

RELATIVE = '../modules/crypto/mbedtls/library/ssl_msg.c'
EXPECTED = '7365ec47a2e1e133693bad61ca6caa27c73d953279716f0a6bfb073e3a32f9fa'


def transform(text):
    substitutions = {
        '#include "common.h"': '#include "common.h"\n#include "dtls_profile_crypto.h"',
        'psa_aead_encrypt(': 'clip_profile_aead_encrypt(',
        'psa_aead_decrypt(': 'clip_profile_aead_decrypt(',
    }
    for old, new in substitutions.items():
        if text.count(old) != 1:
            raise ValueError('Pinned TLS timing insertion point changed: ' + old)
        text = text.replace(old, new)
    return text


def generate(zephyr, output):
    original = (Path(zephyr) / RELATIVE).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != EXPECTED:
        raise ValueError('Pinned TLS source changed; review timing hooks first')
    instrumented = transform(original.decode()).encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(instrumented)
    output.with_suffix('.timing.json').write_text(json.dumps({
        'original': RELATIVE, 'original_sha256': digest,
        'instrumented_sha256': hashlib.sha256(instrumented).hexdigest(),
        'encrypt_call_sites': 1, 'decrypt_call_sites': 1,
        'scope': 'AEAD wrapper calls only; same arguments, returns and cipher',
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zephyr', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate(args.zephyr, args.output)
