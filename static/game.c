#define _POSIX_C_SOURCE 200809L
#include "platform.h"
#include "libretro.h"
#include "game-data.h"
#include "game-identity.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static FILE *log_file;
static char state_dir[PATH_MAX];
static volatile sig_atomic_t stopping;
static bool paused, muted, held[KEY_COUNT], pressed[KEY_COUNT];
static uint32_t held_symbol[KEY_COUNT];
static unsigned slot, volume = 80;
static bool fullscreen;
static uint32_t video[256 * 240];
static unsigned video_width = 256, video_height = 240;
static uint64_t frames, audio_frames;
static bool core_live, recovering;
static bool save_snapshot(const char *name);
static bool battery_save(void);
static const char *button_names[] = {"a", "b", "select", "start", "up", "down", "left", "right"};
static const unsigned button_ids[] = {8, 0, 2, 3, 4, 5, 6, 7};
static uint32_t bindings[] = {'j', 'k', 0xff09, 0xff0d, 'w', 's', 'a', 'd'};
static struct { const char *name; uint32_t value; } key_names[] = {
    {"Return", 0xff0d}, {"Left", 0xff51}, {"Up", 0xff52}, {"Right", 0xff53}, {"Down", 0xff54},
    {"Tab", 0xff09}, {"space", 0x20}, {"Escape", 0xff1b}, {"F1", 0xffbe}, {"F2", 0xffbf},
    {"F5", 0xffc2}, {"F8", 0xffc5}, {"F11", 0xffc8}
};

void message(const char *format, ...) {
    va_list args, copy;
    va_start(args, format); va_copy(copy, args);
    vfprintf(stderr, format, args);
    if (log_file) { vfprintf(log_file, format, copy); fflush(log_file); }
    va_end(copy); va_end(args);
}

void fatal(const char *text) {
    message("Error: %s\n", text);
    if (core_live && frames && !recovering) {
        recovering = true;
        battery_save();
        if (save_snapshot("recovery.state"))
            message("Recovery saved; restart with --recover to resume.\n");
    }
    exit(1);
}
static void on_signal(int signal) { (void)signal; stopping = 1; }

static void path_for(char *path, size_t capacity, const char *name) {
    int n = snprintf(path, capacity, "%s/%s", state_dir, name);
    if (n < 0 || n >= (int)capacity) fatal("State filename is too long.");
}

static int open_state(void) {
    const char *base = getenv("NES_STATE_DIR");
    char parent[PATH_MAX];
    int n;
    if (base && *base) n = snprintf(parent, sizeof(parent), "%s", base);
    else {
        char executable[PATH_MAX];
        ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
        if (length < 0 || length >= (ssize_t)sizeof(executable) - 1)
            fatal("Cannot locate executable; set NES_STATE_DIR to a writable absolute directory.");
        executable[length] = 0;
        char *slash = strrchr(executable, '/');
        if (!slash) fatal("Cannot locate executable directory; set NES_STATE_DIR.");
        *slash = 0;
        n = snprintf(parent, sizeof(parent), "%s/saves", executable);
    }
    if (n < 0 || n >= (int)sizeof(parent)) fatal("State path is too long.");
    n = snprintf(state_dir, sizeof(state_dir), "%s/%s/%s", parent, GAME_ID, ROM_SHA256);
    if (n < 0 || n >= (int)sizeof(state_dir) - 40 || state_dir[0] != '/') fatal("State directory must be an absolute path of reasonable length.");
    for (char *p = state_dir + 1; ; ++p) {
        if (*p != '/' && *p) continue;
        char c = *p; *p = 0;
        if (mkdir(state_dir, 0700) != 0 && errno != EEXIST) fatal("Cannot create state directory.");
        *p = c;
        if (!c) break;
    }
    char path[PATH_MAX]; path_for(path, sizeof(path), "run.lock");
    int fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (fd < 0) fatal("Cannot open game lock.");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) fatal("Another game instance is using this state directory.");
    path_for(path, sizeof(path), "game.log");
    log_file = fopen(path, "w");
    if (!log_file) fatal("Cannot open game log.");
    message("Log: %s\nBuild: %s; FCEUmm %s\nROM: %s\n", path, BUILD_ID, CORE_REVISION, ROM_SHA256);
    return fd;
}

static bool atomic_write(const char *name, const void *header, size_t header_size, const void *data, size_t size) {
    char path[PATH_MAX], temporary[PATH_MAX];
    path_for(path, sizeof(path), name); path_for(temporary, sizeof(temporary), ".write-XXXXXX");
    int fd = mkstemp(temporary);
    if (fd < 0) { message("Write failed for %s: %s\n", name, strerror(errno)); return false; }
    FILE *file = fdopen(fd, "wb");
    if (!file) { close(fd); unlink(temporary); return false; }
    bool ok = fwrite(header, 1, header_size, file) == header_size && fwrite(data, 1, size, file) == size;
    if (ok) ok = fflush(file) == 0 && fsync(fd) == 0;
    if (fclose(file) != 0) ok = false;
    if (ok) ok = rename(temporary, path) == 0;
    if (!ok) { message("Write failed for %s: %s; previous file retained.\n", name, strerror(errno)); unlink(temporary); }
    else {
        int directory = open(state_dir, O_RDONLY | O_DIRECTORY);
        if (directory >= 0) { if (fsync(directory) != 0) message("Directory sync failed: %s\n", strerror(errno)); close(directory); }
    }
    return ok;
}

static bool reserved(uint32_t key) {
    return key == 0x20 || key == 0xff1b || key == 0xffbe || key == 0xffbf || key == 0xffc2 || key == 0xffc5 || key == 0xffc8 || key == '[' || key == ']';
}

static uint32_t parse_key(const char *text) {
    if (strlen(text) == 1) return (unsigned char)text[0];
    for (unsigned i = 0; i < sizeof(key_names)/sizeof(*key_names); ++i)
        if (!strcmp(key_names[i].name, text)) return key_names[i].value;
    if (strncmp(text, "0x", 2)) return 0;
    char *end; unsigned long key = strtoul(text, &end, 16);
    return !*end && key <= UINT32_MAX ? (uint32_t)key : 0;
}

static const char *key_name(uint32_t key) {
    static char buffer[32];
    for (unsigned i = 0; i < sizeof(key_names)/sizeof(*key_names); ++i)
        if (key_names[i].value == key) return key_names[i].name;
    if (key >= 33 && key < 127) snprintf(buffer, sizeof(buffer), "%c", (int)key);
    else snprintf(buffer, sizeof(buffer), "0x%x", key);
    return buffer;
}

static bool setting(const char *assignment) {
    char name[32], value[64], extra;
    if (sscanf(assignment, "%31[^=]=%63s %c", name, value, &extra) != 2) return false;
    for (unsigned i = 0; i < 8; ++i) if (!strcmp(name, button_names[i])) {
        uint32_t key = parse_key(value);
        if (!key || reserved(key)) return false;
        bindings[i] = key; return true;
    }
    char *end; long v = strtol(value, &end, 10);
    if (*end) return false;
    if (!strcmp(name, "volume") && v >= 0 && v <= 100) { volume = v; return true; }
    if (!strcmp(name, "fullscreen") && (v == 0 || v == 1)) { fullscreen = v; return true; }
    return false;
}

static bool config_save(void) {
    char text[2048] =
        "# NES controls. Edit while the game is closed; changes apply next launch.\n"
        "# a/b: game buttons, select/start: game menu buttons.\n"
        "# Use lowercase letters or named keys: Return, Tab, Left, Right, Up, Down.\n"
        "# Fixed shortcuts: Space pause, F2 reset, F5 save, F8 load, [/] slot, F11 fullscreen, Escape quit.\n"
        "# Volume: 0-100. Fullscreen: 0 or 1.\n\n";
    size_t length = strlen(text);
    for (unsigned i = 0; i < 8; ++i) length += snprintf(text + length, sizeof(text) - length, "%s=%s\n", button_names[i], key_name(bindings[i]));
    length += snprintf(text + length, sizeof(text) - length, "volume=%u\nfullscreen=%u\n", volume, fullscreen);
    return atomic_write("settings.conf", "", 0, text, length);
}

static void config_load(void) {
    char path[PATH_MAX], line[256]; path_for(path, sizeof(path), "settings.conf");
    FILE *file = fopen(path, "r");
    if (!file) {
        if (errno != ENOENT) fatal("Cannot read settings.conf.");
        if (!config_save()) fatal("Cannot write initial settings.");
        return;
    }
    while (fgets(line, sizeof(line), file)) {
        if (*line == '#' || *line == '\n') continue;
        if (!setting(line)) { fclose(file); fatal("Invalid settings.conf; existing file preserved. See --help for settings syntax."); }
    }
    if (ferror(file)) fatal("Error reading settings.conf.");
    fclose(file);
}

static void configure(bool interactive) {
    printf("Settings: %s/settings.conf\n", state_dir);
    puts("Controls: a/b = game buttons, select/start = game menu buttons.");
    for (unsigned i = 0; i < 8; ++i) printf("%s=%s\n", button_names[i], key_name(bindings[i]));
    printf("volume=%u\nfullscreen=%u\n", volume, fullscreen);
    if (!interactive) return;
    puts("Enter a setting such as a=x, start=Return, volume=70 or fullscreen=1. Empty line finishes.");
    char line[256];
    while (printf("> "), fflush(stdout), fgets(line, sizeof(line), stdin) && strcmp(line, "\n")) {
        if (!setting(line)) { puts("Invalid setting or reserved shortcut."); continue; }
        if (!config_save()) fatal("Cannot save configuration.");
        puts("Saved.");
    }
}

static uint32_t checksum(const uint8_t *data, size_t size) {
    uint32_t crc = ~0u;
    for (size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (unsigned j = 0; j < 8; ++j) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    return ~crc;
}
static void put32(uint8_t *p, uint32_t v) { for (unsigned i = 0; i < 4; ++i) p[i] = v >> (i * 8); }
static uint32_t get32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24; }

static uint8_t *battery_previous;
static size_t battery_size;
static bool battery_ready;
static double next_battery_save;

static void battery_open(void) {
    size_t size = retro_get_memory_size(RETRO_MEMORY_SAVE_RAM);
    uint8_t *memory = retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
    if (!size) {
        if (GAME_BATTERY) fatal("Core did not expose this cartridge's battery memory; refusing to lose saves.");
        return;
    }
    if ((GAME_MAPPER != 0 && GAME_MAPPER != 1 && GAME_MAPPER != 4) || !memory || size > 1024 * 1024)
        fatal("Unsupported cartridge battery layout.");
    battery_previous = malloc(size);
    if (!battery_previous) fatal("Cannot allocate battery buffer.");
    char path[PATH_MAX]; path_for(path, sizeof(path), "battery.ram");
    FILE *file = fopen(path, "rb");
    if (file) {
        uint8_t header[128];
        bool ok = fread(header, 1, sizeof(header), file) == sizeof(header) &&
            !memcmp(header, "NESRAM01", 8) && !memcmp(header + 8, ROM_SHA256, 64) && get32(header + 72) == size;
        if (ok) ok = fread(battery_previous, 1, size, file) == size && fgetc(file) == EOF &&
            !ferror(file) && checksum(battery_previous, size) == get32(header + 76);
        fclose(file);
        if (!ok) fatal("Invalid battery.ram; existing file preserved. Restore a backup before playing.");
        memcpy(memory, battery_previous, size);
        message("Battery: loaded %zu bytes\n", size);
    } else {
        if (errno != ENOENT) fatal("Cannot read battery.ram; existing file preserved.");
        memcpy(battery_previous, memory, size);
        message("Battery: new cartridge memory (%zu bytes)\n", size);
    }
    battery_size = size; battery_ready = true;
    next_battery_save = monotime() + 5;
}

static bool battery_save(void) {
    if (!battery_ready) return true;
    const uint8_t *memory = retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
    if (!memory || retro_get_memory_size(RETRO_MEMORY_SAVE_RAM) != battery_size) {
        message("Battery: memory layout changed; previous save retained.\n"); return false;
    }
    if (!memcmp(memory, battery_previous, battery_size)) return true;
    uint8_t header[128] = {0};
    memcpy(header, "NESRAM01", 8); memcpy(header + 8, ROM_SHA256, 64);
    put32(header + 72, battery_size); put32(header + 76, checksum(memory, battery_size));
    if (!atomic_write("battery.ram", header, sizeof(header), memory, battery_size)) return false;
    memcpy(battery_previous, memory, battery_size);
    message("Battery: saved %zu bytes\n", battery_size);
    return true;
}

static bool save_snapshot(const char *name) {
    size_t size = retro_serialize_size();
    if (!size || size > 16 * 1024 * 1024) return false;
    uint8_t *data = malloc(size);
    if (!data) return false;
    bool ok = retro_serialize(data, size);
    uint8_t header[128] = {0};
    memcpy(header, "NESSTAT1", 8); memcpy(header + 8, ROM_SHA256, 64); memcpy(header + 72, CORE_REVISION, 40);
    put32(header + 112, size);
    if (ok) put32(header + 116, checksum(data, size));
    if (ok) ok = atomic_write(name, header, sizeof(header), data, size);
    free(data);
    return ok;
}
static bool save_state(unsigned number) {
    char name[32]; snprintf(name, sizeof(name), "slot-%u.state", number);
    bool ok = save_snapshot(name);
    message("Save slot %u: %s\n", number, ok ? "saved" : "failed");
    return ok;
}

static bool load_state(int number) {
    char name[32], label[32], path[PATH_MAX];
    if (number < 0) { strcpy(name, "recovery.state"); strcpy(label, "recovery"); }
    else { snprintf(name, sizeof(name), "slot-%d.state", number); snprintf(label, sizeof(label), "slot %d", number); }
    path_for(path, sizeof(path), name);
    FILE *file = fopen(path, "rb");
    if (!file) { message("Load %s: %s\n", label, strerror(errno)); return false; }
    uint8_t header[128]; size_t size = retro_serialize_size();
    bool ok = fread(header, 1, sizeof(header), file) == sizeof(header) &&
        !memcmp(header, "NESSTAT1", 8) && !memcmp(header + 8, ROM_SHA256, 64) &&
        !memcmp(header + 72, CORE_REVISION, 40) && get32(header + 112) == size && size && size <= 16 * 1024 * 1024;
    uint8_t *data = ok ? malloc(size) : NULL, *backup = ok ? malloc(size) : NULL;
    if (!data || !backup) ok = false;
    if (ok) ok = fread(data, 1, size, file) == size && fgetc(file) == EOF && !ferror(file) && checksum(data, size) == get32(header + 116);
    fclose(file);
    if (ok) {
        ok = retro_serialize(backup, size);
        if (ok && !retro_unserialize(data, size)) {
            if (!retro_unserialize(backup, size)) fatal("Cannot restore current state after a rejected load.");
            ok = false;
        }
    }
    free(data); free(backup);
    if (ok) { audio_clear(); memset(held, 0, sizeof(held)); }
    message("Load %s: %s\n", label, ok ? "loaded" : "rejected (ROM, core, format or checksum mismatch)");
    return ok;
}

static struct retro_game_info_ext content = {
    .full_path = ROM_FILENAME, .dir = "", .name = GAME_ID, .ext = "nes",
    .data = embedded_rom, .size = sizeof(embedded_rom), .persistent_data = true
};
static struct { char *key, *value; } options[256];
static unsigned option_count;

static void core_log(enum retro_log_level level, const char *format, ...) {
    (void)level;
    char text[2048]; va_list args;
    va_start(args, format); vsnprintf(text, sizeof(text), format, args); va_end(args);
    message("Core: %s", text);
}

static bool environment(unsigned command, void *data) {
    switch (command) {
    case RETRO_ENVIRONMENT_GET_GAME_INFO_EXT: *(const struct retro_game_info_ext **)data = &content; return true;
    case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY: case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
        *(const char **)data = state_dir; return true;
    case RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION: *(unsigned *)data = 0; return true;
    case RETRO_ENVIRONMENT_GET_LANGUAGE: *(unsigned *)data = RETRO_LANGUAGE_ENGLISH; return true;
    case RETRO_ENVIRONMENT_GET_CAN_DUPE: *(bool *)data = true; return true;
    case RETRO_ENVIRONMENT_GET_INPUT_BITMASKS: return true;
    case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: *(bool *)data = false; return true;
    case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: return *(enum retro_pixel_format *)data == RETRO_PIXEL_FORMAT_XRGB8888;
    case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: ((struct retro_log_callback *)data)->log = core_log; return true;
    case RETRO_ENVIRONMENT_SET_VARIABLES: {
        const struct retro_variable *v = data;
        for (unsigned i = 0; i < option_count; ++i) { free(options[i].key); free(options[i].value); }
        option_count = 0;
        for (; v->key && option_count < 256; ++v) {
            const char *start = strchr(v->value, ';');
            if (!start) continue;
            start++; while (*start == ' ') ++start;
            options[option_count].key = strdup(v->key);
            options[option_count].value = strndup(start, strcspn(start, "|"));
            if (!options[option_count].key || !options[option_count].value) fatal("Cannot allocate core options.");
            ++option_count;
        }
        return true;
    }
    case RETRO_ENVIRONMENT_GET_VARIABLE: {
        struct retro_variable *v = data;
        if (!strcmp(v->key, "fceumm_region")) v->value = GAME_REGION;
        else if (!strcmp(v->key, "fceumm_sndrate_hint")) v->value = "48KHz";
        else {
            v->value = NULL;
            for (unsigned i = 0; i < option_count; ++i) if (!strcmp(options[i].key, v->key)) { v->value = options[i].value; break; }
        }
        return v->value != NULL;
    }
    case RETRO_ENVIRONMENT_SET_MESSAGE: message("Core: %s\n", ((struct retro_message *)data)->msg); return true;
    case RETRO_ENVIRONMENT_SHUTDOWN: stopping = 1; return true;
    case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS: case RETRO_ENVIRONMENT_SET_CONTROLLER_INFO:
    case RETRO_ENVIRONMENT_SET_MEMORY_MAPS: case RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL:
    case RETRO_ENVIRONMENT_SET_GEOMETRY: return true;
    default: return false;
    }
}

static void video_frame(const void *data, unsigned width, unsigned height, size_t pitch) {
    if (!data) return;
    if (!width || !height || width > 256 || height > 240) fatal("Unexpected emulator video dimensions.");
    video_width = width; video_height = height;
    for (unsigned y = 0; y < height; ++y) memcpy(video + y * width, (const uint8_t *)data + y * pitch, width * 4);
}
static size_t samples(const int16_t *data, size_t count) { audio_push(data, count, volume); audio_frames += count; return count; }
static void sample(int16_t left, int16_t right) { int16_t pair[] = {left, right}; samples(pair, 1); }
static void input_poll(void) {}
static int16_t input(unsigned port, unsigned device, unsigned index, unsigned id) {
    if (port || device != RETRO_DEVICE_JOYPAD || index) return 0;
    uint16_t mask = 0;
    for (unsigned code = 0; code < KEY_COUNT; ++code) if (held[code])
        for (unsigned i = 0; i < 8; ++i) if (held_symbol[code] == bindings[i]) mask |= 1u << button_ids[i];
    return id == RETRO_DEVICE_ID_JOYPAD_MASK ? (int16_t)mask : id < 16 && (mask & (1u << id)) ? 1 : 0;
}

static void title(const char *notice) {
    char text[512];
    snprintf(text, sizeof(text), "%s | %s | slot %u | %s", GAME_TITLE, paused ? "Paused: Space resumes" : "Playing", slot,
             notice ? notice : "F1 help, F5 save, F8 load, F11 fullscreen");
    window_title(text);
}
static void controls_help(void) {
    char text[384]; size_t length = 0;
    for (unsigned i = 0; i < 8; ++i)
        length += snprintf(text + length, sizeof(text) - length, "%s=%s ", button_names[i], key_name(bindings[i]));
    snprintf(text + length, sizeof(text) - length, "| Space pause; F2 reset; [/] slot");
    title(text);
}
static unsigned slot_number(const char *text) {
    if (strlen(text) != 1 || *text < '0' || *text > '9') fatal("Save slot must be 0 through 9.");
    return *text - '0';
}

int main(int argc, char **argv) {
    bool config = false, changed = false, recover = false;
    double seconds = 0;
    int load_on_start = -1, save_on_exit = -1;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--version")) { printf("%s %s\nFCEUmm %s\nROM SHA-256 %s\n", GAME_ID, BUILD_ID, CORE_REVISION, ROM_SHA256); return 0; }
        if (!strcmp(argv[i], "--help")) {
            puts("Usage: " GAME_ID " [--mute] [--paused] [--seconds N] [--load-slot N] [--save-slot N] [--recover]\n"
                 "       " GAME_ID " --configure [--set a=x] [--set volume=70]\n"
                 "Display: NES_DISPLAY=auto|x11|wayland (auto prefers WAYLAND_DISPLAY).\n"
                 "Default controls: WASD = D-pad; J = A; K = B; Enter = Start; Tab = Select.\n"
                 "Space pause/resume, F2 reset, F5 save, F8 load, [/] slot, F11 fullscreen, Escape quit.\n"
                 "Settings support a,b,select,start,up,down,left,right,volume (0-100),fullscreen (0/1).\n"
                 "Keys: single characters, Return, Tab, Left, Right, Up, Down, or hexadecimal X11 keysyms.\n"
                 "Shortcut keys are reserved. State: NES_STATE_DIR/<game id>/<ROM hash> (see game.log).\n"
                 "Existing emulator states are not imported; keep their original emulator.");
            return 0;
        }
    }
    int lock = open_state();
    config_load();
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--configure")) config = true;
        else if (!strcmp(argv[i], "--recover")) recover = true;
        else if (!strcmp(argv[i], "--mute")) muted = true;
        else if (!strcmp(argv[i], "--paused")) paused = true;
        else if (!strcmp(argv[i], "--set") && i + 1 < argc) {
            if (!setting(argv[++i])) fatal("Invalid setting; use --help.");
            changed = true;
        } else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) {
            char *end; seconds = strtod(argv[++i], &end);
            if (*end || !isfinite(seconds) || seconds <= 0 || seconds > 86400) fatal("Invalid run duration.");
        } else if (!strcmp(argv[i], "--load-slot") && i + 1 < argc) {
            slot = slot_number(argv[++i]); load_on_start = (int)slot;
        }
        else if (!strcmp(argv[i], "--save-slot") && i + 1 < argc) save_on_exit = slot_number(argv[++i]);
        else fatal("Unknown or incomplete argument; use --help.");
    }
    if (recover && load_on_start >= 0) fatal("Choose either --recover or --load-slot.");
    if (changed && !config_save()) fatal("Cannot save settings.");
    if (config) { configure(!changed && isatty(STDIN_FILENO)); fclose(log_file); close(lock); return 0; }
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    retro_set_environment(environment); retro_set_video_refresh(video_frame);
    retro_set_audio_sample(sample); retro_set_audio_sample_batch(samples);
    retro_set_input_poll(input_poll); retro_set_input_state(input);
    if (retro_api_version() != RETRO_API_VERSION) fatal("Unsupported libretro API version.");
    retro_init();
    struct retro_game_info game = {ROM_FILENAME, embedded_rom, sizeof(embedded_rom), NULL};
    if (!retro_load_game(&game)) fatal("Core could not load embedded ROM.");
    core_live = true;
    battery_open();
    retro_set_controller_port_device(0, RETRO_DEVICE_JOYPAD);
    retro_set_controller_port_device(1, RETRO_DEVICE_NONE);
    struct retro_system_av_info av; retro_get_system_av_info(&av);
    if (!isfinite(av.timing.fps) || av.timing.fps < 40 || av.timing.fps > 70 || av.timing.sample_rate != 48000)
        fatal("Unexpected core timing or sample rate.");
    message("Core timing: %.9f fps, %.0f Hz\n", av.timing.fps, av.timing.sample_rate);
    if ((recover || load_on_start >= 0) && !load_state(recover ? -1 : load_on_start)) fatal("Requested startup state could not be loaded.");
    window_open(GAME_TITLE); title(NULL);
    if (fullscreen) window_fullscreen(fullscreen);
    if (!muted) audio_open(av.timing.sample_rate);
    double start = monotime(), next_frame = start, next_draw = start;
    double interval = 1.0 / av.timing.fps;
    unsigned rendered = 0;
    while (!stopping && (!seconds || monotime() - start < seconds)) {
        struct event event;
        while ((event = window_poll()).type) {
            if (event.type == EVENT_CLOSE) stopping = 1;
            if (event.type == EVENT_FOCUS_LOST) {
                memset(pressed, 0, sizeof(pressed));
                paused = true; memset(held, 0, sizeof(held)); audio_clear(); title("Focus lost; Space resumes");
                message("Focus lost: paused and released keys.\n");
            }
            if (event.type != EVENT_KEY || event.code >= KEY_COUNT) continue;
            bool repeat = pressed[event.code];
            pressed[event.code] = event.down;
            held[event.code] = event.down; held_symbol[event.code] = event.key;
            if (!event.down || repeat) continue;
            message("Key: 0x%x\n", event.key);
            switch (event.key) {
            case 0xff1b: stopping = 1; break;
            case 0x20: paused = !paused; memset(held, 0, sizeof(held)); audio_clear(); next_frame = monotime(); title(NULL); break;
            case 0xffbe: controls_help(); break;
            case 0xffbf: retro_reset(); audio_clear(); memset(held, 0, sizeof(held)); title("Reset"); break;
            case 0xffc2: title(save_state(slot) ? "Saved" : "Save failed: see game.log"); break;
            case 0xffc5: title(load_state(slot) ? "Loaded" : "Load failed: see game.log"); next_frame = monotime(); break;
            case '[': slot = (slot + 9) % 10; title(NULL); break;
            case ']': slot = (slot + 1) % 10; title(NULL); break;
            case 0xffc8: fullscreen = !fullscreen; window_fullscreen(fullscreen); if (!config_save()) message("Fullscreen preference could not be saved.\n"); break;
            default: break;
            }
        }
        if (!audio_check()) {
            paused = true; memset(held, 0, sizeof(held));
            title("Audio unavailable; F5 saves, Space resumes silently");
        }
        double current = monotime();
        if (battery_ready && current >= next_battery_save) {
            if (!battery_save()) {
                paused = true; memset(held, 0, sizeof(held));
                title("Battery save failed; see game.log");
            }
            next_battery_save = current + 5;
        }
        if (paused) next_frame = current;
        unsigned catchup = 0;
        while (!paused && current >= next_frame && catchup++ < 8) {
            retro_run(); ++frames; next_frame += interval;
        }
        if (!paused && current - next_frame > 0.25) { message("Emulation fell behind; resynchronizing.\n"); next_frame = current; audio_clear(); }
        if (current >= next_draw) {
            window_draw(video, video_width, video_height); ++rendered;
            next_draw = current + (paused ? 0.1 : interval);
        }
        struct timespec delay = {0, 1000000}; nanosleep(&delay, NULL);
    }
    bool saved = save_on_exit < 0 || save_state(save_on_exit);
    if (!battery_save()) saved = false;
    audio_close(); window_close();
    core_live = false;
    free(battery_previous);
    retro_unload_game(); retro_deinit();
    for (unsigned i = 0; i < option_count; ++i) { free(options[i].key); free(options[i].value); }
    message("Finished: emulated=%llu rendered=%u audio_frames=%llu elapsed=%.3f\n",
        (unsigned long long)frames, rendered, (unsigned long long)audio_frames, monotime() - start);
    fclose(log_file); close(lock);
    return saved ? 0 : 1;
}
