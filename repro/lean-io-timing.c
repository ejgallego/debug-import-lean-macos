/* Passive artifact I/O timing. No mapping policy or file contents are changed.
 * macOS: cc -O2 -g -std=c11 -Wall -Wextra -Werror -dynamiclib this.c -o timing.dylib
 * Linux smoke: cc -O2 -g -std=c11 -Wall -Wextra -Werror -shared -fPIC this.c -ldl -o timing.so
 */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#ifndef __APPLE__
#include <dlfcn.h>
#include <sys/syscall.h>
#endif

enum { OPEN_N, OPEN_NS, HEADER_N, HEADER_NS, HEADER_BYTES, MAP_N, MAP_NS,
       MAP_BYTES, MAP_FAILED_N, MAP_WRONG_N, FALLBACK_N, COPY_N, COPY_NS,
       COPY_BYTES, STAT_N, STAT_NS, CLOSE_N, CLOSE_NS, UNTRACKED_FD, METRICS };
/* Public, fixed uint64 layout for debugger snapshots, independent of DWARF. */
_Atomic uint64_t lean_io_totals[METRICS];
static struct { uint64_t start, end, clock; } maps[50000];
static _Atomic unsigned map_used;
static int map_enabled;
__attribute__((constructor)) static void map_setup(void) { map_enabled=getenv("LEAN_FAULT_MAPS")!=NULL; }
static _Atomic unsigned char tracked[4096]; /* 1: artifact, 2: seek-to-zero fallback */
static const char *names[METRICS] = {
    "open_count", "open_ns", "header_read_count", "header_read_ns", "header_read_bytes",
    "mmap_count", "mmap_ns", "mmap_bytes", "mmap_failed", "mmap_wrong_address",
    "fallback_count", "copy_read_count", "copy_read_ns", "copy_read_bytes",
    "fstat_count", "fstat_ns", "close_count", "close_ns", "untracked_fd_count"
};
static void add(int i, uint64_t n) { atomic_fetch_add_explicit(&lean_io_totals[i], n, memory_order_relaxed); }
static uint64_t tick(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000 + (uint64_t)t.tv_nsec;
}
static unsigned state(int fd) {
    return fd >= 0 && fd < 4096 ? atomic_load_explicit(&tracked[fd], memory_order_relaxed) : 0;
}
static int artifact(const char *path) {
    const char *suffixes[] = {".olean", ".olean.server", ".olean.private", ".ir", ".ir.sig", ".ir.private"};
    size_t n = strlen(path);
    for (size_t i=0; i<sizeof(suffixes)/sizeof(suffixes[0]); i++) {
        size_t k = strlen(suffixes[i]);
        if (n >= k && !memcmp(path+n-k, suffixes[i], k)) return 1;
    }
    return 0;
}
#ifdef __APPLE__
#define REAL(name) name
#define WRAP(name) trace_##name
#define INTERPOSE(name) \
__attribute__((used)) static struct { const void *replacement; const void *original; } \
interpose_##name __attribute__((section("__DATA,__interpose"))) = { (const void *)trace_##name, (const void *)name }
#else
/* Bootstrap only: Lean's allocator can mmap before preload constructors.
 * Once resolved, forward to the original libc entry points, like dyld on Mac. */
static int real_open(const char *p,int f,...) {
    va_list ap; va_start(ap,f); mode_t m=(mode_t)va_arg(ap,int); va_end(ap);
    return (int)syscall(SYS_openat,AT_FDCWD,p,f,m);
}
static ssize_t real_read(int f,void *b,size_t n) { return syscall(SYS_read,f,b,n); }
static void *real_mmap(void *a,size_t n,int p,int f,int d,off_t o) { return (void *)syscall(SYS_mmap,a,n,p,f,d,o); }
static off_t real_lseek(int f,off_t o,int w) { return syscall(SYS_lseek,f,o,w); }
static int real_fstat(int f,struct stat *s) { return (int)syscall(SYS_fstat,f,s); }
static int real_close(int f) { return (int)syscall(SYS_close,f); }
static int (*libc_open)(const char *, int, ...);
static ssize_t (*libc_read)(int, void *, size_t);
static void *(*libc_mmap)(void *, size_t, int, int, int, off_t);
static off_t (*libc_lseek)(int, off_t, int);
static int (*libc_fstat)(int, struct stat *);
static int (*libc_close)(int);
static int (*libc_fxstat)(int, int, struct stat *);
static _Atomic int resolved;
__attribute__((constructor)) static void resolve_libc(void) {
#define RESOLVE(name) do { *(void **)(&libc_##name) = dlsym(RTLD_NEXT, #name); if (!libc_##name) _exit(125); } while (0)
    RESOLVE(open); RESOLVE(read); RESOLVE(mmap); RESOLVE(lseek); RESOLVE(fstat); RESOLVE(close);
    *(void **)(&libc_fxstat) = dlsym(RTLD_NEXT, "__fxstat");
    atomic_store(&resolved, 1);
#undef RESOLVE
}
#define REAL(name) (atomic_load(&resolved) ? libc_##name : real_##name)
#define WRAP(name) name
#define INTERPOSE(name)
#endif

int WRAP(open)(const char *path, int flags, ...) {
    mode_t mode=0;
    int needs_mode=flags & O_CREAT;
#ifdef O_TMPFILE
    needs_mode |= (flags & O_TMPFILE)==O_TMPFILE;
#endif
    if (needs_mode) { va_list ap; va_start(ap, flags); mode=(mode_t)va_arg(ap,int); va_end(ap); }
    int is_artifact=artifact(path);
    uint64_t a=is_artifact ? tick() : 0;
    int fd=REAL(open)(path,flags,mode), error=errno;
    if (is_artifact) {
        add(OPEN_NS,tick()-a); add(OPEN_N,1);
        if (fd>=4096) add(UNTRACKED_FD,1);
    }
    if (fd>=0 && fd<4096) atomic_store_explicit(&tracked[fd],is_artifact ? 1 : 0,memory_order_relaxed);
    errno=error;
    return fd;
}
ssize_t WRAP(read)(int fd, void *buf, size_t n) {
    unsigned s=state(fd);
    uint64_t a=s ? tick() : 0;
    ssize_t r=REAL(read)(fd,buf,n); int error=errno;
    if (s) {
        add(s==2 ? COPY_NS : HEADER_NS,tick()-a);
        add(s==2 ? COPY_N : HEADER_N,1);
        if (r>0) add(s==2 ? COPY_BYTES : HEADER_BYTES,(uint64_t)r);
    }
    errno=error;
    return r;
}
void *WRAP(mmap)(void *hint, size_t size, int prot, int flags, int fd, off_t off) {
    unsigned s=state(fd);
    uint64_t a=s ? tick() : 0;
    void *r=REAL(mmap)(hint,size,prot,flags,fd,off); int error=errno;
    if (s) {
        add(MAP_NS,tick()-a); add(MAP_N,1); add(MAP_BYTES,size);
        if (r==MAP_FAILED) add(MAP_FAILED_N,1);
        else if (hint && hint!=r) add(MAP_WRONG_N,1);
        if (map_enabled && r!=MAP_FAILED && r==hint) {
            unsigned i=atomic_fetch_add(&map_used,1);
            if (i<50000) { maps[i].start=(uintptr_t)r; maps[i].end=(uintptr_t)r+size; maps[i].clock=tick(); }
        }
    }
    errno=error;
    return r;
}
off_t WRAP(lseek)(int fd, off_t offset, int whence) {
    off_t r=REAL(lseek)(fd,offset,whence); int error=errno;
    if (state(fd) && offset==0 && whence==SEEK_SET && r==0) {
        atomic_store_explicit(&tracked[fd],2,memory_order_relaxed);
        add(FALLBACK_N,1);
    }
    errno=error;
    return r;
}
int WRAP(fstat)(int fd, struct stat *st) {
    unsigned s=state(fd); uint64_t a=s ? tick() : 0;
    int r=REAL(fstat)(fd,st), error=errno;
    if (s) { add(STAT_NS,tick()-a); add(STAT_N,1); }
    errno=error; return r;
}
#ifndef __APPLE__
/* The pinned x86 Linux Lean binary calls glibc's versioned stat ABI. */
int __fxstat(int version, int fd, struct stat *st) {
    unsigned s=state(fd); uint64_t a=s ? tick() : 0;
    int r=atomic_load(&resolved) && libc_fxstat ? libc_fxstat(version,fd,st) : real_fstat(fd,st);
    int error=errno;
    if (s) { add(STAT_NS,tick()-a); add(STAT_N,1); }
    errno=error; return r;
}
#endif
int WRAP(close)(int fd) {
    unsigned s=state(fd); uint64_t a=s ? tick() : 0;
    /* Clear before close: another thread may reuse the descriptor immediately. */
    if (fd>=0 && fd<4096) atomic_store_explicit(&tracked[fd],0,memory_order_relaxed);
    int r=REAL(close)(fd), error=errno;
    if (s) { add(CLOSE_NS,tick()-a); add(CLOSE_N,1); }
    errno=error; return r;
}
INTERPOSE(open); INTERPOSE(read); INTERPOSE(mmap); INTERPOSE(lseek); INTERPOSE(fstat); INTERPOSE(close);

__attribute__((destructor)) static void report(void) {
    const char *path=getenv("LEAN_IO_TIMING_OUTPUT");
    if (!path) return;
    FILE *f=fopen(path,"w");
    if (!f) return;
    fputs("{\n",f);
    for (int i=0;i<METRICS;i++)
        fprintf(f,"  \"%s\": %llu%s\n",names[i],(unsigned long long)atomic_load(&lean_io_totals[i]), i+1==METRICS ? "" : ",");
    fputs("}\n",f); fclose(f);
    path=getenv("LEAN_FAULT_MAPS");
    if (!path) return;
    f=fopen(path,"w"); if (!f) return;
    unsigned n=atomic_load(&map_used);
    fprintf(f,"{\"overflow\":%s,\"maps\":[",n>50000 ? "true" : "false");
    for (unsigned i=0;i<n && i<50000;i++)
        fprintf(f,"%s[%llu,%llu,%llu]",i ? "," : "",(unsigned long long)maps[i].start,(unsigned long long)maps[i].end,(unsigned long long)maps[i].clock);
    fputs("]}\n",f); fclose(f);
}
