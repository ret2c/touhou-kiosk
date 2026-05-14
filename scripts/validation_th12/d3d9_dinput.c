/*
 * DirectInput 8 + Direct3D 9 combined test.
 * TH12 uses DirectInput 8 for keyboard/joystick polling and D3D9 for
 * rendering. This test exercises both initialisation paths.
 *
 * Public domain (CC0).
 */
#define DIRECTINPUT_VERSION 0x0800
#include <windows.h>
#include <d3d9.h>
#include <dinput.h>
#include <stdio.h>

static FILE *g_log;
#define LOG(...) do { if(g_log){fprintf(g_log,__VA_ARGS__); fflush(g_log);} } while(0)

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l){return DefWindowProcA(h,m,w,l);}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE prev, LPSTR cmd, int show) {
    g_log = fopen("Z:\\tmp\\d3d9_dinput.log", "w");
    LOG("=== d3d9_dinput test ===\n");

    /* DirectInput 8 */
    LPDIRECTINPUT8 di = NULL;
    HRESULT hr = DirectInput8Create(hInst, DIRECTINPUT_VERSION, &IID_IDirectInput8A,
                                    (void**)&di, NULL);
    if (FAILED(hr) || !di) {
        LOG("FAIL DirectInput8Create 0x%08lx\n", hr);
    } else {
        LOG("PASS DirectInput8Create di=%p\n", (void*)di);
        LPDIRECTINPUTDEVICE8 kbd = NULL;
        hr = IDirectInput8_CreateDevice(di, &GUID_SysKeyboard, &kbd, NULL);
        if (FAILED(hr) || !kbd) {
            LOG("FAIL CreateDevice keyboard 0x%08lx\n", hr);
        } else {
            LOG("PASS CreateDevice keyboard kbd=%p\n", (void*)kbd);
            hr = IDirectInputDevice8_SetDataFormat(kbd, &c_dfDIKeyboard);
            LOG("%s SetDataFormat keyboard hr=0x%08lx\n", FAILED(hr)?"FAIL":"PASS", hr);
            IDirectInputDevice8_Release(kbd);
        }
        IDirectInput8_Release(di);
    }

    /* D3D9 quick init */
    IDirect3D9 *d3d = Direct3DCreate9(D3D_SDK_VERSION);
    LOG("%s Direct3DCreate9 ptr=%p\n", d3d?"PASS":"FAIL", (void*)d3d);
    if (d3d) IDirect3D9_Release(d3d);

    LOG("=== done ===\n");
    return 0;
}
