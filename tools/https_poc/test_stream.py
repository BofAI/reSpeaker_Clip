"""Exercise the actual C transport against deterministic TCP/TLS boundaries.

These validate cancellation and ownership, not mbedTLS cryptography. Certificate
and actual I/O behavior still require the matching firmware hardware tests.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class StreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        source = (ROOT / "applications/clip/src/https_stream.c").read_text()
        source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <fcntl.h>
#include <poll.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
#define ZVFS_F_GETFL F_GETFL
#define ZVFS_F_SETFL F_SETFL
#define ZVFS_O_NONBLOCK O_NONBLOCK
#define ZSOCK_MSG_DONTWAIT MSG_DONTWAIT
#define ZSOCK_POLLIN POLLIN
#define ZSOCK_POLLOUT POLLOUT
#define ZSOCK_POLLNVAL POLLNVAL
#define zsock_pollfd pollfd
#define CONFIG_NET_SOCKETS_CONNECT_TIMEOUT 10000
#define CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT 10000
#define MBEDTLS_ERR_SSL_INTERNAL_ERROR -1
#define MBEDTLS_ERR_NET_CONN_RESET -2
#define MBEDTLS_ERR_SSL_WANT_WRITE -3
#define MBEDTLS_ERR_SSL_WANT_READ -4
#define MBEDTLS_ERR_NET_SEND_FAILED -5
#define MBEDTLS_ERR_NET_RECV_FAILED -6
#define MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY -7
#define MBEDTLS_SSL_IS_CLIENT 1
#define MBEDTLS_SSL_TRANSPORT_STREAM 2
#define MBEDTLS_SSL_PRESET_DEFAULT 3
#define MBEDTLS_SSL_VERIFY_REQUIRED 4
#define PSA_SUCCESS 0
typedef int mbedtls_ssl_context;
typedef int mbedtls_ssl_config;
typedef int mbedtls_x509_crt;
static int64_t now, cancel_at = -1;
static int live, sockets, polls, tls_calls, cert_error, verify_error, so_error;
static int connect_pending, handshake_pending, write_pending, read_pending;
static int random_fail, ready, partial, authenticated;
static const unsigned char *retry_buffer;
static size_t retry_length;
static void *k_calloc(size_t n, size_t s) { void *p=calloc(n,s); if(p)live++; return p; }
static void k_free(void *p) { if(p)live--; free(p); }
static int64_t k_uptime_get(void) { return now; }
static int check(void) { return cancel_at >= 0 && now >= cancel_at ? -ECANCELED : 0; }
static int psa_generate_random(unsigned char *p,size_t n) { memset(p,1,n);return random_fail; }
static int psa_crypto_init(void) { return 0; }
static void mbedtls_ssl_init(int *p) { *p=0; }
static void mbedtls_ssl_config_init(int *p) { *p=0; }
static void mbedtls_x509_crt_init(int *p) { *p=0; }
static void mbedtls_ssl_free(int *p) { (void)p; }
static void mbedtls_ssl_config_free(int *p) { (void)p; }
static void mbedtls_x509_crt_free(int *p) { (void)p; }
static int mbedtls_x509_crt_parse(int *p,const unsigned char *c,size_t n) {
 (void)p;assert(n==3 && !memcmp(c,"CA",3));return cert_error;
}
static int mbedtls_ssl_config_defaults(int *p,int r,int t,int preset) {
 assert(r==1 && t==2 && preset==3);*p=1;return 0;
}
static void mbedtls_ssl_conf_authmode(int *p,int v) { assert(*p==1 && v==4);authenticated=1; }
static void mbedtls_ssl_conf_ca_chain(int *p,int *ca,void *crl) { (void)p;(void)ca;assert(!crl); }
static void mbedtls_ssl_conf_rng(int *p,int (*rng)(void*,unsigned char*,size_t),void *ctx) {
 unsigned char b;(void)p;assert(rng(ctx,&b,1)==0);
}
static int mbedtls_ssl_setup(int *ssl,int *conf) { assert(*conf==1);*ssl=1;return 0; }
static int mbedtls_ssl_set_hostname(int *p,const char *host) { assert(*p==1 && !strcmp(host,"receiver"));return 0; }
static void mbedtls_ssl_set_bio(int *p,void *ctx,
 int (*tx)(void*,const unsigned char*,size_t),int (*rx)(void*,unsigned char*,size_t),void *timeout) {
 (void)p;(void)ctx;assert(tx && rx && !timeout);
}
static int zsock_socket(int family,int type,int protocol) { assert(family==AF_INET && type==SOCK_STREAM && protocol==IPPROTO_TCP);sockets++;return 7; }
static int zsock_fcntl(int fd,int op,int flags) { assert(fd==7); if(op==F_SETFL)assert(flags&O_NONBLOCK); return 0; }
static int zsock_connect(int fd,const struct sockaddr *a,socklen_t n) {
 assert(fd==7 && a && n); if(connect_pending){errno=EINPROGRESS;return -1;}return 0;
}
static int zsock_poll(struct pollfd *p,int n,int timeout) {
 assert(n==1 && p->fd==7 && timeout>0 && timeout<=50);polls++;now+=timeout;
 if(ready){p->revents=p->events;return 1;}return 0;
}
static int zsock_getsockopt(int fd,int level,int opt,void *out,socklen_t *len) {
 assert(fd==7 && level==SOL_SOCKET && opt==SO_ERROR && *len==sizeof(int));*(int*)out=so_error;return 0;
}
static ssize_t zsock_send(int fd,const void *p,size_t n,int flags) { assert(fd==7 && p && flags==MSG_DONTWAIT);return n; }
static ssize_t zsock_recv(int fd,void *p,size_t n,int flags) { assert(fd==7 && p && n && flags==MSG_DONTWAIT);errno=EAGAIN;return -1; }
static int zsock_close(int fd) { assert(fd==7);sockets--;return 0; }
static int mbedtls_ssl_handshake(int *s) { assert(*s==1 && authenticated);tls_calls++;return handshake_pending ? MBEDTLS_ERR_SSL_WANT_READ : 0; }
static unsigned int mbedtls_ssl_get_verify_result(int *s) { assert(*s==1);return verify_error; }
static int mbedtls_ssl_close_notify(int *s) { assert(*s==1);return MBEDTLS_ERR_SSL_WANT_WRITE; }
static int mbedtls_ssl_write(int *s,const unsigned char *p,size_t n) {
 assert(*s==1 && p && n);tls_calls++;
 if(write_pending) return MBEDTLS_ERR_SSL_WANT_WRITE;
 if(partial==1){partial=2;return 2;}
 if(partial==2){partial=3;retry_buffer=p;retry_length=n;return MBEDTLS_ERR_SSL_WANT_READ;}
 if(partial==3){assert(p==retry_buffer && n==retry_length);partial=4;}
 return n;
}
static int mbedtls_ssl_read(int *s,void *p,size_t n) { assert(*s==1 && p && n);tls_calls++;return read_pending ? MBEDTLS_ERR_SSL_WANT_READ : MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY; }
'''
        fixture = "#include <netinet/in.h>\n" + fixture
        fixture += source
        fixture += r'''
int main(int argc,char **argv) {
 assert(argc==2);
 const char *name=argv[1];struct https_stream *s=NULL;struct sockaddr a={0};
 int expected=0;
 if(!strcmp(name,"connect-cancel")){connect_pending=1;cancel_at=120;expected=-ECANCELED;}
 if(!strcmp(name,"handshake-cancel")){handshake_pending=1;cancel_at=120;expected=-ECANCELED;}
 if(!strcmp(name,"connect-timeout")){connect_pending=1;expected=-ETIMEDOUT;}
 if(!strcmp(name,"handshake-timeout")){handshake_pending=1;expected=-ETIMEDOUT;}
 if(!strcmp(name,"connect-error")){connect_pending=ready=1;so_error=ECONNREFUSED;expected=-ECONNREFUSED;}
 if(!strcmp(name,"bad-ca")){cert_error=1;expected=-EACCES;}
 if(!strcmp(name,"bad-peer")){verify_error=1;expected=-EACCES;}
 int result=https_stream_open(&s,&a,sizeof(a),"receiver",(const unsigned char*)"CA",3,check);
 assert(result==expected);
 if(expected){assert(!s && !live && !sockets);if(expected==-ECANCELED)assert(now<=170);return 0;}
 assert(s && sockets==1 && live==1);
 if(!strcmp(name,"write-cancel")){write_pending=1;cancel_at=now+120;expected=-ECANCELED;}
 if(!strcmp(name,"read-cancel")){read_pending=1;cancel_at=now+120;expected=-ECANCELED;}
 if(!strcmp(name,"read-timeout")){read_pending=1;expected=-ETIMEDOUT;}
 if(!strcmp(name,"write-timeout")){write_pending=1;expected=-ETIMEDOUT;}
 if(!strcmp(name,"partial-write")){partial=ready=1;}
 char data[8]={0};
 if(read_pending)result=https_stream_read(s,data,sizeof(data));
 else result=https_stream_write(s,data,sizeof(data));
 assert(result==expected);
 if(partial)assert(partial==4);
 if(expected==-ECANCELED)assert(now<=170);
 https_stream_close(s);https_stream_close(NULL);
 assert(!live && !sockets);
 return 0;
}
'''
        (root / "test.c").write_text(fixture)
        cls.executable = root / "test"
        try:
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(cls.executable)], check=True)
        except BaseException:
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_cancellation_releases_stream_without_waiting_for_peer(self):
        for case in ("connect-cancel", "handshake-cancel", "read-cancel", "write-cancel"):
            with self.subTest(case=case):
                subprocess.run([str(self.executable), case], check=True)

    def test_timeouts_and_errors_release_all_owned_resources(self):
        for case in ("connect-timeout", "handshake-timeout", "read-timeout", "write-timeout",
                     "connect-error", "bad-ca", "bad-peer"):
            with self.subTest(case=case):
                subprocess.run([str(self.executable), case], check=True)

    def test_partial_write_retries_identical_remaining_tls_buffer(self):
        subprocess.run([str(self.executable), "partial-write"], check=True)


if __name__ == "__main__":
    unittest.main()
