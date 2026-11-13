#ifndef NES_PLATFORM_H
#define NES_PLATFORM_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

enum { KEY_COUNT = 1024 };
enum event_type { EVENT_NONE, EVENT_KEY, EVENT_FOCUS_LOST, EVENT_CLOSE };
struct event { enum event_type type; uint32_t key; unsigned code; bool down; };
void message(const char *format, ...);
void fatal(const char *message);
double monotime(void);
void window_open(const char *title);
struct event window_poll(void);
void window_title(const char *title);
void window_fullscreen(bool enabled);
void window_draw(const uint32_t *pixels, unsigned width, unsigned height);
void window_close(void);
void audio_open(unsigned rate);
void audio_push(const int16_t *samples, size_t frames, unsigned volume);
void audio_clear(void);
bool audio_check(void);
void audio_close(void);
struct window_backend {
    void (*open)(const char *title);
    struct event (*poll)(void);
    void (*title)(const char *title);
    void (*fullscreen)(bool enabled);
    void (*draw)(const uint32_t *pixels, unsigned width, unsigned height);
    void (*close)(void);
};
extern const struct window_backend x11_backend, wayland_backend;
void scale_framebuffer(uint32_t *target, unsigned width, unsigned height,
                       const uint32_t *source, unsigned source_width, unsigned source_height);
#endif
