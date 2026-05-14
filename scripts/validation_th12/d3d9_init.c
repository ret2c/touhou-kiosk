/*
 * Minimal D3D9 device-creation test.
 *
 * Goal: exercise the wined3d -> OpenGL path that TH12 uses, without
 * needing any redistributable DirectX SDK binary.
 *
 * What it does:
 *   1) Initialise COM-style D3D9 (Direct3DCreate9).
 *   2) Enumerate adapter info (driver name, max resolution, vendor id).
 *   3) Try to create a hardware-accelerated, then software, IDirect3DDevice9
 *      against an off-screen / windowless target.
 *   4) Clear a render target to a known colour and Present-equivalent it,
 *      to actually drive a draw call through wined3d -> GL.
 *   5) Print clear pass/fail markers so a grep can see what the rendering
 *      stack actually did.
 *
 * License: this file is original and released into the public domain
 * (CC0). It contains no Microsoft DirectX SDK sample code; it only
 * #includes <d3d9.h> from mingw-w64 (LGPL/Zlib-equivalent) and links
 * libd3d9 (which on Wine is wined3d).
 */

#include <windows.h>
#include <d3d9.h>
#include <stdio.h>

static FILE *g_log;
#define LOGS(s) do { if(g_log){fprintf(g_log, "%s\n", s); fflush(g_log);} fprintf(stdout, "%s\n", s); fflush(stdout); } while(0)
#define LOGF(...) do { if(g_log){fprintf(g_log, __VA_ARGS__); fflush(g_log);} fprintf(stdout, __VA_ARGS__); fflush(stdout); } while(0)

static const char *PASS = "[PASS]";
static const char *FAIL = "[FAIL]";

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l) {
    return DefWindowProcA(h, m, w, l);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE prev, LPSTR cmdLine, int show) {
    /* console attach so output goes somewhere visible under wine */
    AttachConsole(ATTACH_PARENT_PROCESS);
    freopen("CONOUT$", "w", stdout);
    freopen("CONOUT$", "w", stderr);
    /* Also log to a unix path so we can grep results from the host */
    g_log = fopen("Z:\\tmp\\d3d9_init.log", "w");

    LOGS("=== d3d9_init test ===");

    IDirect3D9 *d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d) {
        LOGF("%s Direct3DCreate9 returned NULL\n", FAIL);
        return 1;
    }
    LOGF("%s Direct3DCreate9 OK, ptr=%p\n", PASS, (void*)d3d);

    UINT n = IDirect3D9_GetAdapterCount(d3d);
    LOGF("[INFO] adapter count = %u\n", n);

    D3DADAPTER_IDENTIFIER9 id = {0};
    HRESULT hr = IDirect3D9_GetAdapterIdentifier(d3d, D3DADAPTER_DEFAULT, 0, &id);
    if (FAILED(hr)) {
        LOGF("%s GetAdapterIdentifier hr=0x%08lx\n", FAIL, hr);
    } else {
        LOGF("%s adapter Driver=\"%s\" Description=\"%s\" VID=0x%04lx DID=0x%04lx\n",
             PASS, id.Driver, id.Description, id.VendorId, id.DeviceId);
    }

    D3DDISPLAYMODE mode = {0};
    hr = IDirect3D9_GetAdapterDisplayMode(d3d, D3DADAPTER_DEFAULT, &mode);
    if (FAILED(hr)) {
        LOGF("%s GetAdapterDisplayMode hr=0x%08lx\n", FAIL, hr);
    } else {
        LOGF("%s display mode %ux%u @ %uHz fmt=%d\n",
             PASS, mode.Width, mode.Height, mode.RefreshRate, mode.Format);
    }

    D3DCAPS9 caps = {0};
    hr = IDirect3D9_GetDeviceCaps(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, &caps);
    if (FAILED(hr)) {
        LOGF("[WARN] HAL caps hr=0x%08lx, trying REF\n", hr);
        hr = IDirect3D9_GetDeviceCaps(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_REF, &caps);
    }
    if (FAILED(hr)) {
        LOGF("%s GetDeviceCaps hr=0x%08lx (no HAL or REF)\n", FAIL, hr);
    } else {
        LOGF("%s caps PixelShaderVersion=0x%08lx VertexShaderVersion=0x%08lx MaxTextureWidth=%lu\n",
             PASS, caps.PixelShaderVersion, caps.VertexShaderVersion, (unsigned long)caps.MaxTextureWidth);
    }

    /* register a class and create a hidden window: D3D9 needs a hwnd */
    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = hInst;
    wc.lpszClassName = "D3D9_TEST";
    if (!RegisterClassA(&wc)) {
        LOGF("%s RegisterClassA gle=%lu\n", FAIL, GetLastError());
        return 2;
    }
    HWND hwnd = CreateWindowExA(
        0, "D3D9_TEST", "Touhou TH12 — D3D9 init smoke test",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE, 100, 100, 640, 480,
        NULL, NULL, hInst, NULL);
    if (!hwnd) {
        LOGF("%s CreateWindowExA gle=%lu\n", FAIL, GetLastError());
        return 3;
    }
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    LOGF("%s CreateWindowExA hwnd=%p (window should be visible on your XQuartz)\n", PASS, (void*)hwnd);

    D3DPRESENT_PARAMETERS pp = {0};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferWidth = 320;
    pp.BackBufferHeight = 240;
    pp.hDeviceWindow = hwnd;
    pp.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;

    IDirect3DDevice9 *dev = NULL;
    DWORD flags = D3DCREATE_SOFTWARE_VERTEXPROCESSING;

    hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL,
                                 hwnd, flags, &pp, &dev);
    if (FAILED(hr)) {
        LOGF("[WARN] CreateDevice HAL hr=0x%08lx, trying REF\n", hr);
        hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_REF,
                                     hwnd, flags, &pp, &dev);
    }
    if (FAILED(hr) || !dev) {
        LOGF("%s CreateDevice hr=0x%08lx\n", FAIL, hr);
        IDirect3D9_Release(d3d);
        return 4;
    }
    LOGF("%s CreateDevice OK dev=%p\n", PASS, (void*)dev);

    /* Drive an actual GPU op: clear and present */
    hr = IDirect3DDevice9_Clear(dev, 0, NULL, D3DCLEAR_TARGET,
                                D3DCOLOR_XRGB(0x33, 0x66, 0x99), 1.0f, 0);
    if (FAILED(hr)) {
        LOGF("%s Clear hr=0x%08lx\n", FAIL, hr);
    } else {
        LOGF("%s Clear OK (this is the wined3d -> GL glClear test)\n", PASS);
    }

    hr = IDirect3DDevice9_BeginScene(dev);
    if (SUCCEEDED(hr)) {
        IDirect3DDevice9_EndScene(dev);
        LOGF("%s BeginScene/EndScene OK\n", PASS);
    } else {
        LOGF("%s BeginScene hr=0x%08lx\n", FAIL, hr);
    }

    hr = IDirect3DDevice9_Present(dev, NULL, NULL, NULL, NULL);
    if (FAILED(hr)) {
        LOGF("%s Present hr=0x%08lx\n", FAIL, hr);
    } else {
        LOGF("%s Present OK (back buffer flipped)\n", PASS);
    }

    /* Hold the window open for ~5 seconds so you can actually see it.
     * Pump messages so the WM doesn't grey it out as "not responding". */
    LOGS("Holding window open for 5 seconds — you should see a slate-blue rectangle on XQuartz...");
    DWORD start = GetTickCount();
    while (GetTickCount() - start < 5000) {
        MSG msg;
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) goto done;
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        IDirect3DDevice9_Clear(dev, 0, NULL, D3DCLEAR_TARGET,
                               D3DCOLOR_XRGB(0x33, 0x66, 0x99), 1.0f, 0);
        IDirect3DDevice9_Present(dev, NULL, NULL, NULL, NULL);
        Sleep(16);
    }
done:

    IDirect3DDevice9_Release(dev);
    IDirect3D9_Release(d3d);
    DestroyWindow(hwnd);

    LOGS("=== done ===");
    return 0;
}
