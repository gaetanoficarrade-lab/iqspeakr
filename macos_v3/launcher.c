// IQspeakr Bundle-Launcher (Mach-O statt Bash-Wrapper)
//
// Hintergrund:
// macOS Tahoe TCC ordnet Berechtigungen ueber den Mach-O-Pfad des
// Hauptprozesses zu (proc_pidpath). Wenn Contents/MacOS/IQspeakr ein
// Bash-Skript ist, sieht TCC den exec'd Python-Subprocess als eigene
// "App" — die Bundle-Berechtigung "IQspeakr" greift nicht.
//
// Mit einem echten Mach-O-Binary ist das Hauptbinary in Bundle-Pfad,
// TCC erkennt es als zum Bundle gehoerend, und exec'd Children erben
// die Identity ueber Apple's "responsible app"-Mechanismus.
//
// Kompiliert mit: clang -O2 -arch arm64 -o launcher launcher.c

#include <unistd.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>

int main(int argc, char *argv[]) {
    char exe_path[PATH_MAX];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "iqspeakr-launcher: _NSGetExecutablePath failed\n");
        return 1;
    }

    // exe_path = /Applications/IQspeakr.app/Contents/MacOS/IQspeakr
    // Bundle-Contents ableiten (zwei Verzeichnisebenen hoch)
    char contents_path[PATH_MAX];
    strncpy(contents_path, exe_path, sizeof(contents_path) - 1);
    contents_path[sizeof(contents_path) - 1] = '\0';

    char *p = strrchr(contents_path, '/');  // /MacOS/IQspeakr -> /MacOS
    if (!p) { fprintf(stderr, "iqspeakr-launcher: bad path\n"); return 1; }
    *p = '\0';
    p = strrchr(contents_path, '/');         // /MacOS -> /Contents
    if (!p) { fprintf(stderr, "iqspeakr-launcher: bad path\n"); return 1; }
    *p = '\0';
    // contents_path jetzt = /Applications/IQspeakr.app/Contents

    // App-Skript + venv im User-Home
    const char *home = getenv("HOME");
    if (!home) { fprintf(stderr, "iqspeakr-launcher: HOME not set\n"); return 1; }

    // venv-Python: ~/.iqspeakr/venv/bin/python3 — dieser Symlink zeigt
    // auf Contents/Resources/python/bin/python3, also INS Bundle. Beim
    // execv() resolved macOS den Symlink, daher proc_pidpath = Bundle-
    // Pfad (TCC mappt korrekt auf com.iqspeakr.app). sys.prefix bleibt
    // venv, daher sind alle pip-Pakete (numpy, whisper, etc.) sichtbar.
    char python_path[PATH_MAX];
    snprintf(python_path, sizeof(python_path),
             "%s/.iqspeakr/venv/bin/python3", home);
    (void)contents_path;  // nicht direkt verwendet, aber fuer Debug behalten

    char app_py[PATH_MAX];
    snprintf(app_py, sizeof(app_py), "%s/.iqspeakr/app.py", home);

    // PATH erweitern fuer ffmpeg-Lookup (ffmpeg liegt in ~/.iqspeakr/bin)
    char new_path[PATH_MAX * 2];
    const char *current_path = getenv("PATH");
    snprintf(new_path, sizeof(new_path), "%s/.iqspeakr/bin:%s",
             home, current_path ? current_path : "/usr/bin:/bin");
    setenv("PATH", new_path, 1);

    // KRITISCH: __PYVENV_LAUNCHER__ zwingt Python, den venv-Symlink-Pfad
    // als Launcher zu nehmen — sonst returnt _NSGetExecutablePath() den
    // resolvten Bundle-Pfad, und Python aktiviert venv nicht. Damit waeren
    // venv-Pakete (numpy, whisper, ...) nicht sichtbar. Diese env var ist
    // die offizielle CPython-Schnittstelle fuer custom Launcher.
    setenv("__PYVENV_LAUNCHER__", python_path, 1);

    // stdout/stderr nach ~/IQspeakr.log umleiten
    char log_path[PATH_MAX];
    snprintf(log_path, sizeof(log_path), "%s/IQspeakr.log", home);
    int log_fd = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (log_fd >= 0) {
        dup2(log_fd, STDOUT_FILENO);
        dup2(log_fd, STDERR_FILENO);
        close(log_fd);
    }

    // execv (NICHT fork+exec) — Python ersetzt diesen Prozess.
    // Mach-O-Identity bleibt erhalten, weil _path0 das Bundle bleibt.
    char *new_argv[] = { python_path, app_py, NULL };
    execv(python_path, new_argv);

    fprintf(stderr, "iqspeakr-launcher: execv failed: %s -> %s\n",
            python_path, app_py);
    return 1;
}
