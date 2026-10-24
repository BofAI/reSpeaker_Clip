#ifndef CLIP_DTLS_PROFILE_CRYPTO_H
#define CLIP_DTLS_PROFILE_CRYPTO_H
#include <psa/crypto.h>
psa_status_t clip_profile_aead_encrypt(mbedtls_svc_key_id_t key, psa_algorithm_t alg,
    const uint8_t *nonce, size_t nonce_length, const uint8_t *additional_data,
    size_t additional_data_length, const uint8_t *plaintext, size_t plaintext_length,
    uint8_t *ciphertext, size_t ciphertext_size, size_t *ciphertext_length);
psa_status_t clip_profile_aead_decrypt(mbedtls_svc_key_id_t key, psa_algorithm_t alg,
    const uint8_t *nonce, size_t nonce_length, const uint8_t *additional_data,
    size_t additional_data_length, const uint8_t *ciphertext, size_t ciphertext_length,
    uint8_t *plaintext, size_t plaintext_size, size_t *plaintext_length);
#endif
