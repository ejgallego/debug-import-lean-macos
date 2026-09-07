/* Process-scoped THP control applied before the target is loaded. */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc<3 || (strcmp(argv[1],"default") && strcmp(argv[1],"disabled"))) {
        fprintf(stderr,"usage: %s default|disabled command [args...]\n",argv[0]); return 2;
    }
    int before=prctl(PR_GET_THP_DISABLE,0L,0L,0L,0L);
    if (before!=0) { fprintf(stderr,"unexpected inherited THP state: %d\n",before); return 3; }
    int disabled=!strcmp(argv[1],"disabled");
    if (disabled && prctl(PR_SET_THP_DISABLE,1L,0L,0L,0L)) { perror("PR_SET_THP_DISABLE"); return 4; }
    int after=prctl(PR_GET_THP_DISABLE,0L,0L,0L,0L);
    if (after!=disabled) { fprintf(stderr,"THP state did not match request: %d\n",after); return 5; }
    const char *path=getenv("LEAN_THP_LAUNCH_OUTPUT");
    if (path) {
        FILE *f=fopen(path,"w"); if (!f) { perror(path); return 6; }
        fprintf(f,"{\"pid\":%d,\"before\":%d,\"after\":%d}\n",getpid(),before,after);
        if (fclose(f)) { perror("fclose"); return 7; }
    }
    execvp(argv[2],argv+2); perror("execvp"); return 127;
}
