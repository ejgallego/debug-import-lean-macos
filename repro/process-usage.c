/* External observer: never executes code inside the stopped Lean process. */
#include <inttypes.h>
#include <libproc.h>
#include <mach/mach_time.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/proc_info.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    struct proc_taskinfo info = {0};
    int pid = atoi(argv[1]);
    if (pid <= 0 || proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &info, sizeof(info)) != sizeof(info)) {
        perror("proc_pidinfo"); return 1;
    }
    mach_timebase_info_data_t tb;
    if (mach_timebase_info(&tb) != KERN_SUCCESS) return 1;
    /* XNU exports these CPU totals in Mach absolute-time units. */
    printf("{\"backend\":\"proc_pidinfo/PROC_PIDTASKINFO\","
           "\"user_ns\":%.0Lf,\"system_ns\":%.0Lf,"
           "\"faults\":%d,\"pageins\":%d,\"cow_faults\":%d,"
           "\"resident_bytes\":%" PRIu64 ",\"clock_resolution_ns\":%.9f}\n",
           (long double)info.pti_total_user * tb.numer / tb.denom,
           (long double)info.pti_total_system * tb.numer / tb.denom,
           info.pti_faults, info.pti_pageins, info.pti_cow_faults,
           info.pti_resident_size, (double)tb.numer / tb.denom);
    return 0;
}
