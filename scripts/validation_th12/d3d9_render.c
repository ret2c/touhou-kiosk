/*
 * D3D9 sustained-render FPS test.
 *
 * Renders a fixed number of frames through wined3d, each frame:
 *   - Clear with a colour cycling on the frame index
 *   - DrawPrimitiveUP of two triangles (a coloured quad) -- exercises
 *     the vertex/raster pipeline
 *   - Present
 *
 * Reports total wall time, frames, average FPS. This is the closest
 * proxy we have for "can this stack do TH12-style rendering at 60Hz"
 * without any actual game.
 *
 * Public domain (CC0).
 */
#include <windows.h>
#include <d3d9.h>
#include <stdio.h>
#include <stdint.h>

static FILE *g_log;
#define LOG(...) do { if(g_log){fprintf(g_log, __VA_ARGS__); fflush(g_log);} } while(0)

typedef struct { float x, y, z, rhw; DWORD color; } VTX;
#define FVF (D3DFVF_XYZRHW | D3DFVF_DIFFUSE)

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l) {
    return DefWindowProcA(h, m, w, l);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE prev, LPSTR cmdLine, int show) {
    g_log = fopen("Z:\\tmp\\d3d9_render.log", "w");
    LOG("=== d3d9_render test ===\n");

    int frames = 240;  /* 4 sec at 60Hz */
    if (cmdLine && cmdLine[0]) {
        int v = atoi(cmdLine);
        if (v > 0 && v < 100000) frames = v;
    }
    LOG("target_frames=%d\n", frames);

    IDirect3D9 *d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d) { LOG("FAIL Direct3DCreate9\n"); return 1; }

    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = hInst;
    wc.lpszClassName = "D3D9_RENDER";
    RegisterClassA(&wc);
    HWND hwnd = CreateWindowExA(0, "D3D9_RENDER",
        "Touhou TH12 — D3D9 render test (Wine + box86 → XQuartz)",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE, 100, 100, 640, 480,
        NULL, NULL, hInst, NULL);
    if (!hwnd) { LOG("FAIL CreateWindow\n"); return 2; }
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);

    D3DPRESENT_PARAMETERS pp = {0};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferWidth = 640;
    pp.BackBufferHeight = 480;
    pp.hDeviceWindow = hwnd;
    pp.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;

    IDirect3DDevice9 *dev = NULL;
    HRESULT hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL,
        hwnd, D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &dev);
    if (FAILED(hr) || !dev) { LOG("FAIL CreateDevice 0x%08lx\n", hr); return 3; }
    LOG("device created\n");

    IDirect3DDevice9_SetFVF(dev, FVF);
    IDirect3DDevice9_SetRenderState(dev, D3DRS_LIGHTING, FALSE);
    IDirect3DDevice9_SetRenderState(dev, D3DRS_CULLMODE, D3DCULL_NONE);

    LARGE_INTEGER pf, t0, t1;
    QueryPerformanceFrequency(&pf);
    QueryPerformanceCounter(&t0);

    int actually_drawn = 0;
    int present_failures = 0;
    int draw_failures = 0;

    for (int f = 0; f < frames; f++) {
        /* Pump messages so the window stays responsive to the WM. */
        MSG msg;
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) { f = frames; break; }
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }

        DWORD bg = D3DCOLOR_XRGB((f * 3) & 0xff, (f * 5) & 0xff, (f * 7) & 0xff);
        IDirect3DDevice9_Clear(dev, 0, NULL, D3DCLEAR_TARGET, bg, 1.0f, 0);

        if (SUCCEEDED(IDirect3DDevice9_BeginScene(dev))) {
            VTX v[6];
            float cx = 320.0f + 100.0f * (float)((f % 60) - 30) / 30.0f;
            float cy = 240.0f;
            float s = 50.0f;
            DWORD c = D3DCOLOR_XRGB(0xff, 0x40, 0x40);
            v[0].x = cx - s; v[0].y = cy - s; v[0].z = 0; v[0].rhw = 1; v[0].color = c;
            v[1].x = cx + s; v[1].y = cy - s; v[1].z = 0; v[1].rhw = 1; v[1].color = c;
            v[2].x = cx - s; v[2].y = cy + s; v[2].z = 0; v[2].rhw = 1; v[2].color = c;
            v[3].x = cx + s; v[3].y = cy - s; v[3].z = 0; v[3].rhw = 1; v[3].color = c;
            v[4].x = cx + s; v[4].y = cy + s; v[4].z = 0; v[4].rhw = 1; v[4].color = c;
            v[5].x = cx - s; v[5].y = cy + s; v[5].z = 0; v[5].rhw = 1; v[5].color = c;
            hr = IDirect3DDevice9_DrawPrimitiveUP(dev, D3DPT_TRIANGLELIST, 2, v, sizeof(VTX));
            if (FAILED(hr)) draw_failures++;
            IDirect3DDevice9_EndScene(dev);
        }

        hr = IDirect3DDevice9_Present(dev, NULL, NULL, NULL, NULL);
        if (FAILED(hr)) present_failures++;
        else actually_drawn++;
    }

    QueryPerformanceCounter(&t1);
    double secs = (double)(t1.QuadPart - t0.QuadPart) / (double)pf.QuadPart;
    double fps = (double)actually_drawn / (secs > 0 ? secs : 1.0);
    LOG("done frames=%d drawn=%d wall=%.3fs fps=%.2f present_fail=%d draw_fail=%d\n",
        frames, actually_drawn, secs, fps, present_failures, draw_failures);

    IDirect3DDevice9_Release(dev);
    IDirect3D9_Release(d3d);
    DestroyWindow(hwnd);
    return 0;
}
