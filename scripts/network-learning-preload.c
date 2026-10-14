#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <pthread.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static int (*real_getsockopt)(int, int, int, void *, socklen_t *);
static int (*real_close)(int);
static int (*real_getaddrinfo)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
static pthread_once_t initialized = PTHREAD_ONCE_INIT;
static pthread_mutex_t pending_lock = PTHREAD_MUTEX_INITIALIZER;
static __thread int in_hook;
struct pending_connection {
    int fd;
    uint64_t cookie;
    struct sockaddr_storage address;
    struct pending_connection *next;
};
static struct pending_connection *pending;
static void before_fork(void) { pthread_mutex_lock(&pending_lock); }
static void after_fork(void) { pthread_mutex_unlock(&pending_lock); }
static void init_real(void) {
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_getsockopt = dlsym(RTLD_NEXT, "getsockopt");
    real_close = dlsym(RTLD_NEXT, "close");
    real_getaddrinfo = dlsym(RTLD_NEXT, "getaddrinfo");
    pthread_atfork(before_fork, after_fork, after_fork);
}
/* JSON-escape untrusted resolver input and environment strings. */
static void escape(char *out, size_t size, const char *value) {
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)(value ?: ""); *p && n + 6 < size; ++p) {
        if (*p < 32 || *p == '"' || *p == '\\') {
            n += (size_t)snprintf(out + n, size - n, "\\u%04x", *p);
        } else out[n++] = *p;
    }
    out[n] = 0;
}
static void emit_event(const struct sockaddr *sa, int result, int error,
                       const char *domain, const char *qtype, const char *protocol) {
    if (in_hook) return;
    const char *path = getenv("BIANBU_NETWORK_LEARNING_SOCKET");
    if (!path || !*path) return;
    in_hook = 1;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) { in_hook = 0; return; }
    struct sockaddr_un un = { .sun_family = AF_UNIX };
    snprintf(un.sun_path, sizeof(un.sun_path), "%s", path);
    if (real_connect(fd, (struct sockaddr *)&un, sizeof(un)) == 0) {
        char ip[INET6_ADDRSTRLEN] = "", name[1537], instance[769];
        unsigned port = 0;
        if (sa && sa->sa_family == AF_INET) {
            const struct sockaddr_in *x = (const struct sockaddr_in *)sa;
            inet_ntop(AF_INET, &x->sin_addr, ip, sizeof(ip)); port = ntohs(x->sin_port);
        } else if (sa && sa->sa_family == AF_INET6) {
            const struct sockaddr_in6 *x = (const struct sockaddr_in6 *)sa;
            inet_ntop(AF_INET6, &x->sin6_addr, ip, sizeof(ip)); port = ntohs(x->sin6_port);
        }
        escape(name, sizeof(name), domain);
        escape(instance, sizeof(instance), getenv("BIANBU_NETWORK_LEARNING_INSTANCE_ID"));
        char buf[4096];
        int n = snprintf(buf, sizeof(buf),
            "{\"op\":\"event\",\"pid\":%d,\"instance_id\":\"%s\","
            "\"event_type\":\"%s\",\"remote_ip\":\"%s\",\"remote_port\":%u,"
            "\"protocol\":\"%s\",\"retval\":%d,\"errno\":%d,"
            "\"domain\":\"%s\",\"dns_qtype\":\"%s\",\"backend\":\"preload\"}\n",
            getpid(), instance, domain ? "dns_query" : "connect", ip, domain ? 0 : port,
            protocol, result, error, name, qtype ?: "");
        if (n > 0 && (size_t)n < sizeof(buf)) send(fd, buf, (size_t)n, MSG_NOSIGNAL);
    }
    real_close(fd);
    in_hook = 0;
}
static const char *socket_protocol(int fd) {
    int type = SOCK_STREAM; socklen_t size = sizeof(type);
    real_getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size);
    return type == SOCK_DGRAM ? "udp" : "tcp";
}
static uint64_t socket_cookie(int fd) {
    uint64_t cookie = 0; socklen_t size = sizeof(cookie);
    if (real_getsockopt(fd, SOL_SOCKET, SO_COOKIE, &cookie, &size) != 0) return 0;
    return cookie;
}
static int take_pending(int fd, struct sockaddr_storage *address) {
    int found = 0;
    pthread_mutex_lock(&pending_lock);
    struct pending_connection **p = &pending;
    while (*p) {
        if ((*p)->fd == fd) {
            struct pending_connection *item = *p;
            if (item->cookie && item->cookie == socket_cookie(fd)) {
                *address = item->address; found = 1;
            }
            *p = item->next; free(item); break;
        }
        p = &(*p)->next;
    }
    pthread_mutex_unlock(&pending_lock);
    return found;
}
int connect(int fd, const struct sockaddr *sa, socklen_t len) {
    pthread_once(&initialized, init_real);
    int result = real_connect(fd, sa, len), saved_errno = errno;
    if (!in_hook && sa && (sa->sa_family == AF_INET || sa->sa_family == AF_INET6)) {
        if (result < 0 && saved_errno == EINPROGRESS) {
            struct pending_connection *item = calloc(1, sizeof(*item));
            if (item && len <= sizeof(item->address)) {
                struct sockaddr_storage old;
                take_pending(fd, &old);
                item->fd = fd; item->cookie = socket_cookie(fd); memcpy(&item->address, sa, len);
                pthread_mutex_lock(&pending_lock);
                item->next = pending; pending = item;
                pthread_mutex_unlock(&pending_lock);
            } else free(item);
        } else if (!(result < 0 && saved_errno == EALREADY)) {
            struct sockaddr_storage old;
            take_pending(fd, &old);
            emit_event(sa, result, result < 0 ? saved_errno : 0, NULL, NULL, socket_protocol(fd));
        }
    }
    errno = saved_errno;
    return result;
}
int getsockopt(int fd, int level, int option, void *value, socklen_t *len) {
    pthread_once(&initialized, init_real);
    int result = real_getsockopt(fd, level, option, value, len), saved_errno = errno;
    if (!in_hook && result == 0 && level == SOL_SOCKET && option == SO_ERROR && value && len && *len >= sizeof(int)) {
        int error = *(int *)value;
        struct sockaddr_storage peer, address; socklen_t size = sizeof(peer);
        /* SO_ERROR=0 alone can also mean a connection is still pending. */
        if ((error && error != EINPROGRESS && error != EALREADY) ||
            (!error && getpeername(fd, (struct sockaddr *)&peer, &size) == 0)) {
            if (take_pending(fd, &address))
                emit_event((struct sockaddr *)&address, error ? -1 : 0, error, NULL, NULL, socket_protocol(fd));
        }
    }
    errno = saved_errno;
    return result;
}
int close(int fd) {
    pthread_once(&initialized, init_real);
    int saved_errno = errno;
    if (!in_hook) {
        struct sockaddr_storage address, peer; socklen_t size = sizeof(peer);
        if (take_pending(fd, &address)) {
            /* Do not consume SO_ERROR: closing an unresolved attempt is cancellation. */
            int connected = getpeername(fd, (struct sockaddr *)&peer, &size) == 0;
            emit_event((struct sockaddr *)&address, connected ? 0 : -1,
                       connected ? 0 : ECANCELED, NULL, NULL, socket_protocol(fd));
        }
    }
    errno = saved_errno;
    return real_close(fd);
}
int getaddrinfo(const char *node, const char *service, const struct addrinfo *hints, struct addrinfo **result) {
    pthread_once(&initialized, init_real);
    int rc = real_getaddrinfo(node, service, hints, result), saved_errno = errno;
    unsigned char numeric[16];
    if (node && !in_hook && inet_pton(AF_INET, node, numeric) != 1 && inet_pton(AF_INET6, node, numeric) != 1) {
        if (rc == 0) {
            for (struct addrinfo *a = *result; a; a = a->ai_next)
                if (a->ai_family == AF_INET || a->ai_family == AF_INET6)
                    emit_event(a->ai_addr, 0, 0, node, a->ai_family == AF_INET ? "A" : "AAAA", "udp");
        } else {
            const char *qtype = hints && hints->ai_family == AF_INET ? "A" :
                                hints && hints->ai_family == AF_INET6 ? "AAAA" : "A/AAAA";
            emit_event(NULL, -1, rc, node, qtype, "udp");
        }
    }
    errno = saved_errno;
    return rc;
}
int __connect_nocancel(int fd, const struct sockaddr *sa, socklen_t len) { return connect(fd, sa, len); }
int connect64(int fd, const struct sockaddr *sa, socklen_t len) { return connect(fd, sa, len); }
int __libc_connect(int fd, const struct sockaddr *sa, socklen_t len) { return connect(fd, sa, len); }
