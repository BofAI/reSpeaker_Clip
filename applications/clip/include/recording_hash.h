/* Content metadata is independent of receiver identity and upload progress. */
#ifndef CLIP_RECORDING_HASH_H
#define CLIP_RECORDING_HASH_H
#include <stddef.h>
#include <stdint.h>
#include <errno.h>
#ifdef CONFIG_CLIP_RECORDING_HASH
int recording_hash_begin(const char *session, uint32_t segment);
int recording_hash_update(const uint8_t *bytes, size_t length);
int recording_hash_end(const char *session, uint32_t segment, uint32_t size);
void recording_hash_abort(void);
int recording_hash_load(const char *session, uint32_t segment, uint32_t size, uint8_t digest[32]);
int recording_hash_save(const char *session, uint32_t segment, uint32_t size, const uint8_t digest[32]);
#else
static inline int recording_hash_begin(const char *s, uint32_t n) { (void)s; (void)n; return 0; }
static inline int recording_hash_update(const uint8_t *p, size_t n) { (void)p; (void)n; return 0; }
static inline void recording_hash_abort(void) {}
static inline int recording_hash_end(const char *s, uint32_t n, uint32_t z) { (void)s; (void)n; (void)z; return -ENOENT; }
static inline int recording_hash_load(const char *s, uint32_t n, uint32_t z, uint8_t d[32]) { (void)s; (void)n; (void)z; (void)d; return -ENOENT; }
static inline int recording_hash_save(const char *s, uint32_t n, uint32_t z, const uint8_t d[32]) { (void)s; (void)n; (void)z; (void)d; return 0; }
#endif
#endif
