#define _DARWIN_C_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <sys/mman.h>
#include <unistd.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
int main(void) {
    size_t n=(size_t)getpagesize()*4;
    char *a=mmap(NULL,n,PROT_NONE,MAP_PRIVATE|MAP_ANON,-1,0);
    if (a==MAP_FAILED) return 1;
    if (mprotect(a,n,PROT_READ|PROT_WRITE)) return 2;
    for (size_t i=0;i<n;i+=getpagesize()) a[i]=7;
    if (madvise(a,n,MADV_FREE_REUSABLE)) return 3;
    if (madvise(a,n,MADV_FREE_REUSE)) return 4;
    for (size_t i=0;i<n;i+=getpagesize()) a[i]=8;
    if (mprotect(a,n,PROT_NONE)) return 5;
    if (munmap(a,n)) return 6;
    mach_vm_address_t src=0,dst=0,third=0;mach_vm_size_t sz=getpagesize(),actual=0;
    if (mach_vm_allocate(mach_task_self(),&src,sz,VM_FLAGS_ANYWHERE)) return 7;
    if (mach_vm_allocate(mach_task_self(),&dst,sz,VM_FLAGS_ANYWHERE)) return 8;
    *(char *)(uintptr_t)src=42;
    if (mach_vm_copy(mach_task_self(),src,sz,dst)) return 9;
    if (mach_vm_read_overwrite(mach_task_self(),src,sz,dst,&actual) || actual!=sz) return 10;
    if (mach_vm_protect(mach_task_self(),dst,sz,FALSE,VM_PROT_READ)) return 11;
    if (mach_vm_map(mach_task_self(),&third,sz,0,VM_FLAGS_ANYWHERE,MEMORY_OBJECT_NULL,0,FALSE,VM_PROT_READ|VM_PROT_WRITE,VM_PROT_ALL,VM_INHERIT_DEFAULT)) return 12;
    if (mach_vm_deallocate(mach_task_self(),src,sz) || mach_vm_deallocate(mach_task_self(),dst,sz) || mach_vm_deallocate(mach_task_self(),third,sz)) return 13;
    printf("{\"address\":%llu,\"size\":%zu,\"release\":%d,\"reuse\":%d,\"dontneed\":%d,\"free\":%d,\"mach_source\":%llu,\"mach_dest\":%llu,\"mach_third\":%llu,\"mach_size\":%llu}\n",(unsigned long long)(uintptr_t)a,n,MADV_FREE_REUSABLE,MADV_FREE_REUSE,MADV_DONTNEED,MADV_FREE,(unsigned long long)src,(unsigned long long)dst,(unsigned long long)third,(unsigned long long)sz);
    return 0;
}
