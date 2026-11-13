#define _GNU_SOURCE
#include "platform.h"
#include "game-identity.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <xcb/xcb.h>
#include <xcb/shm.h>
#include <xcb/xkb.h>

static xcb_connection_t *display;
static xcb_screen_t *screen;
static xcb_window_t window;
static xcb_gcontext_t gc;
static xcb_atom_t protocols, wm_delete;
static xcb_get_keyboard_mapping_reply_t *keys;
static uint8_t min_key, max_key;
static unsigned window_width = 640, window_height = 480;
static uint32_t *framebuffer;
static size_t framebuffer_size;
static xcb_shm_seg_t segment;
static bool use_shm;

static xcb_atom_t atom(const char *name) {
    xcb_intern_atom_reply_t *r = xcb_intern_atom_reply(display, xcb_intern_atom(display, 0, strlen(name), name), NULL);
    if (!r) fatal("X11 atom lookup failed.");
    xcb_atom_t value = r->atom;
    free(r);
    return value;
}

static void x11_title(const char *title) {
    xcb_change_property(display, XCB_PROP_MODE_REPLACE, window, XCB_ATOM_WM_NAME,
                        XCB_ATOM_STRING, 8, strlen(title), title);
    xcb_flush(display);
}

static void refresh_keys(void) {
    free(keys);
    keys = xcb_get_keyboard_mapping_reply(display,
        xcb_get_keyboard_mapping(display, min_key, max_key - min_key + 1), NULL);
    if (!keys) fatal("Cannot read X11 keyboard mapping.");
}

static void x11_open(const char *title) {
    int number;
    display = xcb_connect(NULL, &number);
    if (xcb_connection_has_error(display)) fatal("Cannot connect to X11; check DISPLAY and Xauthority.");
    const xcb_setup_t *setup = xcb_get_setup(display);
    xcb_screen_iterator_t it = xcb_setup_roots_iterator(setup);
    while (number-- && it.rem) xcb_screen_next(&it);
    if (!it.rem) fatal("X11 screen unavailable.");
    screen = it.data;
    bool supported = false;
    for (xcb_format_iterator_t f = xcb_setup_pixmap_formats_iterator(setup); f.rem; xcb_format_next(&f))
        if (f.data->depth == 24 && f.data->bits_per_pixel == 32) supported = true;
    if (!supported || screen->root_depth != 24 || setup->image_byte_order != XCB_IMAGE_ORDER_LSB_FIRST)
        fatal("This renderer requires 24-bit X11 color stored in little-endian 32-bit pixels.");
    window = xcb_generate_id(display);
    uint32_t values[] = {0, XCB_EVENT_MASK_EXPOSURE | XCB_EVENT_MASK_KEY_PRESS | XCB_EVENT_MASK_KEY_RELEASE |
        XCB_EVENT_MASK_STRUCTURE_NOTIFY | XCB_EVENT_MASK_FOCUS_CHANGE};
    xcb_create_window(display, 24, window, screen->root, 0, 0, 640, 480, 0, XCB_WINDOW_CLASS_INPUT_OUTPUT,
        screen->root_visual, XCB_CW_BACK_PIXEL | XCB_CW_EVENT_MASK, values);
    const char cls[] = GAME_ID "\0NESStaticGame";
    xcb_change_property(display, XCB_PROP_MODE_REPLACE, window, XCB_ATOM_WM_CLASS,
                        XCB_ATOM_STRING, 8, sizeof(cls), cls);
    protocols = atom("WM_PROTOCOLS"); wm_delete = atom("WM_DELETE_WINDOW");
    xcb_change_property(display, XCB_PROP_MODE_REPLACE, window, protocols, XCB_ATOM_ATOM, 32, 1, &wm_delete);
    x11_title(title);
    gc = xcb_generate_id(display);
    xcb_create_gc(display, gc, window, 0, NULL);
    const xcb_query_extension_reply_t *ext = xcb_get_extension_data(display, &xcb_shm_id);
    if (ext && ext->present && !getenv("NES_NO_SHM")) {
        xcb_shm_query_version_reply_t *version = xcb_shm_query_version_reply(display, xcb_shm_query_version(display), NULL);
        use_shm = version && (version->major_version > 1 || (version->major_version == 1 && version->minor_version >= 2));
        free(version);
    }
    min_key = setup->min_keycode; max_key = setup->max_keycode;
    refresh_keys();
    xcb_xkb_use_extension_reply_t *xkb = xcb_xkb_use_extension_reply(display,
        xcb_xkb_use_extension(display, 1, 0), NULL);
    if (!xkb || !xkb->supported) fatal("X11 keyboard extension is unavailable.");
    free(xkb);
    unsigned repeat = XCB_XKB_PER_CLIENT_FLAG_DETECTABLE_AUTO_REPEAT;
    xcb_xkb_per_client_flags_reply_t *flags = xcb_xkb_per_client_flags_reply(display,
        xcb_xkb_per_client_flags(display, XCB_XKB_ID_USE_CORE_KBD, repeat, repeat, 0, 0, 0), NULL);
    if (!flags || !(flags->value & repeat)) fatal("X11 detectable keyboard repeat is unavailable.");
    free(flags);
    xcb_map_window(display, window);
    xcb_flush(display);
    message("X11 ready: window=0x%x shared-memory=%s\n", window, use_shm ? "yes" : "no");
}

static struct event x11_poll(void) {
    struct event result = {0};
    if (xcb_connection_has_error(display)) fatal("X11 connection closed.");
    xcb_generic_event_t *event;
    while ((event = xcb_poll_for_event(display))) {
        unsigned type = event->response_type & 0x7f;
        switch (type) {
        case XCB_CONFIGURE_NOTIFY: {
            xcb_configure_notify_event_t *e = (void *)event;
            window_width = e->width; window_height = e->height;
            if (!window_width || !window_height || window_width > 8192 || window_height > 8192)
                fatal("Unsupported window dimensions.");
            break;
        }
        case XCB_MAPPING_NOTIFY: refresh_keys(); break;
        case XCB_FOCUS_OUT: result.type = EVENT_FOCUS_LOST; break;
        case XCB_CLIENT_MESSAGE: {
            xcb_client_message_event_t *e = (void *)event;
            if (e->type == protocols && e->data.data32[0] == wm_delete) result.type = EVENT_CLOSE;
            break;
        }
        case XCB_KEY_PRESS: case XCB_KEY_RELEASE: {
            xcb_key_press_event_t *e = (void *)event;
            if (e->detail < min_key || e->detail > max_key) break;
            result = (struct event){EVENT_KEY,
                xcb_get_keyboard_mapping_keysyms(keys)[(e->detail - min_key) * keys->keysyms_per_keycode],
                e->detail, type == XCB_KEY_PRESS};
            break;
        }
        case 0: fatal("X11 protocol error.");
        default: break;
        }
        free(event);
        if (result.type) return result;
    }
    return result;
}

static void x11_fullscreen(bool enabled) {
    xcb_client_message_event_t e = {0};
    e.response_type = XCB_CLIENT_MESSAGE; e.format = 32; e.window = window;
    e.type = atom("_NET_WM_STATE"); e.data.data32[0] = enabled ? 1 : 0;
    e.data.data32[1] = atom("_NET_WM_STATE_FULLSCREEN"); e.data.data32[3] = 1;
    xcb_send_event(display, 0, screen->root,
        XCB_EVENT_MASK_SUBSTRUCTURE_REDIRECT | XCB_EVENT_MASK_SUBSTRUCTURE_NOTIFY, (char *)&e);
    xcb_flush(display);
}

static void release_framebuffer(void) {
    if (!framebuffer) return;
    if (segment) {
        xcb_shm_detach(display, segment);
        munmap(framebuffer, framebuffer_size);
        segment = 0;
    } else free(framebuffer);
    framebuffer = NULL;
}

static void x11_draw(const uint32_t *source, unsigned source_width, unsigned source_height) {
    size_t size = (size_t)window_width * window_height * 4;
    if (size != framebuffer_size || !framebuffer) {
        release_framebuffer();
        framebuffer_size = size;
        if (use_shm) {
            int fd = memfd_create("nes-framebuffer", MFD_CLOEXEC);
            if (fd >= 0 && ftruncate(fd, size) == 0) {
                void *memory = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
                if (memory != MAP_FAILED) {
                    xcb_shm_seg_t id = xcb_generate_id(display);
                    // XCB owns the descriptor after sending it to the server.
                    xcb_generic_error_t *error = xcb_request_check(display, xcb_shm_attach_fd_checked(display, id, fd, 0));
                    fd = -1;
                    if (!error) { framebuffer = memory; segment = id; }
                    else { free(error); munmap(memory, size); }
                }
            }
            if (fd >= 0) close(fd);
            if (!framebuffer) { use_shm = false; message("Shared memory unavailable; using X11 image transfer.\n"); }
        }
        if (!framebuffer) framebuffer = malloc(size);
        if (!framebuffer) fatal("Cannot allocate framebuffer.");
    }
    scale_framebuffer(framebuffer, window_width, window_height, source, source_width, source_height);
    if (segment) {
        xcb_shm_put_image(display, window, gc, window_width, window_height, 0, 0,
            window_width, window_height, 0, 0, 24, XCB_IMAGE_FORMAT_Z_PIXMAP, 0, segment, 0);
        // Finish server access before reusing the shared buffer.
        free(xcb_get_input_focus_reply(display, xcb_get_input_focus(display), NULL));
    } else {
        unsigned rows = (xcb_get_maximum_request_length(display) * 4 - 64) / (window_width * 4);
        if (!rows) fatal("X11 request limit is too small.");
        for (unsigned y = 0; y < window_height; y += rows) {
            unsigned n = window_height - y < rows ? window_height - y : rows;
            xcb_put_image(display, XCB_IMAGE_FORMAT_Z_PIXMAP, window, gc, window_width, n, 0, y, 0, 24,
                n * window_width * 4, (uint8_t *)(framebuffer + (size_t)y * window_width));
        }
    }
    if (xcb_flush(display) <= 0) fatal("X11 connection lost while drawing.");
}

static void x11_close(void) {
    release_framebuffer();
    free(keys);
    if (display) xcb_disconnect(display);
}

const struct window_backend x11_backend = {x11_open, x11_poll, x11_title, x11_fullscreen, x11_draw, x11_close};
