/* macOS passive VM-operation observer. Buffer records; write at exit. */
#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <mach/mach_time.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

#define CAPACITY 300000
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
/* Mach API coverage is needed for libmalloc and VM-copy/read helpers that
 * bypass libc mmap/madvise. Only observe operations on this process. */
static kern_return_t observe_mach_vm_allocate(vm_map_t task,mach_vm_address_t *a,mach_vm_size_t n,int flags) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_allocate(task,a,n,flags);
    if (task==mach_task_self()) record(5,t,(void *)(uintptr_t)*a,n,flags,r,r,__builtin_return_address(0));return r;
}
static kern_return_t observe_mach_vm_deallocate(vm_map_t task,mach_vm_address_t a,mach_vm_size_t n) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_deallocate(task,a,n);
    if (task==mach_task_self()) record(6,t,(void *)(uintptr_t)a,n,0,r,r,__builtin_return_address(0));return r;
}
static kern_return_t observe_mach_vm_map(vm_map_t task,mach_vm_address_t *a,mach_vm_size_t n,mach_vm_offset_t mask,int flags,mem_entry_name_port_t object,memory_object_offset_t off,boolean_t copy,vm_prot_t cur,vm_prot_t max,vm_inherit_t inherit) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_map(task,a,n,mask,flags,object,off,copy,cur,max,inherit);
    if (task==mach_task_self()) record(7,t,(void *)(uintptr_t)*a,n,cur,r,r,__builtin_return_address(0));return r;
}
static kern_return_t observe_mach_vm_protect(vm_map_t task,mach_vm_address_t a,mach_vm_size_t n,boolean_t maximum,vm_prot_t prot) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_protect(task,a,n,maximum,prot);
    if (task==mach_task_self()) record(8,t,(void *)(uintptr_t)a,n,prot,r,r,__builtin_return_address(0));return r;
}
static kern_return_t observe_mach_vm_copy(vm_map_t task,mach_vm_address_t src,mach_vm_size_t n,mach_vm_address_t dst) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_copy(task,src,n,dst);
    if (task==mach_task_self()) record(9,t,(void *)(uintptr_t)src,n,0,dst,r,__builtin_return_address(0));return r;
}
static kern_return_t observe_mach_vm_read_overwrite(vm_map_t task,mach_vm_address_t src,mach_vm_size_t n,mach_vm_address_t dst,mach_vm_size_t *actual) {
    uint64_t t=mach_absolute_time();kern_return_t r=mach_vm_read_overwrite(task,src,n,dst,actual);
    if (task==mach_task_self()) record(10,t,(void *)(uintptr_t)src,n,0,dst,r,__builtin_return_address(0));return r;
}
#define INTERPOSE(name) __attribute__((used)) static struct { const void *replacement,*original; } interpose_##name __attribute__((section("__DATA,__interpose")))={ (void *)observe_##name,(void *)name }
INTERPOSE(mmap); INTERPOSE(munmap); INTERPOSE(mprotect); INTERPOSE(madvise);
INTERPOSE(mach_vm_allocate); INTERPOSE(mach_vm_deallocate); INTERPOSE(mach_vm_map); INTERPOSE(mach_vm_protect); INTERPOSE(mach_vm_copy); INTERPOSE(mach_vm_read_overwrite);
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
