"""Generate observation-only TCP source for the measured, pinned Zephyr SDK."""
import argparse
import hashlib
from pathlib import Path

from instrument_network import replace_once

EXPECTED = 'bd2bf0bce4f75be843da94565583082c6a2069cd5c799e7bbd104e3b0f89ced9'

def transform(source):
    adapter = '''
#include "tcp_trace.h"
static void clip_tcp_event(struct tcp *conn, unsigned int kind,
                           uint32_t packet_seq, uint32_t packet_ack, int value)
{
    struct clip_tcp_event event = {
        .kind = kind, .connection = (uint32_t)(uintptr_t)conn,
        .seq = conn->seq, .packet_seq = packet_seq, .packet_ack = packet_ack,
        .total = conn->send_data_total, .unacked = conn->unacked_len,
        .cwnd = conn->ca.cwnd, .rto = TCP_RTO_MS,
        .retry = conn->send_data_retries, .mode = conn->data_mode,
        .state = conn->state, .duplicates = conn->dup_ack_cnt, .value = value,
    };
    clip_tcp_trace(&event);
}

'''
    # All calls run with conn->lock held. No packet cursor or state is modified.
    anchor = 'static void tcp_setup_retransmission(struct tcp *conn)\n'
    source = replace_once(source, anchor, adapter + anchor)
    anchor = '\tconn->send_data_retries = 0;\n\tk_work_reschedule_for_queue(&tcp_work_q, &conn->send_data_timer, K_MSEC(TCP_RTO_MS));'
    source = replace_once(source, anchor,
        '\tclip_tcp_event(conn, CLIP_TCP_ARM, 0, 0, TCP_RTO_MS);\n' + anchor)
    anchor = '\tret = tcp_out_ext(conn, PSH | ACK, pkt, conn->seq + conn->unacked_len);'
    source = replace_once(source, anchor, anchor +
        '\n\tclip_tcp_event(conn, CLIP_TCP_TX, conn->seq + conn->unacked_len, len, ret);')
    anchor = '\tNET_DBG("[%p] send_data_retries=%hu", conn, conn->send_data_retries);'
    source = replace_once(source, anchor,
        '\tclip_tcp_event(conn, CLIP_TCP_RTO, 0, 0, 0);\n' + anchor)
    anchor = '\t\tbool fin = FL(&fl, &, FIN, th_seq(th) == conn->ack);'
    source = replace_once(source, anchor, anchor +
        '\n\t\tclip_tcp_event(conn, CLIP_TCP_ACK, th_seq(th), th_ack(th), len);')
    anchor = '\t\t\t\tint temp_unacked_len = conn->unacked_len;'
    source = replace_once(source, anchor, anchor +
        '\n\t\t\t\tclip_tcp_event(conn, CLIP_TCP_FAST, th_seq(th), th_ack(th), 0);')
    anchor = '\t\t\tif (conn->data_mode == TCP_DATA_MODE_RESEND) {\n\t\t\t\tconn->unacked_len = 0;'
    source = replace_once(source, anchor,
        '\t\t\tif (conn->data_mode == TCP_DATA_MODE_RESEND) {\n'
        '\t\t\t\tclip_tcp_event(conn, CLIP_TCP_RECOVERY, th_seq(th), th_ack(th), len_acked);\n'
        '\t\t\t\tconn->unacked_len = 0;')
    return source


def generate(zephyr, output):
    raw = (zephyr / 'subsys/net/ip/tcp.c').read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError('Pinned SDK TCP source changed; review instrumentation')
    text = transform(raw.decode())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zephyr', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate(args.zephyr, args.output)
