/* cc -O2 dlsym-miss.c -o dlsym-miss [-ldl on Linux]
 * ./dlsym-miss hit|miss|unique [count]
 * No Lean, artifact files, allocator replacement, or tracing required. */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach_time.h>
#endif
static double seconds(struct timespec t) { return t.tv_sec+t.tv_nsec/1e9; }
static double cpu(struct rusage r) { return r.ru_utime.tv_sec+r.ru_utime.tv_usec/1e6+r.ru_stime.tv_sec+r.ru_stime.tv_usec/1e6; }
int main(int argc,char **argv) {
    if (argc<2 || argc>3 || (strcmp(argv[1],"hit") && strcmp(argv[1],"miss") && strcmp(argv[1],"unique"))) return 2;
    unsigned n=argc==3 ? (unsigned)strtoul(argv[2],NULL,10) : 77742,found=0;
    struct timespec begin,end;struct rusage before,after;
    /* Warm the dlsym and diagnostic paths before the measured loop. */
    (void)dlsym(RTLD_DEFAULT,"malloc");(void)dlsym(RTLD_DEFAULT,"lean_mmap_repro_missing_warmup");
    char name[96];int hit=!strcmp(argv[1],"hit"),unique=!strcmp(argv[1],"unique");
    getrusage(RUSAGE_SELF,&before);clock_gettime(CLOCK_MONOTONIC,&begin);
#ifdef __APPLE__
    unsigned long long trace_begin=mach_absolute_time();
#else
    unsigned long long trace_begin=(unsigned long long)begin.tv_sec*1000000000+begin.tv_nsec;
#endif
    for (unsigned i=0;i<n;i++) {
        if (unique) snprintf(name,sizeof(name),"lean_mmap_repro_missing_%u",i);
        found+=dlsym(RTLD_DEFAULT,hit ? "malloc" : unique ? name : "lean_mmap_repro_missing_same")!=NULL;
    }
#ifdef __APPLE__
    unsigned long long trace_end=mach_absolute_time();
#else
    struct timespec trace;clock_gettime(CLOCK_MONOTONIC,&trace);
    unsigned long long trace_end=(unsigned long long)trace.tv_sec*1000000000+trace.tv_nsec;
#endif
    clock_gettime(CLOCK_MONOTONIC,&end);getrusage(RUSAGE_SELF,&after);
    printf("{\"pid\":%d,\"mode\":\"%s\",\"count\":%u,\"found\":%u,\"wall_seconds\":%.9f,\"cpu_seconds\":%.9f,\"minor_faults\":%ld,\"major_faults\":%ld,\"trace_begin\":%llu,\"trace_end\":%llu}\n",getpid(),argv[1],n,found,seconds(end)-seconds(begin),cpu(after)-cpu(before),after.ru_minflt-before.ru_minflt,after.ru_majflt-before.ru_majflt,trace_begin,trace_end);
    return found==(hit ? n : 0) ? 0 : 1;
}
