#include <liburing.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <stdio.h>

static struct io_uring ring;

void uring_init(unsigned entries) {
    io_uring_queue_init(entries, &ring, 0);
}

void uring_exit() {
    io_uring_queue_exit(&ring);
}

// Returns 0 on success, -1 on error.
int uring_submit_recv(int fd, char* buf, unsigned size, unsigned long user_data) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    if (!sqe) return -1;
    io_uring_prep_recv(sqe, fd, buf, size, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return io_uring_submit(&ring);
}

int uring_submit_send(int fd, char* buf, unsigned size, unsigned long user_data) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    if (!sqe) return -1;
    io_uring_prep_send(sqe, fd, buf, size, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return io_uring_submit(&ring);
}

// Blocks for completion, writes result into res and user_data ptrs.
int uring_wait_cqe(int* res, unsigned long* user_data) {
    struct io_uring_cqe *cqe;
    int ret = io_uring_wait_cqe(&ring, &cqe);
    if (ret < 0) return ret;
    *res = cqe->res;
    *user_data = io_uring_cqe_get_data64(cqe);
    io_uring_cqe_seen(&ring, cqe);
    return 0;
}

int uring_submit_accept(int fd, struct sockaddr *addr, socklen_t *addrlen, unsigned long user_data) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    if (!sqe) return -1;
    io_uring_prep_accept(sqe, fd, addr, addrlen, 0);
    io_uring_sqe_set_data64(sqe, user_data);
    return io_uring_submit(&ring);
}

int uring_submit_connect(int fd, struct sockaddr *addr, socklen_t addrlen, unsigned long user_data) {
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    if (!sqe) return -1;
    io_uring_prep_connect(sqe, fd, addr, addrlen);
    io_uring_sqe_set_data64(sqe, user_data);
    return io_uring_submit(&ring);
}
