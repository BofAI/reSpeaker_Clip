/* Walk the actual time-bucket tree, without the LIST 200-session limit.
 * Bounded-depth traversal; no permanent directory handles or queue in RAM. */
#include "upload_scan.h"
#include "storage.h"
#include <zephyr/kernel.h>
#include <zephyr/data/json.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <ctype.h>
#include <errno.h>
struct scan_frame { struct fs_dir_t dir; struct fs_dirent entry; };
struct scan {
    struct scan_frame frame[6];
    char path[STORAGE_PATH_MAX], session[15];
    upload_visit_fn visit;
    void *context;
    int (*check)(void);
    const char *after;
};
static bool decimal(const char *s, size_t min, size_t max)
{
    size_t n = strlen(s);
    if (n < min || n > max) { return false; }
    for (; *s; s++) { if (!isdigit((unsigned char)*s)) { return false; } }
    return true;
}
static int sealed_session(struct scan *scan, bool *sealed)
{
    char path[STORAGE_PATH_MAX], body[512];
    int ret = storage_build_session_metadata_path(scan->session, path, sizeof(path));
    if (ret) { return ret; }
    struct fs_file_t file;
    fs_file_t_init(&file);
    ret = fs_open(&file, path, FS_O_READ);
    if (ret == -ENOENT) { *sealed = false; return 0; }
    if (ret) { return ret; }
    ssize_t n = fs_read(&file, body, sizeof(body) - 1);
    int closed = fs_close(&file);
    if (n < 0) { return n; }
    if (closed) { return closed; }
    struct { bool recording; } metadata;
    const struct json_obj_descr fields[] = {JSON_OBJ_DESCR_PRIM_NAMED(
        __typeof__(metadata), "recording", recording, JSON_TOK_TRUE)};
    body[n] = 0;
    int parsed = json_obj_parse(body, n, fields, 1, &metadata);
    *sealed = parsed == 1 && !metadata.recording;
    return 0;
}
static int walk(struct scan *s, unsigned depth, bool sealed)
{
    struct scan_frame *f = &s->frame[depth];
    fs_dir_t_init(&f->dir);
    int ret = fs_opendir(&f->dir, s->path);
    if (ret) { return ret; }
    bool opened = true;
    unsigned int visited = 0;
    size_t prefix = strlen(s->path);
    while (!(ret = s->check()) && !(ret = fs_readdir(&f->dir, &f->entry)) && f->entry.name[0]) {
        visited++;
        const char *name = f->entry.name;
        if (depth < 5) {
            size_t digits = depth == 0 ? 8 : 2;
            if (f->entry.type != FS_DIR_ENTRY_DIR ||
                !(depth == 4 ? decimal(name, 1, 9) : decimal(name, digits, digits))) { continue; }
            if (depth < 4) {
                size_t position = depth == 0 ? 0 : 8 + (depth - 1) * 2;
                memcpy(s->session + position, name, digits);
                if (s->after && strncmp(s->session, s->after, position + digits) < 0) {
                    continue;
                }
            }
            int n = snprintf(s->path + prefix, sizeof(s->path) - prefix, "/%s", name);
            if (n < 0 || (size_t)n >= sizeof(s->path) - prefix) { ret = -ENAMETOOLONG; break; }
            bool child_sealed = sealed;
            if (depth == 3) { ret = sealed_session(s, &child_sealed); }
            /* The target has only four native FAT directory slots. Keep the
             * first three ancestors, close deeper parents while descending,
             * then reopen and skip consumed entries. The owning upload claim
             * excludes file mutation; cancellation is checked while resuming. */
            if (!ret && depth >= 3) {
                ret = fs_closedir(&f->dir); opened = false;
            }
            if (!ret) { ret = walk(s, depth + 1, child_sealed); }
            s->path[prefix] = 0;
            if (!ret && !opened) {
                fs_dir_t_init(&f->dir);
                ret = fs_opendir(&f->dir, s->path); opened = ret == 0;
                for (unsigned int i = 0; !ret && i < visited; i++) {
                    ret = s->check();
                    if (!ret) { ret = fs_readdir(&f->dir, &f->entry); }
                    if (!ret && !f->entry.name[0]) { ret = -EIO; }
                }
            }
            if (ret) { break; }
        } else {
            size_t length = strlen(name);
            if (f->entry.type != FS_DIR_ENTRY_FILE || length < 9 || length > 14 ||
                strcmp(name + length - 5, ".opus")) { continue; }
            char number[10] = {0}; memcpy(number, name, length - 5);
            if (!decimal(number, 4, 9)) { continue; }
            uint32_t segment = strtoul(number, NULL, 10);
            if (!segment) { continue; }
            char canonical[STORAGE_PATH_MAX], actual[STORAGE_PATH_MAX];
            ret = storage_build_chunk_path(s->session, segment, canonical, sizeof(canonical));
            int n = snprintf(actual, sizeof(actual), "%s/%s", s->path, name);
            if (ret || n < 0 || (size_t)n >= sizeof(actual)) { ret = ret ? ret : -ENAMETOOLONG; break; }
            if (strcmp(canonical, actual)) { continue; }
            ret = s->visit(s->session, segment, f->entry.size, sealed, s->context);
            if (ret) { break; }
        }
    }
    int closed = opened ? fs_closedir(&f->dir) : 0;
    return ret ? ret : closed;
}
int upload_scan_after(upload_visit_fn visit, void *context, int (*check)(void),
                      const char *after)
{
    int ret = storage_ensure_mounted();
    if (ret) { return ret; }
    struct scan *scan = k_calloc(1, sizeof(*scan));
    if (!scan) { return -ENOMEM; }
    strcpy(scan->path, "/SD:/REC");
    scan->visit = visit; scan->context = context; scan->check = check;
    scan->after = after;
    ret = walk(scan, 0, false);
    k_free(scan);
    return ret;
}
int upload_scan(upload_visit_fn visit, void *context, int (*check)(void))
{
    return upload_scan_after(visit, context, check, NULL);
}
