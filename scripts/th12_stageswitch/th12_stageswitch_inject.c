/* Loader for th12_stageswitch.dll. Same pattern as the original
 * th12_restart_inject.exe.
 *
 * Build:
 *   i686-w64-mingw32-gcc -m32 -Os -s -static \
 *       th12_stageswitch_inject.c -o th12_stageswitch_inject.exe
 *
 * Usage:
 *   th12_stageswitch_inject.exe [--pid N] [--stage K] [--dll PATH]
 *
 * --stage K writes "K" to "stageswitch_target" in th12's CWD before
 * injecting, so the DLL reads it after attach.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define DEFAULT_DLL "th12_stageswitch.dll"

static DWORD find_th12_pid(void) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32 pe = { .dwSize = sizeof(pe) }; DWORD pid = 0;
    if (Process32First(snap, &pe)) {
        do {
            if (_stricmp(pe.szExeFile, "th12.exe") == 0) { pid = pe.th32ProcessID; break; }
        } while (Process32Next(snap, &pe));
    }
    CloseHandle(snap); return pid;
}

static int find_th12_cwd(DWORD pid, char *out, DWORD outlen) {
    /* easier: use Process32 to find th12.exe path, then derive cwd */
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid);
    if (snap == INVALID_HANDLE_VALUE) return 1;
    MODULEENTRY32 me = { .dwSize = sizeof(me) };
    if (!Module32First(snap, &me)) { CloseHandle(snap); return 1; }
    /* me.szExePath is the th12.exe path; cwd is its directory */
    strncpy(out, me.szExePath, outlen);
    for (int i = (int)strlen(out)-1; i >= 0; i--) {
        if (out[i] == '\\' || out[i] == '/') { out[i] = 0; break; }
    }
    CloseHandle(snap); return 0;
}

static int resolve_dll_path(const char *arg, char *out, DWORD outlen) {
    if (arg) return GetFullPathNameA(arg, outlen, out, NULL) ? 0 : 1;
    char self[MAX_PATH]; DWORD n = GetModuleFileNameA(NULL, self, MAX_PATH);
    if (!n) return 1;
    for (int i = (int)n-1; i >= 0; i--) {
        if (self[i] == '\\' || self[i] == '/') { self[i+1] = 0; break; }
    }
    return snprintf(out, outlen, "%s%s", self, DEFAULT_DLL) >= (int)outlen;
}

int main(int argc, char **argv) {
    DWORD pid = 0; int stage = 0;
    const char *dll_arg = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--pid") && i+1 < argc) pid = (DWORD)strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--stage") && i+1 < argc) stage = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--dll") && i+1 < argc) dll_arg = argv[++i];
    }
    if (!pid) pid = find_th12_pid();
    if (!pid) { fprintf(stderr, "stageswitch: th12.exe not found\n"); return 1; }
    fprintf(stderr, "stageswitch: pid=%lu\n", (unsigned long)pid);

    /* If --stage was passed, write it into th12's CWD/stageswitch_target. */
    if (stage >= 1 && stage <= 6) {
        char cwd[MAX_PATH] = {0};
        if (find_th12_cwd(pid, cwd, sizeof cwd) == 0) {
            char target[MAX_PATH];
            snprintf(target, sizeof target, "%s\\stageswitch_target", cwd);
            HANDLE f = CreateFileA(target, GENERIC_WRITE, 0, NULL,
                                    CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (f != INVALID_HANDLE_VALUE) {
                char buf[2] = { (char)('0' + stage), 0 };
                DWORD nw = 0; WriteFile(f, buf, 1, &nw, NULL);
                CloseHandle(f);
                fprintf(stderr, "stageswitch: wrote target='%c' to %s\n", buf[0], target);
            } else {
                fprintf(stderr, "stageswitch: couldn't open target file %s\n", target);
            }
        } else {
            fprintf(stderr, "stageswitch: couldn't find th12 cwd\n");
        }
    }

    char dll_path[MAX_PATH];
    if (resolve_dll_path(dll_arg, dll_path, sizeof dll_path)) {
        fprintf(stderr, "stageswitch: cannot resolve dll path\n"); return 1;
    }
    fprintf(stderr, "stageswitch: dll=%s\n", dll_path);
    if (GetFileAttributesA(dll_path) == INVALID_FILE_ATTRIBUTES) {
        fprintf(stderr, "stageswitch: dll does not exist\n"); return 1;
    }

    HANDLE proc = OpenProcess(
        PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ |
        PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION, FALSE, pid);
    if (!proc) { fprintf(stderr, "OpenProcess fail le=%lu\n", GetLastError()); return 2; }

    SIZE_T plen = strlen(dll_path) + 1;
    LPVOID rmem = VirtualAllocEx(proc, NULL, plen, MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE);
    SIZE_T nw = 0;
    WriteProcessMemory(proc, rmem, dll_path, plen, &nw);
    HMODULE k32 = GetModuleHandleA("kernel32.dll");
    LPTHREAD_START_ROUTINE pLL = (LPTHREAD_START_ROUTINE)GetProcAddress(k32, "LoadLibraryA");
    HANDLE hT = CreateRemoteThread(proc, NULL, 0, pLL, rmem, 0, NULL);
    WaitForSingleObject(hT, 10000);
    DWORD ex = 0; GetExitCodeThread(hT, &ex);
    fprintf(stderr, "stageswitch: LoadLibrary exit=0x%lx\n", ex);
    CloseHandle(hT);

    Sleep(2500);
    VirtualFreeEx(proc, rmem, 0, MEM_RELEASE);

    SIZE_T n = 0; DWORD post_score=0, post_frame=0, post_stage=0, post_sptr=0, post_cb=0;
    ReadProcessMemory(proc, (LPCVOID)0x004B0C44, &post_score, 4, &n);
    ReadProcessMemory(proc, (LPCVOID)0x004B0CBC, &post_frame, 4, &n);
    ReadProcessMemory(proc, (LPCVOID)0x004B0CB0, &post_stage, 4, &n);
    ReadProcessMemory(proc, (LPCVOID)0x004B44E8, &post_sptr,  4, &n);
    ReadProcessMemory(proc, (LPCVOID)0x004CF0F0, &post_cb,    4, &n);
    fprintf(stderr, "stageswitch: post score=%lu frame=%lu stage=%lu struct=0x%lx cb=0x%lx\n",
            (unsigned long)post_score, (unsigned long)post_frame, (unsigned long)post_stage,
            (unsigned long)post_sptr, (unsigned long)post_cb);

    CloseHandle(proc);
    return 0;
}
