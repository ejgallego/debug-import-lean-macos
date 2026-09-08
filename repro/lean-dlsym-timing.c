/* Called only at Lean's RTLD_DEFAULT lookup site, preserving dlsym semantics. */
#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach_time.h>
#endif
#define CAPACITY 300000
struct event { uint64_t begin,end; int found; };
static struct event events[CAPACITY];
static _Atomic unsigned used;
static const char *output;
static uint64_t ticks(void) {
#ifdef __APPLE__
    return mach_absolute_time();
#else
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;
#endif
}
__attribute__((constructor)) static void setup(void) { output=getenv("LEAN_DLSYM_OUTPUT"); }
void *lean_observe_dlsym(const char *symbol) {
    if (!output) return dlsym(RTLD_DEFAULT,symbol);
    uint64_t begin=ticks();void *result=dlsym(RTLD_DEFAULT,symbol);uint64_t end=ticks();
    unsigned i=atomic_fetch_add(&used,1);
    if (i<CAPACITY) events[i]=(struct event){begin,end,result!=NULL};
    return result;
}
__attribute__((destructor)) static void report(void) {
    if (!output) return;
    FILE *f=fopen(output,"w");if (!f) { perror(output);_Exit(1); }
    unsigned n=atomic_load(&used),numer=1,denom=1;
#ifdef __APPLE__
    mach_timebase_info_data_t tb;mach_timebase_info(&tb);numer=tb.numer;denom=tb.denom;
#endif
    fprintf(f,"{\"pid\":%d,\"overflow\":%s,\"timebase_numer\":%u,\"timebase_denom\":%u,\"events\":[",getpid(),n>CAPACITY ? "true" : "false",numer,denom);
    for (unsigned i=0;i<n && i<CAPACITY;i++) fprintf(f,"%s[%llu,%llu,%d]",i ? "," : "",(unsigned long long)events[i].begin,(unsigned long long)events[i].end,events[i].found);
    fputs("]}\n",f);if (fclose(f)) _Exit(1);
}
