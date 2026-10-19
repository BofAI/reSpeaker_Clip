#ifndef CLIP_WIFI_DIAG_H
#define CLIP_WIFI_DIAG_H

enum wifi_diag_point { WIFI_DIAG_DHCP, WIFI_DIAG_COEX, WIFI_DIAG_CONNECT_BEGIN,
                       WIFI_DIAG_CONNECT_END, WIFI_DIAG_POINTS };
#ifdef CONFIG_CLIP_WIFI_DIAG
int wifi_diag_register(void);
void wifi_diag_reset(void);
void wifi_diag_capture(enum wifi_diag_point point, int result);
#else
static inline int wifi_diag_register(void) { return 0; }
static inline void wifi_diag_reset(void) {}
static inline void wifi_diag_capture(enum wifi_diag_point point, int result)
{ (void)point; (void)result; }
#endif
#endif
