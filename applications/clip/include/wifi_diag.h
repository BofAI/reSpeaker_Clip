#ifndef CLIP_WIFI_DIAG_H
#define CLIP_WIFI_DIAG_H

#include <stdbool.h>

enum wifi_diag_point { WIFI_DIAG_DHCP, WIFI_DIAG_COEX, WIFI_DIAG_CONNECT_BEGIN,
                       WIFI_DIAG_CONNECT_END, WIFI_DIAG_POINTS };
/* NETDIAG=4 is a separate, passive last-join summary, not a stats snapshot. */
enum wifi_join_event { WIFI_JOIN_READY, WIFI_JOIN_REQUEST, WIFI_JOIN_REQUEST_RETURN,
    WIFI_JOIN_RESULT, WIFI_JOIN_DHCP, WIFI_JOIN_DISCONNECT, WIFI_JOIN_DISCONNECT_RETURN,
    WIFI_JOIN_DISCONNECTED, WIFI_JOIN_DOWN, WIFI_JOIN_DISCONNECTED_NO_INFO,
    WIFI_JOIN_EVENTS };
#ifdef CONFIG_CLIP_WIFI_DIAG
int wifi_diag_register(void);
void wifi_diag_reset(void);
void wifi_diag_capture(enum wifi_diag_point point, int result);
void wifi_diag_join_begin(bool initial_ready);
void wifi_diag_join_event(enum wifi_join_event event, int status);
#else
static inline int wifi_diag_register(void) { return 0; }
static inline void wifi_diag_reset(void) {}
static inline void wifi_diag_capture(enum wifi_diag_point point, int result)
{ (void)point; (void)result; }
static inline void wifi_diag_join_begin(bool ready) { (void)ready; }
static inline void wifi_diag_join_event(enum wifi_join_event event, int status)
{ (void)event; (void)status; }
#endif
#endif
