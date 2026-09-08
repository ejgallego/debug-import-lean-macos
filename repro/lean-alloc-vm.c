/* macOS passive VM-operation observer. Buffer records; write at exit. */
#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

#define CAPACITY 100000
struct event { uint64_t begin,end,address,size,result,caller,thread; int kind,arg,error; };
static struct event events[CAPACITY];
static _Atomic unsigned used;
static const char *output;
__attribute__((constructor)) static void setup(void) { output=getenv("LEAN_ALLOC_VM_OUTPUT"); }
static void record(int kind,uint64_t begin,void *a,size_t size,int arg,uint64_t result,int error,void *caller) {
    if (!output) return;
    unsigned i=atomic_fetch_add(&used,1); if (i>=CAPACITY) return;
    struct event *e=&events[i];
    *e=(struct event){.begin=begin,.end=mach_absolute_time(),.address=(uintptr_t)a,.size=size,
        .result=result,.caller=(uintptr_t)caller,.kind=kind,.arg=arg,.error=error};
    pthread_threadid_np(NULL,&e->thread);
}
static void *observe_mmap(void *a,size_t n,int prot,int flags,int fd,off_t off) {
    int relevant=output && (flags & MAP_ANON); uint64_t t=relevant ? mach_absolute_time() : 0;
    void *r=mmap(a,n,prot,flags,fd,off); int err=errno;
    if (relevant) record(1,t,r,n,prot,(uintptr_t)r,r==MAP_FAILED ? err : 0,__builtin_return_address(0));
    errno=err;return r;
}
static int observe_munmap(void *a,size_t n) {
    uint64_t t=output ? mach_absolute_time() : 0; int r=munmap(a,n),err=errno;
    record(2,t,a,n,0,r,r ? err : 0,__builtin_return_address(0)); errno=err;return r;
}
static int observe_mprotect(void *a,size_t n,int prot) {
    uint64_t t=output ? mach_absolute_time() : 0; int r=mprotect(a,n,prot),err=errno;
    record(3,t,a,n,prot,r,r ? err : 0,__builtin_return_address(0)); errno=err;return r;
}
static int observe_madvise(void *a,size_t n,int advice) {
    uint64_t t=output ? mach_absolute_time() : 0; int r=madvise(a,n,advice),err=errno;
    record(4,t,a,n,advice,r,r ? err : 0,__builtin_return_address(0)); errno=err;return r;
}
#define INTERPOSE(name) __attribute__((used)) static struct { const void *replacement,*original; } interpose_##name __attribute__((section("__DATA,__interpose")))={ (void *)observe_##name,(void *)name }
INTERPOSE(mmap); INTERPOSE(munmap); INTERPOSE(mprotect); INTERPOSE(madvise);
static void quoted(FILE *f,const char *s) {
    fputc('"',f);
    if (s) for (;*s;s++) {
        unsigned char c=(unsigned char)*s;
        if (c=='"' || c=='\\') fputc('\\',f);
        if (c<32) fprintf(f,"\\u%04x",c); else fputc(c,f);
    }
    fputc('"',f);
}
__attribute__((destructor)) static void report(void) {
    if (!output) return;
    FILE *f=fopen(output,"w");if (!f) return;
    unsigned n=atomic_load(&used); mach_timebase_info_data_t tb;mach_timebase_info(&tb);
    fprintf(f,"{\"pid\":%d,\"overflow\":%s,\"timebase_numer\":%u,\"timebase_denom\":%u,\"events\":[",getpid(),n>CAPACITY ? "true" : "false",tb.numer,tb.denom);
    for (unsigned i=0;i<n && i<CAPACITY;i++) {
        struct event *e=&events[i];Dl_info info={0};dladdr((void *)(uintptr_t)e->caller,&info);
        fprintf(f,"%s{\"kind\":%d,\"begin\":%llu,\"end\":%llu,\"address\":%llu,\"size\":%llu,\"arg\":%d,\"result\":%llu,\"error\":%d,\"caller\":%llu,\"image_base\":%llu,\"thread\":%llu",i ? ",\n" : "\n",e->kind,
            (unsigned long long)e->begin,(unsigned long long)e->end,(unsigned long long)e->address,(unsigned long long)e->size,e->arg,
            (unsigned long long)e->result,e->error,(unsigned long long)e->caller,(unsigned long long)(uintptr_t)info.dli_fbase,(unsigned long long)e->thread);
        fputs(",\"image\":",f);quoted(f,info.dli_fname);fputs(",\"symbol\":",f);quoted(f,info.dli_sname);fputc('}',f);
    }
    fputs("]}\n",f);fclose(f);
}
