#define _GNU_SOURCE
#include "platform.h"
#include "game-identity.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>
#include <pulse/pulseaudio.h>

static const struct window_backend *backend;
void window_open(const char *title) {
    const char *choice = getenv("NES_DISPLAY");
    if (!choice || !*choice || !strcmp(choice, "auto")) {
        const char *wayland = getenv("WAYLAND_DISPLAY"), *socket = getenv("WAYLAND_SOCKET");
        backend = (wayland && *wayland) || (socket && *socket) ? &wayland_backend : &x11_backend;
    } else if (!strcmp(choice, "wayland")) backend = &wayland_backend;
    else if (!strcmp(choice, "x11")) backend = &x11_backend;
    else fatal("NES_DISPLAY must be auto, x11 or wayland.");
    backend->open(title);
}
struct event window_poll(void) { return backend->poll(); }
void window_title(const char *title) { backend->title(title); }
void window_fullscreen(bool enabled) { backend->fullscreen(enabled); }
void window_draw(const uint32_t *pixels, unsigned width, unsigned height) { backend->draw(pixels, width, height); }
void window_close(void) { backend->close(); }

double monotime(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

void scale_framebuffer(uint32_t *target, unsigned width, unsigned height,
                       const uint32_t *source, unsigned source_width, unsigned source_height) {
    size_t size = (size_t)width * height * 4;
    memset(target, 0, size);
    unsigned w = width, h = w * 3 / 4;
    if (h > height) { h = height; w = h * 4 / 3; }
    if (!w || !h) return;
    unsigned left = (width - w) / 2, top = (height - h) / 2;
    unsigned xs[8192];
    for (unsigned x = 0; x < w; ++x) xs[x] = x * source_width / w;
    for (unsigned y = 0; y < h; ++y) {
        unsigned sy = y * source_height / h;
        uint32_t *row = target + (size_t)(top + y) * width + left;
        if (y && sy == (y - 1) * source_height / h) memcpy(row, row - width, w * 4);
        else for (unsigned x = 0; x < w; ++x) row[x] = source[sy * source_width + xs[x]];
    }
}

static pa_threaded_mainloop *audio_loop;
static pa_context *context;
static pa_stream *stream;
static atomic_bool ready, failed;
static bool audio_running;
static unsigned sample_rate;
static double audio_start;
#define AUDIO_CAPACITY 32768
static int16_t queue[AUDIO_CAPACITY * 2];
static size_t read_at, queued;
static unsigned underruns, overruns;

static void audio_write(pa_stream *s, size_t bytes, void *unused) {
    (void)unused;
    int16_t output[2048];
    while (bytes >= 4) {
        size_t frames = bytes / 4;
        if (frames > 1024) frames = 1024;
        memset(output, 0, frames * 4);
        size_t available = frames < queued ? frames : queued;
        for (size_t i = 0; i < available; ++i) {
            output[2*i] = queue[2*read_at]; output[2*i+1] = queue[2*read_at+1];
            read_at = (read_at + 1) % AUDIO_CAPACITY;
        }
        queued -= available;
        if (available < frames) ++underruns;
        if (pa_stream_write(s, output, frames * 4, NULL, 0, PA_SEEK_RELATIVE) < 0) failed = true;
        bytes -= frames * 4;
    }
}

static void stream_state(pa_stream *s, void *unused) {
    (void)unused;
    if (pa_stream_get_state(s) == PA_STREAM_READY) {
        ready = true;
        message("Audio ready: %u Hz stereo; device=%s\n", sample_rate, pa_stream_get_device_name(s));
    } else if (pa_stream_get_state(s) == PA_STREAM_FAILED || pa_stream_get_state(s) == PA_STREAM_TERMINATED) failed = true;
}

static void context_state(pa_context *c, void *unused) {
    (void)unused;
    if (pa_context_get_state(c) == PA_CONTEXT_FAILED || pa_context_get_state(c) == PA_CONTEXT_TERMINATED) {
        message("Audio connection failed: %s\n", pa_strerror(pa_context_errno(c)));
        failed = true;
    } else if (pa_context_get_state(c) == PA_CONTEXT_READY) {
        pa_sample_spec spec = {PA_SAMPLE_S16LE, sample_rate, 2};
        pa_buffer_attr buffers = {UINT32_MAX, sample_rate * 4 / 10, UINT32_MAX, sample_rate * 4 / 100, UINT32_MAX};
        stream = pa_stream_new(c, "NES game audio", &spec, NULL);
        if (!stream) { failed = true; return; }
        pa_stream_set_state_callback(stream, stream_state, NULL);
        pa_stream_set_write_callback(stream, audio_write, NULL);
        if (pa_stream_connect_playback(stream, NULL, &buffers, PA_STREAM_ADJUST_LATENCY, NULL, NULL) < 0) failed = true;
    }
}

void audio_open(unsigned rate) {
    sample_rate = rate;
    char server[4096];
    const char *explicit = getenv("PULSE_SERVER"), *runtime = getenv("XDG_RUNTIME_DIR");
    int n;
    if (explicit && *explicit) n = snprintf(server, sizeof(server), "%s", explicit);
    else if (runtime && *runtime) n = snprintf(server, sizeof(server), "unix:%s/pulse/native", runtime);
    else n = snprintf(server, sizeof(server), "unix:/run/user/%lu/pulse/native", (unsigned long)getuid());
    if (n < 0 || n >= (int)sizeof(server) || strncmp(server, "unix:/", 6)) fatal("PULSE_SERVER must be a local unix:/absolute/socket path.");
    setenv("PULSE_CLIENTCONFIG", "/dev/null", 1);
    audio_loop = pa_threaded_mainloop_new();
    if (!audio_loop) fatal("Cannot create audio mainloop.");
    context = pa_context_new(pa_threaded_mainloop_get_api(audio_loop), GAME_TITLE);
    if (!context) fatal("Cannot create audio context.");
    pa_context_set_state_callback(context, context_state, NULL);
    audio_start = monotime();
    if (pa_context_connect(context, server, PA_CONTEXT_NOAUTOSPAWN, NULL) < 0) { failed = true; return; }
    if (pa_threaded_mainloop_start(audio_loop) < 0) { failed = true; return; }
    audio_running = true;
}

void audio_push(const int16_t *samples, size_t frames, unsigned volume) {
    if (!audio_loop) return;
    pa_threaded_mainloop_lock(audio_loop);
    if (queued + frames > AUDIO_CAPACITY) { read_at = queued = 0; ++overruns; }
    for (size_t i = 0; i < frames && queued < AUDIO_CAPACITY; ++i, ++queued) {
        size_t pos = (read_at + queued) % AUDIO_CAPACITY;
        queue[2*pos] = (int32_t)samples[2*i] * (int)volume / 100;
        queue[2*pos+1] = (int32_t)samples[2*i+1] * (int)volume / 100;
    }
    pa_threaded_mainloop_unlock(audio_loop);
}

void audio_clear(void) {
    if (!audio_loop) return;
    pa_threaded_mainloop_lock(audio_loop);
    read_at = queued = 0;
    if (ready) { pa_operation *op = pa_stream_flush(stream, NULL, NULL); if (op) pa_operation_unref(op); }
    pa_threaded_mainloop_unlock(audio_loop);
}

bool audio_check(void) {
    if (!audio_loop) return true;
    if (failed || (monotime() - audio_start > 10 && !ready)) {
        audio_close();
        message("Audio unavailable: paused; save with F5 or press Space to continue silently.\n");
        return false;
    }
    return true;
}

void audio_close(void) {
    if (!audio_loop) return;
    if (audio_running) pa_threaded_mainloop_stop(audio_loop);
    message("Audio queue: underruns=%u overruns=%u (includes startup and pauses)\n", underruns, overruns);
    if (stream) { pa_stream_set_state_callback(stream, NULL, NULL); pa_stream_disconnect(stream); pa_stream_unref(stream); }
    pa_context_set_state_callback(context, NULL, NULL);
    pa_context_disconnect(context); pa_context_unref(context);
    pa_threaded_mainloop_free(audio_loop);
    audio_loop = NULL;
}
