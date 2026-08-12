#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <stdint.h>

namespace {

constexpr uint32_t kApiVersion = 3;
constexpr uint32_t kQueueCapacity = 1024;
constexpr uint32_t kCompletionCapacity = 4096;
constexpr uint32_t kInputMoveCapacity = 8192;

struct RelayInputReport {
    uint32_t flags;
    int32_t x;
    int32_t y;
    uint32_t pressure;
    int32_t tilt_x;
    uint32_t tilt_enabled;
    uint64_t token;
};

struct RelayReport {
    uint32_t flags;
    int32_t x;
    int32_t y;
    uint32_t pressure;
    int32_t tilt_x;
    uint32_t tilt_enabled;
    uint64_t token;
    int64_t submitted_qpc;
};

struct RelayCompletion {
    uint64_t token;
    uint64_t submitted_qpc;
    uint64_t inject_begin_qpc;
    uint64_t completed_qpc;
    uint64_t qpc_frequency;
    uint32_t flags;
    int32_t x;
    int32_t y;
    uint32_t pressure;
    uint32_t success;
    uint32_t error;
    uint32_t queue_delay_us;
    uint32_t inject_call_us;
};

struct RelayStats {
    uint32_t struct_size;
    uint32_t api_version;
    uint64_t submitted;
    uint64_t injected;
    uint64_t failed;
    uint64_t queue_full;
    uint32_t max_queue_depth;
    uint32_t last_error;
    uint64_t total_queue_delay_us;
    uint64_t total_inject_call_us;
    uint32_t last_queue_delay_us;
    uint32_t last_inject_call_us;
    uint32_t max_queue_delay_us;
    uint32_t max_inject_call_us;
    uint64_t completion_dropped;
    uint64_t qpc_frequency;
};

struct InputMove {
    int32_t x;
    int32_t y;
    uint32_t flags;
    uint32_t message_time_ms;
    uint64_t observed_qpc;
    uint64_t qpc_frequency;
};

struct InputCaptureStats {
    uint32_t struct_size;
    uint32_t api_version;
    uint64_t captured;
    uint64_t drained;
    uint64_t dropped;
    uint32_t max_queue_depth;
    uint32_t last_error;
    uint64_t qpc_frequency;
};

struct InputCapture {
    HANDLE ready_event;
    HANDLE thread;
    DWORD thread_id;
    HHOOK hook;
    CRITICAL_SECTION lock;
    InputMove moves[kInputMoveCapacity];
    uint32_t head;
    uint32_t count;
    DWORD startup_error;
    LARGE_INTEGER qpc_frequency;
    InputCaptureStats stats;
};

InputCapture* volatile g_input_capture = nullptr;

struct Relay {
    HSYNTHETICPOINTERDEVICE device;
    HANDLE wake_event;
    HANDLE idle_event;
    HANDLE thread;
    CRITICAL_SECTION lock;
    RelayReport queue[kQueueCapacity];
    uint32_t head;
    uint32_t count;
    RelayCompletion completions[kCompletionCapacity];
    uint32_t completion_head;
    uint32_t completion_count;
    bool stopping;
    uint32_t fatal_error;
    uint32_t min_frame_interval_us;
    uint32_t dpi;
    LARGE_INTEGER qpc_frequency;
    int64_t last_inject_qpc;
    RelayStats stats;
};

uint32_t clamp_u32(uint32_t value, uint32_t low, uint32_t high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

int32_t clamp_i32(int32_t value, int32_t low, int32_t high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

uint32_t elapsed_us(const Relay* relay, int64_t start, int64_t end) {
    if (end <= start || relay->qpc_frequency.QuadPart <= 0) return 0;
    const uint64_t ticks = static_cast<uint64_t>(end - start);
    const uint64_t micros =
        (ticks * 1000000ULL) / static_cast<uint64_t>(relay->qpc_frequency.QuadPart);
    return static_cast<uint32_t>(micros > UINT32_MAX ? UINT32_MAX : micros);
}

void wait_for_frame_slot(Relay* relay) {
    if (relay->last_inject_qpc == 0 || relay->min_frame_interval_us == 0) return;
    const int64_t interval_ticks =
        (relay->qpc_frequency.QuadPart * relay->min_frame_interval_us) / 1000000LL;
    const int64_t deadline = relay->last_inject_qpc + interval_ticks;
    LARGE_INTEGER now{};
    QueryPerformanceCounter(&now);
    if (now.QuadPart >= deadline) return;

    // Yield once when another runnable thread can usefully run, then keep the
    // final sub-millisecond interval precise. A normal Sleep(1) overshoots the
    // pointer-frame separation by almost a full millisecond on many systems.
    SwitchToThread();
    do {
        YieldProcessor();
        QueryPerformanceCounter(&now);
    } while (now.QuadPart < deadline);
}

void fill_pointer_info(Relay* relay, const RelayReport& report, POINTER_TYPE_INFO* info) {
    ZeroMemory(info, sizeof(*info));
    info->type = PT_PEN;
    POINTER_PEN_INFO& pen = info->penInfo;
    POINTER_INFO& pointer = pen.pointerInfo;
    pointer.pointerType = PT_PEN;
    pointer.pointerId = 1;
    pointer.pointerFlags = report.flags;
    pointer.ptPixelLocation.x = report.x;
    pointer.ptPixelLocation.y = report.y;
    pointer.ptPixelLocationRaw = pointer.ptPixelLocation;
    pointer.ptHimetricLocation.x =
        static_cast<LONG>((static_cast<int64_t>(report.x) * 2540LL) / relay->dpi);
    pointer.ptHimetricLocation.y =
        static_cast<LONG>((static_cast<int64_t>(report.y) * 2540LL) / relay->dpi);
    pointer.ptHimetricLocationRaw = pointer.ptHimetricLocation;
    pointer.historyCount = 1;
    pointer.dwKeyStates =
        (report.flags & POINTER_FLAG_FIRSTBUTTON) != 0 ? MK_LBUTTON : 0;
    if ((report.flags & POINTER_FLAG_DOWN) != 0) {
        pointer.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_DOWN;
    } else if ((report.flags & POINTER_FLAG_UP) != 0) {
        pointer.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_UP;
    } else {
        pointer.ButtonChangeType = POINTER_CHANGE_NONE;
    }
    pen.penMask = PEN_MASK_PRESSURE;
    pen.pressure = clamp_u32(report.pressure, 0, 1024);
    if (report.tilt_enabled != 0) pen.penMask |= PEN_MASK_TILT_X;
    pen.tiltX = clamp_i32(report.tilt_x, -90, 90);
}

bool pop_report(Relay* relay, RelayReport* report) {
    bool have_report = false;
    EnterCriticalSection(&relay->lock);
    if (relay->count != 0) {
        *report = relay->queue[relay->head];
        relay->head = (relay->head + 1) % kQueueCapacity;
        --relay->count;
        have_report = true;
    }
    LeaveCriticalSection(&relay->lock);
    return have_report;
}

void push_completion(Relay* relay, const RelayCompletion& completion) {
    if (relay->completion_count == kCompletionCapacity) {
        relay->completion_head = (relay->completion_head + 1) % kCompletionCapacity;
        --relay->completion_count;
        ++relay->stats.completion_dropped;
    }
    const uint32_t tail =
        (relay->completion_head + relay->completion_count) % kCompletionCapacity;
    relay->completions[tail] = completion;
    ++relay->completion_count;
}

DWORD WINAPI relay_thread_main(void* raw_relay) {
    Relay* relay = static_cast<Relay*>(raw_relay);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);

    for (;;) {
        WaitForSingleObject(relay->wake_event, INFINITE);
        RelayReport report{};
        while (pop_report(relay, &report)) {
            LARGE_INTEGER before{};
            LARGE_INTEGER after{};
            POINTER_TYPE_INFO info{};
            fill_pointer_info(relay, report, &info);
            BOOL ok = FALSE;
            DWORD error = ERROR_SUCCESS;
            for (uint32_t attempt = 0; attempt < 3; ++attempt) {
                wait_for_frame_slot(relay);
                if (attempt == 0) QueryPerformanceCounter(&before);
                SetLastError(ERROR_SUCCESS);
                ok = InjectSyntheticPointerInput(relay->device, &info, 1);
                error = ok ? ERROR_SUCCESS : GetLastError();
                QueryPerformanceCounter(&after);
                relay->last_inject_qpc = after.QuadPart;
                if (ok || error != ERROR_NOT_READY) break;
            }

            const uint32_t queue_delay = elapsed_us(
                relay, report.submitted_qpc, before.QuadPart
            );
            const uint32_t inject_time = elapsed_us(
                relay, before.QuadPart, after.QuadPart
            );
            EnterCriticalSection(&relay->lock);
            if (ok) {
                ++relay->stats.injected;
            } else {
                ++relay->stats.failed;
                relay->fatal_error = error == ERROR_SUCCESS ? ERROR_GEN_FAILURE : error;
                relay->stats.last_error = relay->fatal_error;
            }
            relay->stats.total_queue_delay_us += queue_delay;
            relay->stats.total_inject_call_us += inject_time;
            relay->stats.last_queue_delay_us = queue_delay;
            relay->stats.last_inject_call_us = inject_time;
            if (queue_delay > relay->stats.max_queue_delay_us) {
                relay->stats.max_queue_delay_us = queue_delay;
            }
            if (inject_time > relay->stats.max_inject_call_us) {
                relay->stats.max_inject_call_us = inject_time;
            }
            push_completion(
                relay,
                RelayCompletion{
                    report.token,
                    static_cast<uint64_t>(report.submitted_qpc),
                    static_cast<uint64_t>(before.QuadPart),
                    static_cast<uint64_t>(after.QuadPart),
                    static_cast<uint64_t>(relay->qpc_frequency.QuadPart),
                    report.flags,
                    report.x,
                    report.y,
                    report.pressure,
                    ok ? 1U : 0U,
                    error,
                    queue_delay,
                    inject_time,
                }
            );
            if (relay->count == 0) SetEvent(relay->idle_event);
            LeaveCriticalSection(&relay->lock);
        }

        EnterCriticalSection(&relay->lock);
        const bool should_stop = relay->stopping && relay->count == 0;
        LeaveCriticalSection(&relay->lock);
        if (should_stop) return 0;
    }
}

LRESULT CALLBACK input_hook_proc(int code, WPARAM w_param, LPARAM l_param) {
    InputCapture* capture = static_cast<InputCapture*>(
        InterlockedCompareExchangePointer(
            reinterpret_cast<PVOID volatile*>(&g_input_capture),
            nullptr,
            nullptr
        )
    );
    if (code == HC_ACTION && capture != nullptr && w_param == WM_MOUSEMOVE) {
        const MSLLHOOKSTRUCT* info = reinterpret_cast<const MSLLHOOKSTRUCT*>(l_param);
        LARGE_INTEGER observed{};
        QueryPerformanceCounter(&observed);
        EnterCriticalSection(&capture->lock);
        if (capture->count == kInputMoveCapacity) {
            capture->head = (capture->head + 1) % kInputMoveCapacity;
            --capture->count;
            ++capture->stats.dropped;
        }
        const uint32_t tail = (capture->head + capture->count) % kInputMoveCapacity;
        capture->moves[tail] = InputMove{
            info->pt.x,
            info->pt.y,
            info->flags,
            info->time,
            static_cast<uint64_t>(observed.QuadPart),
            static_cast<uint64_t>(capture->qpc_frequency.QuadPart),
        };
        ++capture->count;
        ++capture->stats.captured;
        if (capture->count > capture->stats.max_queue_depth) {
            capture->stats.max_queue_depth = capture->count;
        }
        LeaveCriticalSection(&capture->lock);
    }
    return CallNextHookEx(capture != nullptr ? capture->hook : nullptr, code, w_param, l_param);
}

DWORD WINAPI input_thread_main(void* raw_capture) {
    InputCapture* capture = static_cast<InputCapture*>(raw_capture);
    capture->thread_id = GetCurrentThreadId();
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);

    // Force creation of this thread's message queue before create() can post
    // WM_QUIT during an early shutdown.
    MSG message{};
    PeekMessageW(&message, nullptr, WM_USER, WM_USER, PM_NOREMOVE);

    HMODULE module = nullptr;
    GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&input_hook_proc),
        &module
    );
    capture->hook = SetWindowsHookExW(WH_MOUSE_LL, input_hook_proc, module, 0);
    capture->startup_error = capture->hook != nullptr ? ERROR_SUCCESS : GetLastError();
    capture->stats.last_error = capture->startup_error;
    SetEvent(capture->ready_event);
    if (capture->hook == nullptr) return 0;

    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    UnhookWindowsHookEx(capture->hook);
    capture->hook = nullptr;
    return 0;
}

}  // namespace

extern "C" __declspec(dllexport) uint32_t __cdecl mp_synth_api_version() {
    return kApiVersion;
}

extern "C" __declspec(dllexport) void* __cdecl mp_input_create(
    uint32_t* error_out
) {
    if (error_out != nullptr) *error_out = ERROR_SUCCESS;
    InputCapture* capture = static_cast<InputCapture*>(
        HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(InputCapture))
    );
    if (capture == nullptr) {
        if (error_out != nullptr) *error_out = ERROR_OUTOFMEMORY;
        return nullptr;
    }
    InitializeCriticalSection(&capture->lock);
    QueryPerformanceFrequency(&capture->qpc_frequency);
    capture->stats.struct_size = sizeof(InputCaptureStats);
    capture->stats.api_version = kApiVersion;
    capture->stats.qpc_frequency = static_cast<uint64_t>(
        capture->qpc_frequency.QuadPart
    );
    capture->ready_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (capture->ready_event == nullptr) {
        const DWORD error = GetLastError();
        DeleteCriticalSection(&capture->lock);
        HeapFree(GetProcessHeap(), 0, capture);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    if (InterlockedCompareExchangePointer(
            reinterpret_cast<PVOID volatile*>(&g_input_capture),
            capture,
            nullptr
        ) != nullptr) {
        CloseHandle(capture->ready_event);
        DeleteCriticalSection(&capture->lock);
        HeapFree(GetProcessHeap(), 0, capture);
        if (error_out != nullptr) *error_out = ERROR_ALREADY_EXISTS;
        return nullptr;
    }
    capture->thread = CreateThread(nullptr, 0, input_thread_main, capture, 0, nullptr);
    if (capture->thread == nullptr) {
        const DWORD error = GetLastError();
        InterlockedCompareExchangePointer(
            reinterpret_cast<PVOID volatile*>(&g_input_capture),
            nullptr,
            capture
        );
        CloseHandle(capture->ready_event);
        DeleteCriticalSection(&capture->lock);
        HeapFree(GetProcessHeap(), 0, capture);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    const DWORD wait = WaitForSingleObject(capture->ready_event, 2000);
    if (wait != WAIT_OBJECT_0 || capture->startup_error != ERROR_SUCCESS) {
        const DWORD error = wait == WAIT_OBJECT_0
            ? capture->startup_error
            : ERROR_TIMEOUT;
        if (capture->thread_id != 0) PostThreadMessageW(capture->thread_id, WM_QUIT, 0, 0);
        WaitForSingleObject(capture->thread, 2000);
        InterlockedCompareExchangePointer(
            reinterpret_cast<PVOID volatile*>(&g_input_capture),
            nullptr,
            capture
        );
        CloseHandle(capture->thread);
        CloseHandle(capture->ready_event);
        DeleteCriticalSection(&capture->lock);
        HeapFree(GetProcessHeap(), 0, capture);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    return capture;
}

extern "C" __declspec(dllexport) uint32_t __cdecl mp_input_drain_moves(
    void* raw_capture,
    InputMove* moves_out,
    uint32_t capacity
) {
    if (raw_capture == nullptr || moves_out == nullptr || capacity == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    InputCapture* capture = static_cast<InputCapture*>(raw_capture);
    EnterCriticalSection(&capture->lock);
    const uint32_t count = capture->count < capacity ? capture->count : capacity;
    for (uint32_t index = 0; index < count; ++index) {
        moves_out[index] = capture->moves[capture->head];
        capture->head = (capture->head + 1) % kInputMoveCapacity;
        --capture->count;
    }
    capture->stats.drained += count;
    LeaveCriticalSection(&capture->lock);
    return count;
}

extern "C" __declspec(dllexport) int __cdecl mp_input_get_stats(
    void* raw_capture,
    InputCaptureStats* stats_out,
    uint32_t stats_size
) {
    if (
        raw_capture == nullptr || stats_out == nullptr ||
        stats_size < sizeof(InputCaptureStats)
    ) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    InputCapture* capture = static_cast<InputCapture*>(raw_capture);
    EnterCriticalSection(&capture->lock);
    *stats_out = capture->stats;
    LeaveCriticalSection(&capture->lock);
    return 1;
}

extern "C" __declspec(dllexport) void __cdecl mp_input_destroy(void* raw_capture) {
    if (raw_capture == nullptr) return;
    InputCapture* capture = static_cast<InputCapture*>(raw_capture);
    if (capture->thread_id != 0) {
        PostThreadMessageW(capture->thread_id, WM_QUIT, 0, 0);
    }
    WaitForSingleObject(capture->thread, INFINITE);
    InterlockedCompareExchangePointer(
        reinterpret_cast<PVOID volatile*>(&g_input_capture),
        nullptr,
        capture
    );
    CloseHandle(capture->thread);
    CloseHandle(capture->ready_event);
    DeleteCriticalSection(&capture->lock);
    HeapFree(GetProcessHeap(), 0, capture);
}

extern "C" __declspec(dllexport) void* __cdecl mp_synth_create(
    uint32_t min_frame_interval_us,
    uint32_t* error_out
) {
    if (error_out != nullptr) *error_out = ERROR_SUCCESS;
    Relay* relay = static_cast<Relay*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Relay)));
    if (relay == nullptr) {
        if (error_out != nullptr) *error_out = ERROR_OUTOFMEMORY;
        return nullptr;
    }
    InitializeCriticalSection(&relay->lock);
    relay->min_frame_interval_us = min_frame_interval_us;
    relay->dpi = 96;
    const UINT dpi = GetDpiForSystem();
    if (dpi != 0) relay->dpi = dpi;
    QueryPerformanceFrequency(&relay->qpc_frequency);
    relay->stats.struct_size = sizeof(RelayStats);
    relay->stats.api_version = kApiVersion;
    relay->stats.qpc_frequency = static_cast<uint64_t>(relay->qpc_frequency.QuadPart);

    relay->device = CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT);
    if (relay->device == nullptr) {
        const DWORD error = GetLastError();
        DeleteCriticalSection(&relay->lock);
        HeapFree(GetProcessHeap(), 0, relay);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    relay->wake_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    relay->idle_event = CreateEventW(nullptr, TRUE, TRUE, nullptr);
    if (relay->wake_event == nullptr || relay->idle_event == nullptr) {
        const DWORD error = GetLastError();
        if (relay->wake_event != nullptr) CloseHandle(relay->wake_event);
        if (relay->idle_event != nullptr) CloseHandle(relay->idle_event);
        DestroySyntheticPointerDevice(relay->device);
        DeleteCriticalSection(&relay->lock);
        HeapFree(GetProcessHeap(), 0, relay);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    relay->thread = CreateThread(nullptr, 0, relay_thread_main, relay, 0, nullptr);
    if (relay->thread == nullptr) {
        const DWORD error = GetLastError();
        CloseHandle(relay->wake_event);
        CloseHandle(relay->idle_event);
        DestroySyntheticPointerDevice(relay->device);
        DeleteCriticalSection(&relay->lock);
        HeapFree(GetProcessHeap(), 0, relay);
        if (error_out != nullptr) *error_out = error;
        return nullptr;
    }
    return relay;
}

extern "C" __declspec(dllexport) int __cdecl mp_synth_submit(
    void* raw_relay,
    uint32_t flags,
    int32_t x,
    int32_t y,
    uint32_t pressure,
    int32_t tilt_x,
    uint32_t tilt_enabled,
    uint64_t token
) {
    if (raw_relay == nullptr) {
        SetLastError(ERROR_INVALID_HANDLE);
        return 0;
    }
    Relay* relay = static_cast<Relay*>(raw_relay);
    LARGE_INTEGER submitted{};
    QueryPerformanceCounter(&submitted);

    EnterCriticalSection(&relay->lock);
    if (relay->stopping) {
        LeaveCriticalSection(&relay->lock);
        SetLastError(ERROR_OPERATION_ABORTED);
        return 0;
    }
    if (relay->fatal_error != ERROR_SUCCESS) {
        const DWORD error = relay->fatal_error;
        LeaveCriticalSection(&relay->lock);
        SetLastError(error);
        return 0;
    }
    if (relay->count == kQueueCapacity) {
        ++relay->stats.queue_full;
        LeaveCriticalSection(&relay->lock);
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return 0;
    }
    const uint32_t tail = (relay->head + relay->count) % kQueueCapacity;
    relay->queue[tail] = RelayReport{
        flags,
        x,
        y,
        pressure,
        tilt_x,
        tilt_enabled,
        token,
        submitted.QuadPart,
    };
    ++relay->count;
    ++relay->stats.submitted;
    if (relay->count > relay->stats.max_queue_depth) {
        relay->stats.max_queue_depth = relay->count;
    }
    ResetEvent(relay->idle_event);
    LeaveCriticalSection(&relay->lock);
    SetEvent(relay->wake_event);
    return 1;
}

extern "C" __declspec(dllexport) int __cdecl mp_synth_submit_batch(
    void* raw_relay,
    const RelayInputReport* reports,
    uint32_t report_count
) {
    if (raw_relay == nullptr || reports == nullptr || report_count == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    if (report_count > kQueueCapacity) {
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return 0;
    }
    Relay* relay = static_cast<Relay*>(raw_relay);
    LARGE_INTEGER submitted{};
    QueryPerformanceCounter(&submitted);

    EnterCriticalSection(&relay->lock);
    if (relay->stopping) {
        LeaveCriticalSection(&relay->lock);
        SetLastError(ERROR_OPERATION_ABORTED);
        return 0;
    }
    if (relay->fatal_error != ERROR_SUCCESS) {
        const DWORD error = relay->fatal_error;
        LeaveCriticalSection(&relay->lock);
        SetLastError(error);
        return 0;
    }
    if (report_count > kQueueCapacity - relay->count) {
        ++relay->stats.queue_full;
        LeaveCriticalSection(&relay->lock);
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return 0;
    }
    for (uint32_t index = 0; index < report_count; ++index) {
        const RelayInputReport& input = reports[index];
        const uint32_t tail = (relay->head + relay->count) % kQueueCapacity;
        relay->queue[tail] = RelayReport{
            input.flags,
            input.x,
            input.y,
            input.pressure,
            input.tilt_x,
            input.tilt_enabled,
            input.token,
            submitted.QuadPart,
        };
        ++relay->count;
        ++relay->stats.submitted;
    }
    if (relay->count > relay->stats.max_queue_depth) {
        relay->stats.max_queue_depth = relay->count;
    }
    ResetEvent(relay->idle_event);
    LeaveCriticalSection(&relay->lock);
    SetEvent(relay->wake_event);
    return 1;
}

extern "C" __declspec(dllexport) uint32_t __cdecl mp_synth_drain_completions(
    void* raw_relay,
    RelayCompletion* completions_out,
    uint32_t capacity
) {
    if (raw_relay == nullptr || completions_out == nullptr || capacity == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    Relay* relay = static_cast<Relay*>(raw_relay);
    EnterCriticalSection(&relay->lock);
    const uint32_t count = relay->completion_count < capacity
        ? relay->completion_count
        : capacity;
    for (uint32_t index = 0; index < count; ++index) {
        completions_out[index] = relay->completions[relay->completion_head];
        relay->completion_head = (relay->completion_head + 1) % kCompletionCapacity;
        --relay->completion_count;
    }
    LeaveCriticalSection(&relay->lock);
    return count;
}

extern "C" __declspec(dllexport) int __cdecl mp_synth_wait_idle(
    void* raw_relay,
    uint32_t timeout_ms
) {
    if (raw_relay == nullptr) return 0;
    Relay* relay = static_cast<Relay*>(raw_relay);
    return WaitForSingleObject(relay->idle_event, timeout_ms) == WAIT_OBJECT_0 ? 1 : 0;
}

extern "C" __declspec(dllexport) int __cdecl mp_synth_get_stats(
    void* raw_relay,
    RelayStats* stats_out,
    uint32_t stats_size
) {
    if (raw_relay == nullptr || stats_out == nullptr || stats_size < sizeof(RelayStats)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return 0;
    }
    Relay* relay = static_cast<Relay*>(raw_relay);
    EnterCriticalSection(&relay->lock);
    *stats_out = relay->stats;
    LeaveCriticalSection(&relay->lock);
    return 1;
}

extern "C" __declspec(dllexport) void __cdecl mp_synth_destroy(void* raw_relay) {
    if (raw_relay == nullptr) return;
    Relay* relay = static_cast<Relay*>(raw_relay);
    EnterCriticalSection(&relay->lock);
    relay->stopping = true;
    LeaveCriticalSection(&relay->lock);
    SetEvent(relay->wake_event);
    WaitForSingleObject(relay->thread, INFINITE);
    DestroySyntheticPointerDevice(relay->device);
    CloseHandle(relay->thread);
    CloseHandle(relay->wake_event);
    CloseHandle(relay->idle_event);
    DeleteCriticalSection(&relay->lock);
    HeapFree(GetProcessHeap(), 0, relay);
}
