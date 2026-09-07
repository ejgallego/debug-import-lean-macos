/* In-process phase snapshots. Buffer in memory; emit only at process exit. */
#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE
#include <lean/lean.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <libproc.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/mach_vm.h>
#endif

#include "lean-fault-regions.h"

#define CAPACITY 8192
struct event {
    char kind;
    char label[192];
    uint64_t wall_ns, value, trace_clock;
    uint64_t disk_read, disk_write, resident, compressed, decompressions;
    int memory_valid;
    struct rusage usage;
};
static struct event events[CAPACITY];
static size_t used;
static int failed;
static const char *output;
static int memory_enabled;
static void memory_snapshot(struct event *e) {
#ifdef __APPLE__
    struct rusage_info_v4 r;
    task_vm_info_data_t v;
    mach_msg_type_number_t n=TASK_VM_INFO_COUNT;
    if (proc_pid_rusage(getpid(),RUSAGE_INFO_V4,(rusage_info_t *)&r) ||
        task_info(mach_task_self(),TASK_VM_INFO,(task_info_t)&v,&n)!=KERN_SUCCESS || n<TASK_VM_INFO_REV5_COUNT) { failed=1; return; }
    e->disk_read=r.ri_diskio_bytesread; e->disk_write=r.ri_diskio_byteswritten;
    e->resident=v.resident_size; e->compressed=v.compressed; e->decompressions=v.decompressions;
#else
    FILE *f=fopen("/proc/self/io","r");
    if (!f) { failed=1; return; }
    char key[64]; unsigned long long val; int fields=0;
    while (fscanf(f,"%63s %llu",key,&val)==2) {
        if (!strcmp(key,"read_bytes:")) { e->disk_read=val; fields++; }
        if (!strcmp(key,"write_bytes:")) { e->disk_write=val; fields++; }
    }
    fclose(f);
    f=fopen("/proc/self/statm","r"); unsigned long pages, total;
    if (!f) { failed=1; return; }
    if (fscanf(f,"%lu %lu",&total,&pages)!=2 || fields!=2) failed=1;
    else e->resident=(uint64_t)pages*(uint64_t)sysconf(_SC_PAGESIZE);
    fclose(f);
#endif
    e->memory_valid=1;
}

__attribute__((constructor)) static void setup(void) { output=getenv("LEAN_INNER_TIMING_OUTPUT"); memory_enabled=getenv("LEAN_MEMORY_TIMING")!=NULL; }
static lean_object *record(char kind, lean_object *label, uint64_t value) {
    if (output) {
        if (used==CAPACITY || strlen(lean_string_cstr(label))>=sizeof(events[0].label)) {
            failed=1;
        } else {
            struct event *e=&events[used++];
            struct timespec t;
            if (clock_gettime(CLOCK_MONOTONIC,&t) || getrusage(RUSAGE_SELF,&e->usage)) failed=1;
            e->wall_ns=(uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_nsec;
            e->kind=kind; e->value=value;
#ifdef __APPLE__
            e->trace_clock=mach_absolute_time();
#else
            e->trace_clock=e->wall_ns;
#endif
            if (memory_enabled && kind!='C' && strncmp(lean_string_cstr(label),"extension:",10)) memory_snapshot(e);
            strcpy(e->label,lean_string_cstr(label));
            if (kind=='E' && !strcmp(e->label,"finalize")) fault_regions_capture();
        }
    }
    return lean_io_result_mk_ok(lean_box(0));
}
LEAN_EXPORT lean_object *lean_import_phase_begin(lean_object *label) { return record('B',label,0); }
LEAN_EXPORT lean_object *lean_import_phase_end(lean_object *label) { return record('E',label,0); }
LEAN_EXPORT lean_object *lean_import_phase_count(lean_object *label,uint64_t count) { return record('C',label,count); }
static void quoted(FILE *f, const char *s) {
    fputc('"',f);
    for (;*s;s++) {
        unsigned char c=(unsigned char)*s;
        if (c=='"' || c=='\\') { fputc('\\',f); fputc(c,f); }
        else if (c<32) fprintf(f,"\\u%04x",c);
        else fputc(c,f);
    }
    fputc('"',f);
}
static uint64_t ns(struct timeval t) { return (uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_usec*1000; }
__attribute__((destructor)) static void report(void) {
    if (!output) return;
    FILE *f=fopen(output,"w");
    if (!f) return;
    fprintf(f,"{\"pid\":%d,\"compression_supported\":%s,\"overflow_or_error\":%s,\"events\":[\n",getpid(),
#ifdef __APPLE__
        "true",
#else
        "false",
#endif
        failed ? "true" : "false");
    for (size_t i=0;i<used;i++) {
        struct event *e=&events[i];
        fprintf(f,"{\"kind\":\"%c\",\"label\":",e->kind); quoted(f,e->label);
        fprintf(f,",\"trace_clock\":%llu,\"memory_valid\":%s,\"disk_read_bytes\":%llu,\"disk_write_bytes\":%llu,\"resident_bytes\":%llu,\"compressed_bytes\":%llu,\"decompressions\":%llu",
                (unsigned long long)e->trace_clock,e->memory_valid ? "true" : "false",
                (unsigned long long)e->disk_read,(unsigned long long)e->disk_write,(unsigned long long)e->resident,
                (unsigned long long)e->compressed,(unsigned long long)e->decompressions);
        fprintf(f,",\"value\":%llu,\"wall_ns\":%llu,\"user_ns\":%llu,\"system_ns\":%llu,"
                  "\"minor_faults\":%ld,\"major_faults\":%ld,\"input_blocks\":%ld,"
                  "\"voluntary_switches\":%ld,\"involuntary_switches\":%ld}%s\n",
                (unsigned long long)e->value,(unsigned long long)e->wall_ns,
                (unsigned long long)ns(e->usage.ru_utime),(unsigned long long)ns(e->usage.ru_stime),
                e->usage.ru_minflt,e->usage.ru_majflt,e->usage.ru_inblock,
                e->usage.ru_nvcsw,e->usage.ru_nivcsw,i+1==used ? "" : ",");
    }
    fputs("]}\n",f); fclose(f);
    const char *regions=getenv("LEAN_FAULT_REGIONS");
    if (regions) { f=fopen(regions,"w"); if (f) { fault_regions_report(f); fclose(f); } }
}
