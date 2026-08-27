/*
 * Bundle executable. Must be a Mach-O whose path is Contents/MacOS/MindControl:
 * NSBundle.mainBundle() walks up from the running image, and macOS 26's
 * Control Center will not host a status item whose bundle id is empty. A shell
 * script that exec's python3 leaves the image at Resources/python/bin/python3,
 * which is not a bundle.
 */
#include <copyfile.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <Python.h>

#define LOG_CAP (4 * 1024 * 1024)

static void ensure_dir(const char *path) {
    char buf[PATH_MAX];
    snprintf(buf, sizeof buf, "%s", path);
    for (char *p = buf + 1; *p; p++) {
        if (*p != '/') {
            continue;
        }
        *p = 0;
        mkdir(buf, 0755);
        *p = '/';
    }
    mkdir(buf, 0755);
}

static int join(char *out, size_t n, const char *a, const char *b) {
    return snprintf(out, n, "%s/%s", a, b) >= (int)n ? -1 : 0;
}

int main(int argc, char **argv) {
    char raw[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0) {
        fprintf(stderr, "mindcontrol: executable path too long\n");
        return 1;
    }
    char exe[PATH_MAX];
    if (realpath(raw, exe) == NULL) {
        snprintf(exe, sizeof exe, "%s", raw);
    }

    char macos[PATH_MAX], scratch[PATH_MAX];
    snprintf(scratch, sizeof scratch, "%s", exe);
    snprintf(macos, sizeof macos, "%s", dirname(scratch));
    char contents[PATH_MAX], res[PATH_MAX], home[PATH_MAX];
    if (join(scratch, sizeof scratch, macos, "..") || realpath(scratch, contents) == NULL ||
        join(res, sizeof res, contents, "Resources") || join(home, sizeof home, res, "python")) {
        fprintf(stderr, "mindcontrol: cannot resolve bundle layout\n");
        return 1;
    }

    const char *user = getenv("HOME");
    if (user == NULL) {
        fprintf(stderr, "mindcontrol: HOME is unset\n");
        return 1;
    }
    char config[PATH_MAX], state[PATH_MAX], logpath[PATH_MAX];
    snprintf(config, sizeof config, "%s/.config/mindcontrol", user);
    snprintf(state, sizeof state, "%s/.local/state/mindcontrol", user);
    ensure_dir(config);
    ensure_dir(state);

    char shipped[PATH_MAX], dest[PATH_MAX];
    if (join(shipped, sizeof shipped, res, "config.toml") == 0 &&
        join(dest, sizeof dest, config, "config.toml") == 0 && access(dest, F_OK) != 0) {
        copyfile(shipped, dest, NULL, COPYFILE_DATA);
    }

    char bridge[PATH_MAX];
    if (join(bridge, sizeof bridge, macos, "mindcontrol-bridge") == 0 && access(bridge, X_OK) == 0) {
        setenv("MINDCONTROL_BRIDGE", bridge, 1);
    }
    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    if (join(logpath, sizeof logpath, state, "app.log") == 0) {
        struct stat st;
        if (stat(logpath, &st) == 0 && st.st_size > LOG_CAP) {
            unlink(logpath);
        }
        if (!isatty(STDOUT_FILENO)) {
            int fd = open(logpath, O_WRONLY | O_CREAT | O_APPEND, 0644);
            if (fd >= 0) {
                dup2(fd, STDOUT_FILENO);
                dup2(fd, STDERR_FILENO);
                if (fd > STDERR_FILENO) {
                    close(fd);
                }
            }
            time_t now = time(NULL);
            struct tm tm;
            localtime_r(&now, &tm);
            char stamp[32];
            strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", &tm);
            fprintf(stderr, "\n=== %s launch ===\n", stamp);
        }
    }

    if (chdir(config) != 0) {
        fprintf(stderr, "mindcontrol: cannot enter %s\n", config);
        return 1;
    }

    int drop = 0;
    if (argc >= 2 && strncmp(argv[1], "-psn_", 5) == 0) {
        drop = 1;
    }
    /*
     * Interpreter arguments in python's own order: options, then -m / -c / a
     * script. The -m cannot be assumed to come first -- multiprocessing re-execs
     * sys.executable, which is this binary, as `-B -u -c <bootstrap>`, replaying
     * the very flags PYTHONDONTWRITEBYTECODE and PYTHONUNBUFFERED set above. So
     * scan for a module or code argument, and inject `-m mindcontrol` after the
     * options only when the caller named none, which is the LaunchServices case.
     */
    int start = 1 + drop, cut = argc, own = 0;
    for (int a = start; a < argc; a++) {
        const char *arg = argv[a];
        if (strcmp(arg, "-m") == 0 || strcmp(arg, "-c") == 0) {
            own = 1;
            break;
        }
        if (arg[0] != '-' || arg[1] == '\0') {
            cut = a;  /* a script path, or "-" for stdin */
            break;
        }
        if (strcmp(arg, "-X") == 0 || strcmp(arg, "-W") == 0) {
            a++;  /* takes its value as a separate argument */
        }
    }
    int nargs = 1 + (argc - start) + (own ? 0 : 2);
    char **args = calloc((size_t)nargs + 1, sizeof *args);
    if (args == NULL) {
        return 1;
    }
    int i = 0;
    args[i++] = exe;
    for (int a = start; a < cut; a++) {
        args[i++] = argv[a];
    }
    if (!own) {
        args[i++] = "-m";
        args[i++] = "mindcontrol";
    }
    for (int a = cut; a < argc; a++) {
        args[i++] = argv[a];
    }

    int rc = Py_BytesMain(nargs, args);
    free(args);
    return rc;
}
