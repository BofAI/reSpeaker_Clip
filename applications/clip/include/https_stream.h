/* Single-owner TLS stream. All waits periodically check the job callback. */
#ifndef CLIP_HTTPS_STREAM_H
#define CLIP_HTTPS_STREAM_H

#include <stddef.h>
#include <sys/types.h>
#include <zephyr/net/socket.h>

struct https_stream;
int https_stream_open(struct https_stream **out, const struct sockaddr *address,
                      socklen_t address_length, const char *hostname,
                      const unsigned char *ca, size_t ca_length, int (*check)(void));
int https_stream_write(struct https_stream *stream, const void *data, size_t length);
ssize_t https_stream_read(struct https_stream *stream, void *data, size_t length);
void https_stream_close(struct https_stream *stream);

#endif
