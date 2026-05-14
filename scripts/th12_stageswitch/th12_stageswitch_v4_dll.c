/*
 * th12_stageswitch_v4.dll — in-process DLL that drives TH12's stage-init
 * code paths from the main game thread via a WH_GETMESSAGE hook, then
 * additionally zeroes a small set of post-process render-state globals
 * that TH12's natural pause-menu Retry path zeroes but the bare
 * teardown+init pair does not.
 *
 * Specifically, in addition to calling 0x422770 (stage teardown) and
 * 0x422700(0) (stage init), this DLL writes:
 *     [0x004CF2A8] = 0x00000000   (bloom Clear color, alpha=0)
 *     [0x004CF3FC] = 0            (a render-flag global)
 *     [0x004CE000..0x004CE5FF] = 0  (the pause-capture JPEG buffer)
 *
 * These three addresses came from a memory diff between a dirty
 * post-stage_switch state and a cleaned post-pause-Retry state on TH12
 * running natively on Windows. Writing them brings TH12's data-section
 * state into the same shape as a natural retry produces.
 *
 * Known limitation: the visible green/cyan tint that can appear on a
 * stage_switch under wine/Mali is NOT fully cleared by these writes.
 * The pause→Retry path also issues IDirect3DDevice9 method calls
 * (Clear, SetRenderState, SetTexture, SetSamplerState, etc.) that
 * re-initialize the d3d device's internal state held inside wined3d.
 * Those calls do not appear in a /proc/<pid>/mem diff because that
 * state lives on the host (wine) side, not in TH12's heap. Replicating
 * those calls is open work; this DLL fixes the memory side only.
 *
 * Build:
 *   i686-w64-mingw32-gcc -m32 -shared -Os -s -static -Wl,--kill-at \
 *       th12_stageswitch_v4_dll.c -o th12_stageswitch_v4.dll
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>

#define STAGE_NUM_ADDR        0x004B0CB0u
#define STAGE_BACKUP_ADDR     0x004B0CB4u
#define STAGE_TABLE_PTR_ADDR  0x004B452Cu
#define STAGE_STRUCT_PTR_ADDR 0x004B44E8u
#define TASK_CALLBACK_SLOT    0x004CF0F0u
#define WINDOW_CLASS_NAME     "BASE"

#define FN_STAGE_TEAR_DOWN     0x00422770u
#define FN_STAGE_START_WRAPPER 0x00422700u

#define D3D_DEVICE_PTR_ADDR    0x004CE8F0u
#define BLOOM_RT_1_ADDR        0x004CEA94u
#define BLOOM_RT_2_ADDR        0x004CEA98u
#define BLOOM_RT_3_ADDR        0x004CEA9Cu

/* Targets identified by the dirty-vs-clean diff: */
#define BLOOM_CLEAR_COLOR_ADDR 0x004CF2A8u  /* MUST be 0 (alpha=0), not 0xFF000000 */
#define BLOOM_FLAG_ADDR        0x004CF3FCu  /* MUST be 0 */
#define PAUSE_CAPTURE_BUFFER   0x004CE000u  /* zeroed by natural retry */
#define PAUSE_CAPTURE_LEN      0x600        /* ~1.4 kB; covers 4CE000..4CE5FF */

#define D3D9_COLORFILL_VTBL_OFFSET 0x8C

typedef void   (__cdecl   *fn_teardown_t)(void);
typedef void * (__stdcall *fn_start_t)(unsigned int);
typedef HRESULT (__stdcall *fn_colorfill_t)(void *this_dev, void *surface,
                                             const void *rect, DWORD color);

static HHOOK         g_hook   = NULL;
static volatile LONG g_done   = 0;

static void write_log(const char *s) {
    HANDLE f = CreateFileA("th12_stageswitch_v4.log", FILE_APPEND_DATA,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    SetFilePointer(f, 0, NULL, FILE_END);
    DWORD n; DWORD len = 0; while (s[len]) len++;
    WriteFile(f, s, len, &n, NULL); CloseHandle(f);
}
static void log_hex4(const char *p, DWORD a, DWORD b, DWORD c, DWORD d) {
    char buf[260]; int i = 0;
    while (p[i] && i < 180) { buf[i] = p[i]; i++; }
    buf[i++] = ' '; const char *hex = "0123456789abcdef";
    DWORD vs[4] = {a,b,c,d};
    for (int k = 0; k < 4; k++) {
        for (int j = 7; j >= 0; j--) buf[i++] = hex[(vs[k] >> (j*4)) & 0xf];
        buf[i++] = ' ';
    }
    buf[i++] = '\n'; buf[i] = 0; write_log(buf);
}

static int read_target_stage(void) {
    HANDLE f = CreateFileA("stageswitch_target", GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return 1;
    char buf[8] = {0}; DWORD got = 0;
    ReadFile(f, buf, 4, &got, NULL); CloseHandle(f);
    for (DWORD k = 0; k < got; k++) {
        if (buf[k] >= '1' && buf[k] <= '6') return buf[k] - '0';
    }
    return 1;
}

static int wipe_bloom_rts(void) {
    void *dev = *(void **volatile)D3D_DEVICE_PTR_ADDR;
    if (dev == NULL) return -1;
    void **vtable = *(void ***)dev;
    if (vtable == NULL) return -1;
    fn_colorfill_t pColorFill =
        (fn_colorfill_t)vtable[D3D9_COLORFILL_VTBL_OFFSET / 4];
    const DWORD rt_addrs[3] = {BLOOM_RT_1_ADDR, BLOOM_RT_2_ADDR, BLOOM_RT_3_ADDR};
    int cleared = 0;
    for (int i = 0; i < 3; i++) {
        void *rt = *(void **volatile)(uintptr_t)rt_addrs[i];
        if (rt == NULL) continue;
        HRESULT hr = pColorFill(dev, rt, NULL, 0x00000000u);
        log_hex4("RT ColorFill rt_addr,surf,hr,_",
                 rt_addrs[i], (DWORD)(uintptr_t)rt, (DWORD)hr, 0);
        if (SUCCEEDED(hr)) cleared++;
    }
    return cleared;
}

static void log_state(const char *prefix) {
    DWORD bcc = *(volatile DWORD*)BLOOM_CLEAR_COLOR_ADDR;
    DWORD flg = *(volatile DWORD*)BLOOM_FLAG_ADDR;
    DWORD c0  = *(volatile DWORD*)PAUSE_CAPTURE_BUFFER;        /* 0xFFD8FFE0 = JPEG */
    DWORD c4  = *(volatile DWORD*)(PAUSE_CAPTURE_BUFFER + 4);
    log_hex4(prefix, bcc, flg, c0, c4);
}

static LRESULT CALLBACK HookProc(int code, WPARAM wp, LPARAM lp) {
    if (code >= 0 && InterlockedCompareExchange(&g_done, 1, 0) == 0) {
        DWORD pre_stage = *(volatile DWORD*)STAGE_NUM_ADDR;
        DWORD pre_sptr  = *(volatile DWORD*)STAGE_STRUCT_PTR_ADDR;
        DWORD pre_table = *(volatile DWORD*)STAGE_TABLE_PTR_ADDR;
        log_hex4("HOOK pre stage,sptr,table,_", pre_stage, pre_sptr, pre_table, 0);
        log_state("HOOK pre bcc,flg,jpeg0,jpeg4");

        int new_stage = read_target_stage();
        log_hex4("HOOK target_stage,_,_,_", new_stage, 0, 0, 0);

        if (pre_stage < 1 || pre_stage > 7 || pre_sptr == 0) {
            log_hex4("HOOK precond fail; bailing", 0, 0, 0, 0);
            g_done = 1;
            return CallNextHookEx(g_hook, code, wp, lp);
        }

        DWORD new_table = 0x004AEBF0u + (((DWORD)new_stage) << 6);
        *(volatile DWORD*)STAGE_NUM_ADDR       = (DWORD)new_stage;
        *(volatile DWORD*)STAGE_BACKUP_ADDR    = (DWORD)new_stage;
        *(volatile DWORD*)STAGE_TABLE_PTR_ADDR = new_table;

        ((fn_teardown_t)FN_STAGE_TEAR_DOWN)();
        void *new_struct = ((fn_start_t)FN_STAGE_START_WRAPPER)(0);
        log_hex4("HOOK after stage_start new_struct,_,_,_",
                 (DWORD)(uintptr_t)new_struct, 0, 0, 0);

        /* Clear bloom RT surfaces. */
        int cleared = wipe_bloom_rts();
        log_hex4("HOOK bloom rts cleared,_,_,_", (DWORD)cleared, 0, 0, 0);

        /* Match natural Retry cleanup state. The pause-menu retry path
         * clears these globals on its way out; the stage_switch path
         * doesn't, so we write them explicitly to keep the engine state
         * equivalent to a clean retry. */

        /* 1) Bloom Clear color: alpha=0 (transparent), not 0xFF000000.
         *    On wine/Mali the opaque value drives the bleed. */
        *(volatile DWORD*)BLOOM_CLEAR_COLOR_ADDR = 0x00000000u;

        /* 2) Bloom/render flag at [0x4CF3FC]: clear. */
        *(volatile DWORD*)BLOOM_FLAG_ADDR = 0;

        /* 3) Zero the ~1.4 kB pause-capture/JPEG buffer at [0x4CE000].
         *    If this buffer is sampled as a texture source during
         *    composition, stale contents can bleed into the next frame. */
        memset((void*)PAUSE_CAPTURE_BUFFER, 0, PAUSE_CAPTURE_LEN);

        log_state("HOOK post-fix bcc,flg,jpeg0,jpeg4");
    }
    return CallNextHookEx(g_hook, code, wp, lp);
}

static DWORD WINAPI WorkerThread(LPVOID p) {
    HINSTANCE hMod = (HINSTANCE)p;
    write_log("WORKER start\n");

    HWND hwnd = NULL;
    for (int i = 0; i < 500; i++) {
        hwnd = FindWindowA(WINDOW_CLASS_NAME, NULL);
        if (hwnd) break;
        Sleep(10);
    }
    if (!hwnd) { write_log("no BASE window\n"); FreeLibraryAndExitThread(hMod, 1); return 1; }
    DWORD main_tid = GetWindowThreadProcessId(hwnd, NULL);

    g_hook = SetWindowsHookExA(WH_GETMESSAGE, HookProc, hMod, main_tid);
    if (!g_hook) {
        log_hex4("WORKER hook fail gle,_,_,_", GetLastError(), 0, 0, 0);
        FreeLibraryAndExitThread(hMod, 2); return 2;
    }

    int waited = 0; int last_post = -1000;
    while (!g_done && waited < 5000) {
        if (waited - last_post >= 50) {
            PostMessageA(hwnd, WM_NULL, 0, 0);
            last_post = waited;
        }
        Sleep(10); waited += 10;
    }
    log_hex4("WORKER waited_ms,done,_,_", waited, g_done, 0, 0);

    UnhookWindowsHookEx(g_hook);
    g_hook = NULL;
    Sleep(50);
    FreeLibraryAndExitThread(hMod, 0);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hMod, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hMod);
        HANDLE h = CreateThread(NULL, 0, WorkerThread, hMod, 0, NULL);
        if (h) CloseHandle(h);
    }
    return TRUE;
}
