#ifndef CLIP_UPLOAD_SCAN_H
#define CLIP_UPLOAD_SCAN_H
#include <stdint.h>
#include <stdbool.h>
/* Called only while the upload job owns storage, or by an idle USB query.
 * Positive callback return stops enumeration; negative return propagates. */
typedef int (*upload_visit_fn)(const char *session, uint32_t segment, uint32_t size,
                               bool sealed, void *context);
int upload_scan(upload_visit_fn visit, void *context, int (*check)(void));
/* Skip strictly earlier session subtrees, for efficient cursor pages. */
int upload_scan_after(upload_visit_fn visit, void *context, int (*check)(void),
                      const char *session);
#endif
