// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the patched wait while another thread dispatches the default queue.
#include <assert.h>
#include <pthread.h>
#include <sys/socket.h>

extern void *wl_display_create(void);
extern void *wl_client_create(void *, int);
extern void wl_display_run(void *);
extern void *wl_display_connect_to_fd(int);
extern int wl_display_dispatch(void *);
extern void wl_proxy_destroy(void *);
extern void *wl_proxy_get_queue(void *);
extern int patched_roundtrip(void *, void *);

static void done(void *data, void *callback, unsigned serial)
{
    *(int *)data = 1;
    wl_proxy_destroy(callback);
}
void (*done_listener)(void *, void *, unsigned) = done;

static void *server(void *display)
{
    wl_display_run(display);
    return NULL;
}

static void *dispatch(void *display)
{
    while (wl_display_dispatch(display) >= 0) {}
    return NULL;
}

int main(void)
{
    int sockets[2];
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    void *server_display = wl_display_create();
    assert(server_display && wl_client_create(server_display, sockets[0]));
    void *client_display = wl_display_connect_to_fd(sockets[1]);
    assert(client_display);
    pthread_t server_thread, dispatch_thread;
    assert(pthread_create(&server_thread, NULL, server, server_display) == 0);
    assert(pthread_create(&dispatch_thread, NULL, dispatch, client_display) == 0);
    for (int i = 0; i < 2000; i++)
        assert(patched_roundtrip(client_display, wl_proxy_get_queue(client_display)) >= 0);
    // Process exit stops the two blocking event loops and closes their sockets.
    return 0;
}
