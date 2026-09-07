/* No Lean, artifact data, or page accesses: map one page many times.
 * Usage: mmap-hints COUNT SPACING_BYTES any|ascending|descending|shuffled file|anon
 * cc -O2 -g -std=c11 -Wall -Wextra -Werror mmap-hints.c -o mmap-hints
 * All modes use ordinary non-destructive hints (no MAP_FIXED flags).
 */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

typedef struct { struct timespec clock; struct rusage usage; } stamp;

static void die(const char *s) { perror(s); exit(1); }
static void fail(const char *s) { fprintf(stderr, "%s\n", s); exit(1); }
static stamp now(void) {
    stamp s;
    if (clock_gettime(CLOCK_MONOTONIC, &s.clock)) die("clock_gettime");
    if (getrusage(RUSAGE_SELF, &s.usage)) die("getrusage");
    return s;
}
static double wall(stamp a, stamp b) {
    return (b.clock.tv_sec - a.clock.tv_sec) + (b.clock.tv_nsec - a.clock.tv_nsec) / 1e9;
}
static double cpu(struct timeval t) { return t.tv_sec + t.tv_usec / 1e6; }
static void phase(const char *name, stamp a, stamp b) {
    printf("phase,%s,%.9f,%.9f,%.9f,%ld,%ld\n", name, wall(a,b),
           cpu(b.usage.ru_utime) - cpu(a.usage.ru_utime),
           cpu(b.usage.ru_stime) - cpu(a.usage.ru_stime),
           b.usage.ru_minflt - a.usage.ru_minflt, b.usage.ru_majflt - a.usage.ru_majflt);
}
static size_t number(const char *s) {
    char *end;
    errno = 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno || !*s || *s == '-' || *end || !v || v > SIZE_MAX) fail("invalid number");
    return (size_t)v;
}
static uint64_t random_step(uint64_t *state) {
    /* Fixed seed and portable shuffle for reproducible insertion order. */
    uint64_t x = *state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    *state = x;
    return x * UINT64_C(2685821657736338717);
}

int main(int argc, char **argv) {
    if (argc != 5) fail("usage: mmap-hints COUNT SPACING_BYTES "
                       "any|ascending|descending|shuffled file|anon");
    if (sizeof(uintptr_t) != 8) fail("requires a 64-bit host");
    size_t count = number(argv[1]), spacing = number(argv[2]);
    long ps = sysconf(_SC_PAGESIZE);
    if (ps <= 0) fail("invalid page size");
    size_t size = (size_t)ps;
    uintptr_t base = UINT64_C(0x10000000000); /* 1 TiB */
    if (count > 100000 || spacing < size || spacing % size ||
        spacing > (UINT64_C(0x7f0000000000) - base) / count)
        fail("invalid count/spacing/range");
    int any = !strcmp(argv[3], "any"), descending = !strcmp(argv[3], "descending");
    int shuffled = !strcmp(argv[3], "shuffled");
    if (!any && !descending && !shuffled && strcmp(argv[3], "ascending")) fail("invalid order");
    int anonymous = !strcmp(argv[4], "anon");
    if (!anonymous && strcmp(argv[4], "file")) fail("invalid backing");
    void **addresses = calloc(count, sizeof(*addresses));
    size_t *order = malloc(count * sizeof(*order));
    /* Only one checkpoint per 1024 calls, written out after mapping. */
    size_t blocks = (count + 1023) / 1024;
    stamp *checkpoints = calloc(blocks, sizeof(*checkpoints));
    if (!addresses || !order || !checkpoints) die("allocate metadata");
    for (size_t i = 0; i < count; i++) {
        addresses[i] = MAP_FAILED; /* pre-touch storage before timing */
        order[i] = descending ? count - i - 1 : i;
    }
    uint64_t state = 42;
    if (shuffled) for (size_t i = count - 1; i; i--) {
        size_t j = random_step(&state) % (i + 1);
        size_t tmp = order[i]; order[i] = order[j]; order[j] = tmp;
    }
    for (size_t i = 0; i < blocks; i++) checkpoints[i] = now();
    FILE *file = NULL;
    int fd = -1, flags = MAP_PRIVATE;
    if (anonymous) flags |= MAP_ANON;
    else {
        file = tmpfile(); /* one unlinked file, one offset, no reads or writes */
        if (!file) die("tmpfile");
        fd = fileno(file);
        if (ftruncate(fd, (off_t)size)) die("ftruncate");
    }
    printf("config,count,%zu,page_size,%zu,spacing,%zu,order,%s,backing,%s,base,0x%" PRIxPTR ",seed,42\n",
           count, size, spacing, argv[3], argv[4], base);
    puts("# phase,name,wall_s,user_s,system_s,minor_faults,major_faults");
    puts("# batch,attempts,cumulative_map_s,batch_map_s");
    puts("# outcomes,mapped,exact_hint,wrong_hint,failed,first_errno");
    fflush(stdout);
    size_t mapped = 0, exact = 0, wrong = 0, failed = 0, checkpoint = 0;
    int first_errno = 0;
    stamp a = now();
    for (size_t i = 0; i < count; i++) {
        void *hint = any ? NULL : (void *)(base + order[i] * spacing);
        void *p = mmap(hint, size, PROT_READ | PROT_WRITE, flags, fd, 0);
        addresses[i] = p;
        if (p == MAP_FAILED) { failed++; if (!first_errno) first_errno = errno; }
        else { mapped++; if (!any) { if (p == hint) exact++; else wrong++; } }
        if ((i + 1) % 1024 == 0 || i + 1 == count) checkpoints[checkpoint++] = now();
    }
    stamp b = now();
    phase("map", a, b);
    printf("outcomes,%zu,%zu,%zu,%zu,%d\n", mapped, exact, wrong, failed, first_errno);
    stamp previous = a;
    for (size_t i = 0; i < blocks; i++) {
        size_t attempts = (i + 1) * 1024;
        if (attempts > count) attempts = count;
        printf("batch,%zu,%.9f,%.9f\n", attempts, wall(a, checkpoints[i]), wall(previous, checkpoints[i]));
        previous = checkpoints[i];
    }
    fflush(stdout);
    a = now();
    for (size_t i = 0; i < count; i++)
        if (addresses[i] != MAP_FAILED && munmap(addresses[i], size)) die("munmap");
    b = now();
    phase("unmap", a, b);
    if (file && fclose(file)) die("fclose");
    free(checkpoints); free(order); free(addresses);
    /* Wrong hints change the workload: retain results, but flag the run invalid. */
    return failed || wrong ? 2 : 0;
}
