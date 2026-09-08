#define _DARWIN_C_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <sys/mman.h>
#include <unistd.h>
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
    printf("{\"address\":%llu,\"size\":%zu,\"release\":%d,\"reuse\":%d,\"dontneed\":%d,\"free\":%d}\n",(unsigned long long)(uintptr_t)a,n,MADV_FREE_REUSABLE,MADV_FREE_REUSE,MADV_DONTNEED,MADV_FREE);
    return 0;
}
