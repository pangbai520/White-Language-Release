/* The portable package has no C SDK. Keep this host freestanding so a release
   can check its DLL ABI without downloading another compiler installation. */
__declspec(dllimport) void *__stdcall GetStdHandle(unsigned long kind);
__declspec(dllimport) int __stdcall WriteFile(void *handle, const void *bytes,
    unsigned long length, unsigned long *written, void *overlapped);
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(unsigned int code);

extern int add(int a, int b);
extern int factorial(int n);
extern double multiply_float(double a, double b);

void mainCRTStartup(void) {
    int ok = add(5, 7) == 12 && factorial(4) == 24 && multiply_float(1.5, 4.0) == 6.0;
    static const char passed[] = "PASS: C to White Language FFI calls\n";
    static const char failed[] = "FAIL: C to White Language FFI result\n";
    const char *message = ok ? passed : failed;
    unsigned long length = ok ? sizeof(passed) - 1 : sizeof(failed) - 1;
    unsigned long written = 0;
    if (!WriteFile(GetStdHandle((unsigned long)-11), message, length, &written, 0) || written != length) {
        ExitProcess(2);
    }
    ExitProcess(ok ? 0 : 1);
}
