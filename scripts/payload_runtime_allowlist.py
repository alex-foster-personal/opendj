"""The engine payload's runtime-load allowlist, as data.

Split out of :mod:`scripts.build_engine_payload` so the builder stays under
the quality ratchet's file-size ceiling. The builder imports
:data:`RUNTIME_LOAD_ALLOWLIST` from here, adds the beatgrid runner's entries,
and is the only consumer; the classification logic, the stale-entry guard
and the key formats are documented beside it there.
"""

from __future__ import annotations

# Why no GPU backend load in pylib's torch can run: shared by the vocals
# torch entries below, and true of the wheel itself, not of how it is called.
_NO_TORCH_GPU = (
    "the arm64 macOS torch wheel reports torch.version.cuda and torch.version.hip "
    "as None and torch.cuda.is_available() False (measured in the payload, issue "
    "#4053), so no CUDA, ROCm or XPU backend is ever selected"
)

# Every runtime-loaded library the payload is allowed to name, keyed by the
# library string, with the reason it cannot strand a tester. Anything the
# scan finds that is not here fails the build.
#
# The keys are the LITERAL argument. Sites whose argument is dynamic are
# keyed by "<relative path>:<line>" instead, because there is no library
# name to reason about, only a call site.
RUNTIME_LOAD_ALLOWLIST: dict[str, str] = {
    "c": (
        "libc / libSystem is present on every macOS install by definition; "
        "there is no machine where a bundle runs and libc does not."
    ),
    "System": "same as libc: /usr/lib/libSystem.B.dylib is part of macOS.",
    "objc": "/usr/lib/libobjc.A.dylib is part of macOS.",
    "SystemConfiguration": "an OS framework under /System/Library.",
    "CoreFoundation": "an OS framework under /System/Library.",
    "CoreServices": "an OS framework under /System/Library.",
    "Foundation": "an OS framework under /System/Library.",
    "AppKit": "an OS framework under /System/Library.",
    "Cocoa": "an OS framework under /System/Library.",
    "Quartz": "an OS framework under /System/Library.",
    "libc.dylib": "an absolute-name spelling of libc; see 'c'.",
    "libSystem.dylib": "an absolute-name spelling of libSystem; see 'System'.",
    "/usr/lib/libSystem.B.dylib": (
        "the absolute path of libSystem, loaded by apps/diagnostics/probe_native_metrics.py "
        "for mach_timebase_info; part of every macOS install, see 'System'."
    ),
    "/usr/lib/libproc.dylib": (
        "the macOS process-info library (proc_pid_rusage), loaded by "
        "apps/diagnostics/probe_native_metrics.py behind a platform.system() == 'Darwin' "
        "guard; it ships in the dyld shared cache of every macOS install."
    ),
    "AudioToolbox": (
        "an OS framework under /System/Library, loaded by audioread's macOS "
        "decoder (librosa's fallback when soundfile cannot read a file)."
    ),
    "sndfile": (
        "the SYSTEM libsndfile, which is soundfile.py's SECOND choice. Its "
        "first is the copy bundled in the wheel, and that copy ships: the "
        "payload carries pylib/_soundfile_data/libsndfile_arm64.dylib, and "
        "importing soundfile under the payload interpreter with an empty "
        "environment and sys.path limited to pylib reports "
        "__libsndfile_version__ 1.2.2 without ever setting _libname, which is "
        "the attribute the find_library branch assigns. A tester with no "
        "Homebrew libsndfile never reaches this line."
    ),
    "libc.so.6": (
        "the Linux libc soname, loaded only inside apps/engine_core/parent_watch.py's "
        "prctl(PR_SET_PDEATHSIG) branch, which returns first unless sys.platform starts "
        "with 'linux', and in torch.distributed.elastic's redirects.py, whose get_libc() "
        "returns None on macOS before reaching it (the other match there is a docstring "
        "example); a macOS bundle never reaches either call."
    ),
    "kernel32": (
        "Windows kernel DLL loaded via ctypes.WinDLL/CDLL in filelock's "
        "sys.platform == 'win32' branch, torch inductor's is_windows() dlclose "
        "path and torch.distributed.elastic redirects.py's IS_WINDOWS block; the "
        "arm64 macOS bundle runs with sys.platform 'darwin', so no such branch "
        "executes."
    ),
    "kernel32.dll": (
        "Windows kernel DLL preloaded by torch's _load_dll_libraries(), which is "
        "defined only inside torch/__init__.py's sys.platform == 'win32' guard; "
        "darwin never enters that block."
    ),
    "vcruntime140.dll": (
        "Windows MSVC runtime preloaded inside torch's win32-only _load_dll_libraries() "
        "before importing native extensions; unreachable on darwin arm64."
    ),
    "msvcp140.dll": (
        "Windows MSVC C++ runtime preloaded inside torch's win32-only "
        "_load_dll_libraries(); unreachable on darwin arm64."
    ),
    "vcruntime140_1.dll": (
        "Windows MSVC supplemental runtime preloaded inside torch's win32-only "
        "_load_dll_libraries(); unreachable on darwin arm64."
    ),
    "libc.so": (
        "Linux/Alpine libc soname used by torch inductor DLLWrapper._dlclose only "
        "inside is_linux() when CDLL(None) lacks dlclose; inductor JIT compile paths "
        "that instantiate DLLWrapper are not exercised on darwin arm64."
    ),
    "/usr/lib64/libgomp.so.1": (
        "Linux OpenMP soname loaded by torch inductor CppCodeCache only in a gomp "
        "ImportError fallback guarded by os.path.exists('/usr/lib64/libgomp.so.1'); "
        "that fbcode/buck workaround never runs on darwin arm64."
    ),
    "libnvidia-ml.so.1": (
        "NVIDIA management library for NVML GPU discovery in torch.cuda; loaded only "
        "when CUDA device enumeration runs. There is no CUDA stack in the shipped "
        "arm64 macOS product."
    ),
    "__lib_path__": (
        "not a runtime library name: a placeholder inside torch inductor "
        "cpu_vec_isa.py's _avx_py_load codegen template string, substituted before "
        "the generated probe runs; the literal line is never executed during import "
        "on darwin."
    ),
    "pylib/filelock/_identity.py:162": (
        "CDLL(None) is dlopen(NULL): it binds sysctl symbols out of the process "
        "image already loaded on macOS (libSystem), inside filelock's "
        "sys.platform == 'darwin' branch; no filesystem search."
    ),
    # pylib's torch is the `vocals` extra's pin (pyproject.toml), used only by
    # the Demucs vocal worker. Dynamic sites are keyed by LINE, so any torch
    # bump moves them and fails the build until each is re-read here; an
    # entry whose line no longer holds a load site fails it too (stale).
    # Measured on torch 2.13.0 (issue #4053): a real vocal separation under
    # the payload interpreter dlopens exactly libtorch_global_deps.dylib
    # (__init__.py:380) and torchaudio's two abi3 extensions (_ops.py:1516),
    # all payload-relative, and imports no torch._inductor, profiler._cupti
    # or distributed.elastic module.
    "pylib/torch/__init__.py:327": (
        "torch 2.13 _preload_cuda_lib loads a CUDA wheel soname found under "
        "sys.path's nvidia/ subtree; it raises unless platform.system() == 'Linux'."
    ),
    "pylib/torch/__init__.py:380": (
        "torch 2.13 _load_global_deps loads libtorch_global_deps.dylib from "
        "torch/lib beside __file__: a payload-relative path, not a system search."
    ),
    "pylib/torch/__init__.py:403": (
        "torch 2.13 retry of libtorch_global_deps in the OSError branch, reached "
        "only when _preload_cuda_deps does not re-raise, i.e. only for a missing "
        "CUDA soname; same payload-relative path as line 380."
    ),
    "pylib/torch/_inductor/codecache.py:3658": (
        "torch 2.13 inductor CppCodeCache loads a library it JIT-compiled; demucs "
        "and the vocal worker never call torch.compile, so torch._inductor is "
        "never imported."
    ),
    "pylib/torch/_inductor/codecache.py:4727": (
        "torch 2.13 inductor DLLWrapper opens a library it compiled; unreachable "
        "without torch.compile, which the vocal worker never calls."
    ),
    "pylib/torch/_inductor/codecache.py:4748": (
        "torch 2.13 CDLL(None) in DLLWrapper._dlclose's is_linux() branch: "
        "dlopen(NULL), no filesystem search, and unreachable on darwin."
    ),
    "pylib/torch/_inductor/cpp_builder.py:1430": (
        "torch 2.13 perload_clang_libomp_win, called only from inductor's "
        "_IS_WINDOWS OpenMP setup branch; darwin never enters it."
    ),
    "pylib/torch/_inductor/cpp_builder.py:1446": (
        "torch 2.13 perload_icx_libomp_win, called only from the same "
        "_IS_WINDOWS branch; darwin never enters it."
    ),
    "pylib/torch/_ops.py:1516": (
        "torch 2.13 torch.ops.load_library loads the caller's path after "
        "os.path.realpath; the vocal worker reaches it only through torchaudio's "
        "_extension loader, which names its own payload-relative "
        "torchaudio/lib/*.abi3.so, not a system search."
    ),
    "pylib/torch/cuda/__init__.py:93": (
        "torch 2.13 ROCm amdsmi CDLL hook definition, built only when "
        f"torch.version.hip is set; {_NO_TORCH_GPU}."
    ),
    "pylib/torch/cuda/__init__.py:99": f"torch 2.13 ROCm amdsmi CDLL hook; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/__init__.py:102": f"torch 2.13 ROCm amdsmi CDLL hook; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/_utils.py:30": f"torch 2.13 ROCm HIP runtime via rocm_sdk; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/_utils.py:33": f"torch 2.13 Windows ROCm HIP runtime; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/_utils.py:107": f"torch 2.13 ROCm hiprtc via rocm_sdk; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/_utils.py:113": f"torch 2.13 Windows ROCm hiprtc; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/_utils.py:144": f"torch 2.13 NVRTC for CUDA kernel JIT; {_NO_TORCH_GPU}.",
    "pylib/torch/cuda/memory.py:1246": (
        "torch 2.13 CUDAPluggableAllocator loads a caller-named allocator; "
        f"nothing in the payload constructs one, and {_NO_TORCH_GPU}."
    ),
    "pylib/torch/distributed/elastic/multiprocessing/redirects.py:40": (
        "torch 2.13 Windows C runtime inside get_libc's IS_WINDOWS branch, which "
        "returns None on macOS first; the vocal worker never imports it."
    ),
    "pylib/torch/profiler/_cupti_monitor.py:293": (
        f"torch 2.13 CUPTI for the CUDA profiler; never imported, and {_NO_TORCH_GPU}."
    ),
    "pylib/torch/profiler/_cupti_monitor.py:1101": (
        f"torch 2.13 CUPTI hardware-trace enable; never imported, and {_NO_TORCH_GPU}."
    ),
    "pylib/torch/xpu/memory.py:513": (
        "torch 2.13 XPU pluggable allocator loads a caller-named library; nothing "
        f"in the payload constructs one, and {_NO_TORCH_GPU}."
    ),
}
