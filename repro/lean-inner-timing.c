/* In-process phase snapshots. Buffer in memory; emit only at process exit. */
#define _POSIX_C_SOURCE 200809L
#include <lean/lean.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

#define CAPACITY 8192
struct event {
    char kind;
    char label[192];
    uint64_t wall_ns, value;
    struct rusage usage;
};
static struct event events[CAPACITY];
static size_t used;
static int failed;
static const char *output;
__attribute__((constructor)) static void setup(void) { output=getenv("LEAN_INNER_TIMING_OUTPUT"); }
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
            strcpy(e->label,lean_string_cstr(label));
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
    fprintf(f,"{\"overflow_or_error\":%s,\"events\":[\n",failed ? "true" : "false");
    for (size_t i=0;i<used;i++) {
        struct event *e=&events[i];
        fprintf(f,"{\"kind\":\"%c\",\"label\":",e->kind); quoted(f,e->label);
        fprintf(f,",\"value\":%llu,\"wall_ns\":%llu,\"user_ns\":%llu,\"system_ns\":%llu,"
                  "\"minor_faults\":%ld,\"major_faults\":%ld,\"input_blocks\":%ld,"
                  "\"voluntary_switches\":%ld,\"involuntary_switches\":%ld}%s\n",
                (unsigned long long)e->value,(unsigned long long)e->wall_ns,
                (unsigned long long)ns(e->usage.ru_utime),(unsigned long long)ns(e->usage.ru_stime),
                e->usage.ru_minflt,e->usage.ru_majflt,e->usage.ru_inblock,
                e->usage.ru_nvcsw,e->usage.ru_nivcsw,i+1==used ? "" : ",");
    }
    fputs("]}\n",f); fclose(f);
}
