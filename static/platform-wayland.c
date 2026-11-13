#define _GNU_SOURCE
#include "platform.h"
#include "game-identity.h"
#include "xdg-shell.h"
#include <errno.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#define UNUSED __attribute__((unused))
static struct wl_display *display;
static struct wl_registry *registry;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_seat *seat;
static struct wl_keyboard *keyboard;
static struct xdg_wm_base *shell;
static struct wl_surface *surface;
static struct xdg_surface *shell_surface;
static struct xdg_toplevel *toplevel;
static struct wl_callback *frame;
static struct xkb_context *xkb;
static struct xkb_keymap *keymap;
static bool configured, focused;
static unsigned width = 640, height = 480, normal_width = 640, normal_height = 480;
static uint32_t seat_id;
static struct event events[128];
static unsigned event_start, event_count;

static void enqueue(struct event event) {
    if (event_count == 128) {
        event_start = event_count = 0;
        events[event_count++] = (struct event){.type = EVENT_FOCUS_LOST};
    }
    events[(event_start + event_count++) % 128] = event;
}

static void pump(int timeout) {
    while (wl_display_prepare_read(display) != 0)
        if (wl_display_dispatch_pending(display) < 0) fatal("Wayland connection closed.");
    int sent = wl_display_flush(display);
    if (sent < 0 && errno != EAGAIN) {
        wl_display_cancel_read(display);
        fatal("Wayland connection closed while sending requests.");
    }
    struct pollfd fd = {wl_display_get_fd(display), POLLIN | (sent < 0 ? POLLOUT : 0), 0};
    int result = poll(&fd, 1, timeout);
    if (result > 0 && (fd.revents & POLLIN)) {
        if (wl_display_read_events(display) < 0) fatal("Wayland connection closed while reading events.");
    } else wl_display_cancel_read(display);
    if ((result < 0 && errno != EINTR) || (fd.revents & (POLLERR | POLLHUP | POLLNVAL)))
        fatal("Wayland compositor disconnected.");
    if (wl_display_dispatch_pending(display) < 0) fatal("Wayland event dispatch failed.");
}

static void sync_done(void *data, struct wl_callback *callback, uint32_t serial UNUSED) {
    *(bool *)data = true;
    wl_callback_destroy(callback);
}
static const struct wl_callback_listener sync_listener = {sync_done};
static void roundtrip(void) {
    bool done = false;
    struct wl_callback *callback = wl_display_sync(display);
    wl_callback_add_listener(callback, &sync_listener, &done);
    double deadline = monotime() + 5;
    while (!done && monotime() < deadline) pump(50);
    if (!done) fatal("Wayland compositor did not respond within five seconds.");
}

static void keyboard_keymap(void *data UNUSED, struct wl_keyboard *object UNUSED,
                            uint32_t format, int32_t fd, uint32_t size) {
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || !size || size > 16 * 1024 * 1024) {
        close(fd); fatal("Unsupported Wayland keyboard map.");
    }
    char *text = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (text == MAP_FAILED) fatal("Cannot map Wayland keyboard layout.");
    struct xkb_keymap *next = text[size - 1] == 0 ?
        xkb_keymap_new_from_string(xkb, text, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS) : NULL;
    munmap(text, size);
    if (!next) fatal("Cannot parse Wayland keyboard layout.");
    if (keymap) enqueue((struct event){.type = EVENT_FOCUS_LOST});
    xkb_keymap_unref(keymap); keymap = next;
}
static void keyboard_enter(void *data UNUSED, struct wl_keyboard *object UNUSED,
                           uint32_t serial UNUSED, struct wl_surface *target, struct wl_array *keys UNUSED) {
    focused = target == surface;
}
static void keyboard_leave(void *data UNUSED, struct wl_keyboard *object UNUSED,
                           uint32_t serial UNUSED, struct wl_surface *target UNUSED) {
    focused = false;
    enqueue((struct event){.type = EVENT_FOCUS_LOST});
}
static void keyboard_key(void *data UNUSED, struct wl_keyboard *object UNUSED,
                         uint32_t serial UNUSED, uint32_t time UNUSED, uint32_t key, uint32_t state) {
    if (!focused || !keymap || key >= KEY_COUNT - 8) return;
    const xkb_keysym_t *symbols;
    unsigned code = key + 8;
    int count = xkb_keymap_key_get_syms_by_level(keymap, code, 0, 0, &symbols);
    if (count > 0) enqueue((struct event){EVENT_KEY, symbols[0], code, state == WL_KEYBOARD_KEY_STATE_PRESSED});
}
static void keyboard_modifiers(void *data UNUSED, struct wl_keyboard *object UNUSED,
                               uint32_t serial UNUSED, uint32_t depressed UNUSED, uint32_t latched UNUSED,
                               uint32_t locked UNUSED, uint32_t group UNUSED) {}
static void keyboard_repeat(void *data UNUSED, struct wl_keyboard *object UNUSED,
                            int32_t rate UNUSED, int32_t delay UNUSED) {}
static const struct wl_keyboard_listener keyboard_listener = {
    keyboard_keymap, keyboard_enter, keyboard_leave, keyboard_key, keyboard_modifiers, keyboard_repeat
};
static void seat_capabilities(void *data UNUSED, struct wl_seat *object, uint32_t capabilities) {
    if ((capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && !keyboard) {
        keyboard = wl_seat_get_keyboard(object);
        wl_keyboard_add_listener(keyboard, &keyboard_listener, NULL);
    } else if (!(capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && keyboard) {
        wl_keyboard_release(keyboard); keyboard = NULL; focused = false;
        enqueue((struct event){.type = EVENT_FOCUS_LOST});
    }
}
static void seat_name(void *data UNUSED, struct wl_seat *object UNUSED, const char *name UNUSED) {}
static const struct wl_seat_listener seat_listener = {seat_capabilities, seat_name};
static void ping(void *data UNUSED, struct xdg_wm_base *object, uint32_t serial) { xdg_wm_base_pong(object, serial); }
static const struct xdg_wm_base_listener shell_listener = {ping};
static void global(void *data UNUSED, struct wl_registry *object, uint32_t name,
                   const char *interface, uint32_t version) {
    if (!strcmp(interface, "wl_compositor") && !compositor && version >= 4)
        compositor = wl_registry_bind(object, name, &wl_compositor_interface, 4);
    else if (!strcmp(interface, "wl_shm") && !shm) shm = wl_registry_bind(object, name, &wl_shm_interface, 1);
    else if (!strcmp(interface, "xdg_wm_base") && !shell) {
        shell = wl_registry_bind(object, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(shell, &shell_listener, NULL);
    } else if (!strcmp(interface, "wl_seat") && !seat && version >= 5) {
        seat_id = name;
        seat = wl_registry_bind(object, name, &wl_seat_interface, 5);
        wl_seat_add_listener(seat, &seat_listener, NULL);
    }
}
static void global_remove(void *data UNUSED, struct wl_registry *object UNUSED, uint32_t name) {
    if (name == seat_id && seat) {
        if (keyboard) { wl_keyboard_release(keyboard); keyboard = NULL; }
        wl_seat_destroy(seat); seat = NULL; focused = false;
        enqueue((struct event){.type = EVENT_FOCUS_LOST});
    }
}
static const struct wl_registry_listener registry_listener = {global, global_remove};
static void surface_configure(void *data UNUSED, struct xdg_surface *object, uint32_t serial) {
    xdg_surface_ack_configure(object, serial); configured = true;
}
static const struct xdg_surface_listener surface_listener = {surface_configure};
static void top_configure(void *data UNUSED, struct xdg_toplevel *object UNUSED,
                          int32_t w, int32_t h, struct wl_array *states) {
    bool fullscreen = false;
    uint32_t *state;
    wl_array_for_each(state, states) if (*state == XDG_TOPLEVEL_STATE_FULLSCREEN) fullscreen = true;
    if (w < 0 || h < 0 || w > 8192 || h > 8192) fatal("Unsupported Wayland window dimensions.");
    width = w ? (unsigned)w : normal_width;
    height = h ? (unsigned)h : normal_height;
    if (!fullscreen) { normal_width = width; normal_height = height; }
    message("Wayland configure: %ux%u fullscreen=%u\n", width, height, fullscreen);
}
static void top_close(void *data UNUSED, struct xdg_toplevel *object UNUSED) { enqueue((struct event){.type = EVENT_CLOSE}); }
static const struct xdg_toplevel_listener top_listener = {.configure = top_configure, .close = top_close};

static void wayland_title(const char *title) { xdg_toplevel_set_title(toplevel, title); }
static void wayland_open(const char *title) {
    display = wl_display_connect(NULL);
    if (!display) fatal("Cannot connect to Wayland; check WAYLAND_DISPLAY and XDG_RUNTIME_DIR (or select NES_DISPLAY=x11).");
    xkb = xkb_context_new(XKB_CONTEXT_NO_DEFAULT_INCLUDES | XKB_CONTEXT_NO_ENVIRONMENT_NAMES);
    if (!xkb) fatal("Cannot initialize Wayland keyboard support.");
    registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    roundtrip();
    if (!compositor || !shm || !shell) fatal("Wayland requires wl_compositor v4, wl_shm and xdg_wm_base.");
    surface = wl_compositor_create_surface(compositor);
    shell_surface = xdg_wm_base_get_xdg_surface(shell, surface);
    xdg_surface_add_listener(shell_surface, &surface_listener, NULL);
    toplevel = xdg_surface_get_toplevel(shell_surface);
    xdg_toplevel_add_listener(toplevel, &top_listener, NULL);
    xdg_toplevel_set_app_id(toplevel, GAME_ID);
    wayland_title(title);
    wl_surface_commit(surface);
    double deadline = monotime() + 5;
    while (!configured && monotime() < deadline) pump(50);
    if (!configured) fatal("Wayland compositor did not configure the game window.");
    message("Wayland ready: native xdg-shell, shared-memory renderer\n");
}
static struct event wayland_poll(void) {
    if (!event_count) pump(0);
    if (!event_count) return (struct event){0};
    struct event event = events[event_start];
    event_start = (event_start + 1) % 128; --event_count;
    return event;
}
static void wayland_fullscreen(bool enabled) {
    if (enabled) xdg_toplevel_set_fullscreen(toplevel, NULL);
    else xdg_toplevel_unset_fullscreen(toplevel);
}

struct buffer {
    struct wl_buffer *object;
    uint32_t *pixels;
    size_t size;
    unsigned width, height;
    bool busy;
};
static struct buffer buffers[2];
static void buffer_release(void *data, struct wl_buffer *object UNUSED) { ((struct buffer *)data)->busy = false; }
static const struct wl_buffer_listener buffer_listener = {buffer_release};
static void buffer_destroy(struct buffer *buffer) {
    if (buffer->object) wl_buffer_destroy(buffer->object);
    if (buffer->pixels) munmap(buffer->pixels, buffer->size);
    memset(buffer, 0, sizeof(*buffer));
}
static void buffer_create(struct buffer *buffer) {
    buffer_destroy(buffer);
    buffer->size = (size_t)width * height * 4;
    int fd = memfd_create("nes-wayland-frame", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, buffer->size)) fatal("Cannot create Wayland shared-memory buffer.");
    void *pixels = mmap(NULL, buffer->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { close(fd); fatal("Cannot map Wayland shared-memory buffer."); }
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, buffer->size);
    buffer->object = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4, WL_SHM_FORMAT_XRGB8888);
    wl_shm_pool_destroy(pool); close(fd);
    buffer->pixels = pixels; buffer->width = width; buffer->height = height;
    wl_buffer_add_listener(buffer->object, &buffer_listener, buffer);
}
static void frame_done(void *data UNUSED, struct wl_callback *object, uint32_t time UNUSED) {
    wl_callback_destroy(object); frame = NULL;
}
static const struct wl_callback_listener frame_listener = {frame_done};
static void wayland_draw(const uint32_t *pixels, unsigned source_width, unsigned source_height) {
    if (frame) return;
    struct buffer *buffer = !buffers[0].busy ? &buffers[0] : !buffers[1].busy ? &buffers[1] : NULL;
    if (!buffer) return;
    if (!buffer->object || buffer->width != width || buffer->height != height) buffer_create(buffer);
    scale_framebuffer(buffer->pixels, width, height, pixels, source_width, source_height);
    wl_surface_attach(surface, buffer->object, 0, 0);
    wl_surface_damage_buffer(surface, 0, 0, width, height);
    frame = wl_surface_frame(surface);
    wl_callback_add_listener(frame, &frame_listener, NULL);
    buffer->busy = true;
    wl_surface_commit(surface);
    if (wl_display_flush(display) < 0 && errno != EAGAIN) fatal("Wayland connection lost while drawing.");
}
static void wayland_close(void) {
    if (frame) wl_callback_destroy(frame);
    xdg_toplevel_destroy(toplevel); xdg_surface_destroy(shell_surface); wl_surface_destroy(surface);
    for (unsigned i = 0; i < 2; ++i) buffer_destroy(&buffers[i]);
    if (keyboard) wl_keyboard_release(keyboard);
    if (seat) wl_seat_destroy(seat);
    xdg_wm_base_destroy(shell); wl_shm_destroy(shm); wl_compositor_destroy(compositor);
    wl_registry_destroy(registry); wl_display_disconnect(display);
    xkb_keymap_unref(keymap); xkb_context_unref(xkb);
}
const struct window_backend wayland_backend = {wayland_open, wayland_poll, wayland_title, wayland_fullscreen, wayland_draw, wayland_close};
