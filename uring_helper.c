#include <liburing.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>

static struct io_uring ring;

// Returns 0 on success, -errno on error.
int uring_init(unsigned entries) {
    return io_uring_queue_init(entries, &ring, 0);
}

void uring_exit() {
    io_uring_queue_exit(&ring);
}

// The uring_prep_* functions only queue a request in the submission ring.
// Nothing reaches the kernel until uring_wait(), which submits everything
// queued since the last call in the same system call that waits.
static struct io_uring_sqe *get_sqe(void) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    if (!sqe) {
        // Ring full: submit what's queued to make room.
        io_uring_submit(&ring);
        sqe = io_uring_get_sqe(&ring);
    }
    return sqe;
}

// Each returns 0 once queued, or -EBUSY if the ring is still full.
int uring_prep_recv(int fd, void *buf, unsigned size, unsigned long long user_data) {
    struct io_uring_sqe *sqe = get_sqe();
    if (!sqe) return -EBUSY;
    io_uring_prep_recv(sqe, fd, buf, size, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return 0;
}

int uring_prep_send(int fd, const void *buf, unsigned size, unsigned long long user_data) {
    struct io_uring_sqe *sqe = get_sqe();
    if (!sqe) return -EBUSY;
    io_uring_prep_send(sqe, fd, buf, size, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return 0;
}

int uring_prep_accept(int fd, struct sockaddr *addr, socklen_t *addrlen, unsigned long long user_data) {
    struct io_uring_sqe *sqe = get_sqe();
    if (!sqe) return -EBUSY;
    io_uring_prep_accept(sqe, fd, addr, addrlen, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return 0;
}

int uring_prep_connect(int fd, struct sockaddr *addr, socklen_t addrlen, unsigned long long user_data) {
    struct io_uring_sqe *sqe = get_sqe();
    if (!sqe) return -EBUSY;
    io_uring_prep_connect(sqe, fd, addr, addrlen);
    io_uring_sqe_set_data64(sqe, user_data);
    return 0;
}

// Completes with res = -ETIME once *ts has passed. The kernel reads *ts when
// the request is submitted, so it must stay valid until the next uring_wait().
int uring_prep_timeout(struct __kernel_timespec *ts, unsigned long long user_data) {
    struct io_uring_sqe *sqe = get_sqe();
    if (!sqe) return -EBUSY;
    io_uring_prep_timeout(sqe, ts, 0, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return 0;
}

static inline void cpu_relax(void) {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__)
    __asm__ __volatile__("yield");
#endif
}

static long long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000LL + t.tv_nsec;
}

// Submits every queued request, waits for at least one completion, and copies
// up to `max` ready completions into res/user_data. Returns how many it
// copied (possibly 0, e.g. after a signal), or -errno.
//
// The completion ring is memory shared with the kernel, so checking it is a
// plain read. If completions are already there, this makes no system call at
// all (unless requests are queued). Otherwise it polls the ring for up to
// spin_ns before blocking in the kernel; with spin_ns = 0 it blocks at once,
// submitting and waiting in a single system call.
int uring_wait(long long spin_ns, int *res, unsigned long long *user_data, int max) {
    int ret;
    if (io_uring_cq_ready(&ring)) {
        ret = io_uring_submit(&ring);
    } else if (spin_ns > 0) {
        ret = io_uring_submit(&ring);
        long long deadline = now_ns() + spin_ns;
        while (!io_uring_cq_ready(&ring) && now_ns() < deadline)
            cpu_relax();
        if (!io_uring_cq_ready(&ring))
            ret = io_uring_submit_and_wait(&ring, 1);
    } else {
        ret = io_uring_submit_and_wait(&ring, 1);
    }
    // EINTR: a signal arrived. EBUSY/EAGAIN: the kernel wants completions
    // reaped before it takes more requests. Either way, hand back what's ready.
    if (ret < 0 && ret != -EINTR && ret != -EBUSY && ret != -EAGAIN)
        return ret;

    struct io_uring_cqe *cqe;
    unsigned head, n = 0;
    io_uring_for_each_cqe(&ring, head, cqe) {
        if (n == (unsigned)max) break;
        res[n] = cqe->res;
        user_data[n] = cqe->user_data;
        n++;
    }
    io_uring_cq_advance(&ring, n);
    return n;
}
