// Sandboxed parent stand-in for the Tauri shell, used by
// scripts/lib/sandboxed_jit_smoke.sh (INSTALL-3287).
//
// The payload interpreter is signed with com.apple.security.inherit, and a
// binary carrying that key only runs as the child of a sandboxed process, so
// the App Store signing cannot be proven by running python3 directly the way
// the Developer ID smoke does. This parent spawns the same shape the real app
// does (a /bin/sh launcher that execs the interpreter) and reports how the
// child ended on stdout, where the smoke reads it.
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <mach-o/dyld.h>
#include <libgen.h>
#include <string.h>
extern char **environ;
int main(void) {
    char exe[4096]; uint32_t n = sizeof exe;
    if (_NSGetExecutablePath(exe, &n) != 0) return 90;
    char script[4200];
    snprintf(script, sizeof script, "%s/../Resources/jitprobe.sh", dirname(exe));
    char *argv[] = {"/bin/sh", script, NULL};
    pid_t pid;
    int rc = posix_spawn(&pid, "/bin/sh", NULL, NULL, argv, environ);
    if (rc != 0) { printf("SPAWN_FAILED %d\n", rc); return 91; }
    int status;
    waitpid(pid, &status, 0);
    if (WIFSIGNALED(status)) { printf("CHILD_SIGNAL %d\n", WTERMSIG(status)); return 128 + WTERMSIG(status); }
    printf("CHILD_EXIT %d\n", WEXITSTATUS(status));
    fflush(stdout);
    return WEXITSTATUS(status);
}
