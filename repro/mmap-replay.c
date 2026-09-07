/* Replay Lean compacted-file mappings, then revisit pages without Lean.
 * cc -O2 -g -std=c11 -Wall -Wextra -Werror repro/mmap-replay.c -o results/mmap-replay
 * Input: one artifact path per line, in observed mmap order (duplicates retained).
 * Only 64-bit little-endian hosts and Lean v2/v3 compacted headers are supported.
 * Accesses are synthetic; pointer relocation and Lean heap activity are omitted.
 */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#if defined(__linux__) && !defined(MAP_FIXED_NOREPLACE)
#define MAP_FIXED_NOREPLACE 0x100000
#endif
#ifdef __APPLE__
typedef char residency_byte;
#else
typedef unsigned char residency_byte;
#endif

typedef struct {
    char *path;
    unsigned char *data;
    size_t size, pages;
    int mapped, have_previous;
    residency_byte *previous;
} region;

typedef struct {
    struct timespec time;
    struct rusage usage;
} stamp;

static size_t page_size;
static uint64_t checksum;

static void die(const char *what) {
    perror(what);
    exit(1);
}

static void fail(const char *what) {
    fprintf(stderr, "%s\n", what);
    exit(1);
}

static void *allocate(size_t bytes) {
    void *p = malloc(bytes);
    if (!p) die("malloc");
    return p;
}

static stamp now(void) {
    stamp s;
    if (clock_gettime(CLOCK_MONOTONIC, &s.time)) die("clock_gettime");
    if (getrusage(RUSAGE_SELF, &s.usage)) die("getrusage");
    return s;
}

static double cpu(struct timeval t) { return t.tv_sec + t.tv_usec / 1e6; }

static void report(const char *phase, int cycle, int pass, stamp a, stamp b) {
    printf("phase,%s,%d,%d,%.9f,%.9f,%.9f,%ld,%ld,%" PRIu64 "\n",
           phase, cycle, pass,
           (double)(b.time.tv_sec - a.time.tv_sec) +
               (b.time.tv_nsec - a.time.tv_nsec) / 1e9,
           cpu(b.usage.ru_utime) - cpu(a.usage.ru_utime),
           cpu(b.usage.ru_stime) - cpu(a.usage.ru_stime),
           b.usage.ru_minflt - a.usage.ru_minflt,
           b.usage.ru_majflt - a.usage.ru_majflt, checksum);
    fflush(stdout);
}

static void read_exact(int fd, void *buffer, size_t bytes) {
    unsigned char *p = buffer;
    while (bytes) {
        /* Keep each request within ssize_t and platform read limits. */
        size_t chunk = bytes > 1048576 ? 1048576 : bytes;
        ssize_t n = read(fd, p, chunk);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) die("read");
        if (!n) fail("unexpected EOF");
        p += n;
        bytes -= (size_t)n;
    }
}

/* Same header-read, exact-address check, and copy fallback as Lean. */
static int load(region *r, int saved) {
    int fd = open(r->path, O_RDONLY);
    if (fd < 0) die(r->path);
    struct stat st;
    if (fstat(fd, &st)) die("fstat");
    if (!S_ISREG(st.st_mode) || st.st_size < 88 ||
        (uintmax_t)st.st_size > SIZE_MAX) fail("invalid artifact size/type");
    unsigned char header[88];
    read_exact(fd, header, sizeof(header));
    if (memcmp(header, "olean", 5) || (header[5] != 2 && header[5] != 3))
        fail("expected Lean v2/v3 compacted header");
    uintptr_t base;
    memcpy(&base, header + 80, sizeof(base));
    r->size = (size_t)st.st_size;
    r->pages = r->size / page_size + (r->size % page_size != 0);
    int flags = MAP_PRIVATE;
#ifdef MAP_FIXED_NOREPLACE
    if (saved) flags |= MAP_FIXED_NOREPLACE;
#endif
    void *hint = saved ? (void *)base : NULL;
    void *p = mmap(hint, r->size, PROT_READ | PROT_WRITE, flags, fd, 0);
    /* Outcome: 0 accepted mapping, 1 failed syscall, 2 wrong address. */
    int outcome = p == MAP_FAILED ? 1 : (saved && p != hint ? 2 : 0);
    r->mapped = outcome == 0;
    if (!r->mapped) {
        if (p != MAP_FAILED && munmap(p, r->size)) die("munmap rejected address");
        p = allocate(r->size);
        if (lseek(fd, 0, SEEK_SET) < 0) die("lseek");
        read_exact(fd, p, r->size);
    }
    r->data = p;
    if (close(fd)) die("close");
    return outcome;
}

static void touch(region *r, int write_pages) {
    volatile unsigned char *p = r->data;
    for (size_t i = 0; i < r->size;) {
        unsigned char v = p[i];
        checksum += v;
        if (write_pages) p[i] = v; /* A real private write, retaining file contents. */
        if (r->size - i <= page_size) break;
        i += page_size;
    }
}

static void residency(region *rs, size_t count, residency_byte *scratch,
                      int cycle, int pass) {
    uint64_t pages = 0, resident = 0, lost = 0, gained = 0;
    stamp a = now();
    for (size_t i = 0; i < count; i++) {
        region *r = &rs[i];
        if (!r->mapped) continue; /* malloc fallback is deliberately excluded. */
        if (mincore(r->data, r->size, scratch)) die("mincore");
        pages += r->pages;
        for (size_t j = 0; j < r->pages; j++) {
            int present = scratch[j] & 1;
            int previous = r->previous[j] & 1;
            resident += present;
            if (r->have_previous) {
                lost += previous && !present;
                gained += !previous && present;
            }
            /* Retain all returned flags, although this summary uses only bit 0. */
            r->previous[j] = scratch[j];
        }
        r->have_previous = 1;
    }
    stamp b = now();
    report("probe", cycle, pass, a, b);
    printf("residency,%d,%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
           cycle, pass, pages, resident, lost, gained);
    fflush(stdout);
}

static int positive(const char *s) {
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || !*s || *end || v < 1 || v > 100000) fail("invalid positive count");
    return (int)v;
}

int main(int argc, char **argv) {
    int passes = 3, cycles = 1, saved = 1, probe = 0, writes = 0;
    const char *manifest = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--passes") && i + 1 < argc) passes = positive(argv[++i]);
        else if (!strcmp(argv[i], "--cycles") && i + 1 < argc) cycles = positive(argv[++i]);
        else if (!strcmp(argv[i], "--any-address")) saved = 0;
        else if (!strcmp(argv[i], "--residency")) probe = 1;
        else if (!strcmp(argv[i], "--write")) writes = 1;
        else if (argv[i][0] != '-' && !manifest) manifest = argv[i];
        else fail("usage: mmap-replay [--passes N] [--cycles N] [--any-address] "
                  "[--residency] [--write] FILES.txt");
    }
    if (!manifest) fail("missing FILES.txt (one compacted artifact path per line)");
    uint16_t endian = 1;
    if (sizeof(uintptr_t) != 8 || *(unsigned char *)&endian != 1)
        fail("requires a 64-bit little-endian host");
    long ps = sysconf(_SC_PAGESIZE);
    if (ps <= 0) fail("cannot determine page size");
    page_size = (size_t)ps;
    FILE *f = fopen(manifest, "r");
    if (!f) die(manifest);
    region *rs = NULL;
    size_t count = 0, capacity = 0, line_capacity = 0;
    char *line = NULL;
    ssize_t length;
    while ((length = getline(&line, &line_capacity, f)) >= 0) {
        if (length && line[length - 1] == '\n') line[--length] = 0;
        if (!length || memchr(line, 0, (size_t)length)) fail("empty/NUL manifest path");
        if (count == capacity) {
            size_t next = capacity ? capacity * 2 : 256;
            if (next < capacity || next > SIZE_MAX / sizeof(*rs)) fail("manifest too large");
            region *p = realloc(rs, next * sizeof(*rs));
            if (!p) die("realloc");
            rs = p;
            capacity = next;
        }
        rs[count] = (region){.path = strdup(line)};
        if (!rs[count].path) die("strdup");
        count++;
    }
    if (ferror(f)) die("read manifest");
    if (fclose(f)) die("close manifest");
    free(line);
    if (!count) fail("empty manifest");
    printf("config,page_size,%zu,files,%zu,passes,%d,cycles,%d,saved_address,%d,write,%d,probe,%d\n",
           page_size, count, passes, cycles, saved, writes, probe);
    puts("# phase,name,cycle,pass,wall_s,user_s,system_s,minor_faults,major_faults,checksum");
    puts("# mappings,cycle,mapped,syscall_failed,wrong_address,total_bytes,fallback_bytes");
    puts("# residency,cycle,pass,mapped_pages,resident,lost_since_probe,gained_since_probe");
    fflush(stdout);
    for (int c = 1; c <= cycles; c++) {
        size_t outcomes[3] = {0}, max_pages = 0;
        uint64_t bytes = 0, fallback_bytes = 0;
        stamp a = now();
        for (size_t i = 0; i < count; i++) {
            int outcome = load(&rs[i], saved);
            outcomes[outcome]++;
            bytes += rs[i].size;
            if (outcome) fallback_bytes += rs[i].size;
            if (rs[i].pages > max_pages) max_pages = rs[i].pages;
        }
        stamp b = now();
        report("map", c, 0, a, b);
        printf("mappings,%d,%zu,%zu,%zu,%" PRIu64 ",%" PRIu64 "\n",
               c, outcomes[0], outcomes[1], outcomes[2], bytes, fallback_bytes);
        residency_byte *scratch = NULL;
        if (probe) {
            scratch = allocate(max_pages);
            for (size_t i = 0; i < count; i++) {
                rs[i].have_previous = 0;
                if (rs[i].mapped) {
                    rs[i].previous = allocate(rs[i].pages);
                    memset(rs[i].previous, 0, rs[i].pages);
                }
            }
            residency(rs, count, scratch, c, 0);
        }
        for (int p = 1; p <= passes; p++) {
            a = now();
            for (size_t i = 0; i < count; i++) touch(&rs[i], writes);
            b = now();
            report("touch", c, p, a, b);
            if (probe) residency(rs, count, scratch, c, p);
        }
        a = now();
        for (size_t i = 0; i < count; i++) {
            if (rs[i].mapped) {
                if (munmap(rs[i].data, rs[i].size)) die("munmap");
            } else free(rs[i].data);
        }
        b = now();
        report("unmap", c, 0, a, b);
        for (size_t i = 0; i < count; i++) {
            free(rs[i].previous);
            rs[i].previous = NULL;
        }
        free(scratch);
    }
    for (size_t i = 0; i < count; i++) free(rs[i].path);
    free(rs);
    return 0;
}
