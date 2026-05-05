#!/usr/bin/env python3
"""Relink: 用用户自己的 Qt 把 lgpl_dist 里的二进制重新组合成可运行的发行包.

用法 (在 lgpl_dist/ 内):
    pixi run relink <platform> --arch <arch> --qt-dir <Qt 版本根>
    pixi run relink --clear        # 删整个 lgpl_dist/relink/

示例:
    pixi run relink macos   --arch arm64       --qt-dir ~/Qt/6.8.3
    pixi run relink ios     --arch arm64       --qt-dir ~/Qt/6.8.3
    pixi run relink android --arch arm64-v8a   --qt-dir ~/Qt/6.8.3
    pixi run relink windows --arch x86_64      --qt-dir C:/Qt/6.8.3

输出位置 (统一在 lgpl_dist/relink/ 下, 按 platform/arch 分目录):
    macos:   relink/macos/<arch>/<APP>.app/      (macdeployqt + ad-hoc 签名, 双击运行)
    ios:     relink/ios/<arch>/<APP>.app/        (静态重链; 然后开 xcodeproj 部署真机)
    android: relink/android/<abi>/...apk         (androiddeployqt + assembleRelease + debug-keystore)
    windows: relink/windows/<arch>/bin/          (windeployqt; 直接跑 .exe)

签名策略:
    macos:   ad-hoc (codesign --sign -). Apple Silicon 必须, 不需任何证书.
    ios:     用户在 Xcode 里选自己的 Apple ID Team (免费 7 天 / 付费一年).
    android: assembleRelease (release-grade APK, debuggable=false, 优化过)
             用 ~/.android/debug.keystore 签名. 不需正式 release 密钥也能 adb
             install. 想发 Google Play 自己改 build.gradle 的 signingConfig.
    windows: 不签 (SmartScreen 警告可绕过).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Qt path resolution
# ---------------------------------------------------------------------------

def resolve_qt_for_target(qt_dir: Path, plat: str, arch: str) -> Path:
    """qt_dir + plat + arch → Qt 平台/架构子目录 (含 bin/ 和 lib/ 的根).

    qt_dir 通常是 Qt 版本根, e.g. ~/Qt/6.8.3 or C:/Qt/6.8.3.
    也接受已经是平台子目录的路径 (如直接传 ~/Qt/6.8.3/macos).
    """
    candidates: list[Path] = []
    if plat == "macos":
        candidates = [qt_dir / "macos", qt_dir]
    elif plat == "ios":
        candidates = [qt_dir / "ios", qt_dir]
    elif plat == "windows":
        if arch == "x86_64":
            candidates = [qt_dir / "msvc2022_64", qt_dir]
        elif arch == "arm64":
            candidates = [qt_dir / "msvc2022_arm64", qt_dir]
    elif plat == "android":
        abi_to_dir = {
            "arm64-v8a":   "android_arm64_v8a",
            "armeabi-v7a": "android_armv7",
            "x86_64":      "android_x86_64",
            "x86":         "android_x86",
        }
        sub = abi_to_dir.get(arch)
        if sub:
            candidates = [qt_dir / sub, qt_dir]

    for c in candidates:
        if c.is_dir() and (c / "bin").is_dir():
            print(f"  Qt for {plat}/{arch}: {c}")
            return c
    sys.exit(f"ERROR: 找不到 {plat}/{arch} 对应的 Qt. 试过: "
             f"{[str(c) for c in candidates]}")


# ---------------------------------------------------------------------------
# macOS 名单式删除: GPL-only + 本应用不用的 Qt 模块
# ---------------------------------------------------------------------------

# Qt 6 里**只有 GPLv3 + Commercial 双授权**的模块, 不能进 LGPL 分发包.
# https://doc.qt.io/qt-6/qtvirtualkeyboard-licensing.html
GPL_ONLY_FRAMEWORKS_MAC = {"QtVirtualKeyboard", "QtVirtualKeyboardSettings"}

# 本应用不用的 Qt 3D 系列. macdeployqt 拷 plugin/qml 是按 category 整体复制,
# Scene3D / Scene2D 子模块顺手把整套 Qt 3D framework 也拽过来. 名单式删除,
# 比追 link 闭包稳 (闭包里 *StyleImpl 等会反过来引出意外保留).
UNUSED_FRAMEWORKS_MAC = {
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic",
    "Qt3DQuick", "Qt3DQuickScene2D", "Qt3DQuickScene3D", "Qt3DRender",
}


def _otool_qt_deps(binary: Path) -> set[str]:
    """otool -L <binary> 抽 Qt framework 名 (无后缀), 比如 {'QtCore','Qt3DCore'}."""
    if not binary.exists():
        return set()
    try:
        r = subprocess.run(["otool", "-L", str(binary)],
                           capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return set()
    return set(re.findall(r'(Qt\w+)\.framework', r.stdout))


def _macos_remove_blacklisted(app: Path) -> None:
    """按名单删 .app 里不要的 Qt framework + 引用方 (qml 子目录 / plugin 文件).

    名单 = GPL_ONLY_FRAMEWORKS_MAC ∪ UNUSED_FRAMEWORKS_MAC.
    流程:
      1. Frameworks/<X>.framework 名字命中 → 整目录删.
      2. qml/<...>/qmldir 同级 plugin .dylib otool 引用了名单 → 整 mod 目录删.
      3. PlugIns/<cat>/<plug>.dylib otool 引用了名单 → 删该文件.
      4. qml/.../VirtualKeyboard/ 名字兜底 (旧版 Qt 命名差异保险).
      5. 删空了的中间目录.
    """
    fw_dir = app / "Contents" / "Frameworks"
    qml_dir = app / "Contents" / "Resources" / "qml"
    plugins_dir = app / "Contents" / "PlugIns"
    blacklist = GPL_ONLY_FRAMEWORKS_MAC | UNUSED_FRAMEWORKS_MAC
    deleted: list[str] = []

    # 1. Frameworks/<X>.framework 名字命中
    if fw_dir.is_dir():
        for fw in sorted(fw_dir.iterdir()):
            if fw.is_dir() and fw.suffix == ".framework" and fw.stem in blacklist:
                shutil.rmtree(fw)
                deleted.append(f"Frameworks/{fw.name}")

    # 2. qml 模块: plugin .dylib 引用了名单 → 整 mod 目录删
    if qml_dir.is_dir():
        for qmldir_file in sorted(qml_dir.rglob("qmldir")):
            mod_dir = qmldir_file.parent
            if not mod_dir.is_dir():
                continue
            for plug in mod_dir.glob("*.dylib"):
                if _otool_qt_deps(plug) & blacklist:
                    rel = mod_dir.relative_to(qml_dir)
                    shutil.rmtree(mod_dir)
                    deleted.append(f"qml/{rel}/")
                    break

    # 3. PlugIns/<cat>/<plug>.dylib 引用了名单
    if plugins_dir.is_dir():
        for plug in sorted(plugins_dir.rglob("*.dylib")):
            if not plug.exists():
                continue
            if _otool_qt_deps(plug) & blacklist:
                rel = plug.relative_to(plugins_dir)
                plug.unlink()
                deleted.append(f"PlugIns/{rel}")

    # 4. 名字兜底: VirtualKeyboard 的 qml 子目录 (旧 Qt 版本里命名可能不在闭包).
    if qml_dir.is_dir():
        for vk in qml_dir.rglob("VirtualKeyboard"):
            if vk.is_dir():
                rel = vk.relative_to(qml_dir)
                shutil.rmtree(vk, ignore_errors=True)
                deleted.append(f"qml/{rel}/ (name match)")

    # 5. 收尾: 删空目录
    for root_dir in (plugins_dir, qml_dir):
        if not root_dir.is_dir():
            continue
        for d in sorted(root_dir.rglob("*"),
                        key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                rel = d.relative_to(root_dir)
                d.rmdir()
                deleted.append(f"{root_dir.name}/{rel}/ (empty)")

    if deleted:
        print(f"\n  Removed blacklisted Qt modules ({len(deleted)} entries):")
        for n in deleted:
            print(f"    - {n}")
    else:
        print(f"\n  (no blacklisted Qt modules to remove)")


def _clean_relink_dir(relink_dir: Path) -> None:
    """删 lgpl_dist/relink/<plat>/<arch>/ 下的旧产出 (如果有), 显式打印让用户看到."""
    if relink_dir.exists():
        rel = relink_dir.relative_to(SCRIPT_DIR)
        print(f"  Cleaning old relink output: rm -rf {rel}/")
        shutil.rmtree(relink_dir)


def _relink_dir_for(plat: str, arch: str) -> Path:
    """统一的 relink 输出位置: lgpl_dist/relink/<plat>/<arch>/.
    android 时 arch 是 abi (arm64-v8a 等)."""
    return SCRIPT_DIR / "relink" / plat / arch


def detect_qt_host_root(qt_version_root: Path) -> Path:
    """从 qt_version_root (e.g. ~/Qt/6.8.3) 找 host Qt: mac→macos, linux→gcc_64."""
    candidates = []
    if sys.platform == "darwin":
        candidates = ["macos"]
    elif sys.platform.startswith("linux"):
        candidates = ["gcc_64"]
    else:
        candidates = ["msvc2022_64"]
    for c in candidates:
        p = qt_version_root / c
        if p.is_dir() and (p / "bin").is_dir():
            return p
    sys.exit(f"ERROR: 找不到 Qt host ({candidates}) under {qt_version_root}")


# ---------------------------------------------------------------------------
# macos
# ---------------------------------------------------------------------------

def relink_macos(arch: str, qt_root: Path) -> None:
    """macdeployqt 拷 Qt frameworks/plugins 进 .app + ad-hoc 签名."""
    plat_dir = SCRIPT_DIR / "macos" / arch
    if not plat_dir.is_dir():
        sys.exit(f"ERROR: {plat_dir.relative_to(SCRIPT_DIR)} 不存在 "
                 "(在 mac 上跑过 pixi run lgpl 才会有)")

    apps = sorted(p for p in plat_dir.iterdir()
                  if p.is_dir() and p.name.endswith(".app"))
    if not apps:
        sys.exit(f"ERROR: 没找到 .app in {plat_dir}")
    src_app = apps[0]

    relink_dir = _relink_dir_for("macos", arch)
    _clean_relink_dir(relink_dir)
    relink_dir.mkdir(parents=True)
    dst_app = relink_dir / src_app.name
    shutil.copytree(src_app, dst_app, symlinks=True)
    print(f"  Copied: {src_app.name} → {relink_dir.relative_to(SCRIPT_DIR)}/{src_app.name}")

    macdeployqt = qt_root / "bin" / "macdeployqt"
    if not macdeployqt.exists():
        sys.exit(f"ERROR: macdeployqt not found at {macdeployqt}")

    cmd = [str(macdeployqt), str(dst_app),
           "-always-overwrite",
           # 跳过用 Apple 私有 API 的组件. QtVirtualKeyboard 是 GPLv3-only
           # (不是 LGPL) 而且用 iOS/macOS TextInput 私有 API, 这个 flag 会
           # 自动剔除它, 比写 hardcoded 黑名单干净.
           "-appstore-compliant",
           # 让 codesign 用 ad-hoc 签名. Apple Silicon 上未签名 .app 拒跑,
           # ad-hoc 不需任何证书.
           "-codesign=-"]
    # 用 lgpl 时生成的 stub Main.qml 让 qmlimportscanner 知道要 deploy 哪些 QML
    # 模块. 不传的话 scanner 扫 AOT 编译过的 QRC 总是空, QtQuick.Controls 等
    # 不会被 deploy, 运行时 "module ... not found" 直接挂.
    qml_stub = SCRIPT_DIR / "qml-imports-stub"
    if qml_stub.is_dir():
        cmd += [f"-qmldir={qml_stub}"]
        print(f"  using QML import stub: {qml_stub.relative_to(SCRIPT_DIR)}/")
    print(f"  macdeployqt {dst_app.relative_to(SCRIPT_DIR)} -appstore-compliant -codesign=-")
    subprocess.run(cmd, check=True)

    # 按名单删: GPL-only (QtVirtualKeyboard) + 本应用不用的 Qt 3D 全套.
    # macdeployqt 拷 plugin/qml 是按 category 整体复制, Scene3D/Scene2D 等
    # 顺手把整套 Qt3D framework 拽进来. 直接按名单删干净.
    _macos_remove_blacklisted(dst_app)

    print(f"\n=== Done ===")
    print(f"双击运行: {dst_app}")


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------

def relink_windows(arch: str, qt_root: Path) -> None:
    """windeployqt 拷 Qt DLL/plugins/qml 进 bin/."""
    plat_dir = SCRIPT_DIR / "windows" / arch
    if not plat_dir.is_dir():
        sys.exit(f"ERROR: {plat_dir.relative_to(SCRIPT_DIR)} 不存在 "
                 "(在 win11 上跑过 pixi run lgpl 才会有)")
    src_bin = plat_dir / "bin"
    if not src_bin.is_dir():
        sys.exit(f"ERROR: {src_bin} 不存在")

    relink_dir = _relink_dir_for("windows", arch)
    _clean_relink_dir(relink_dir)
    dst_bin = relink_dir / "bin"
    shutil.copytree(src_bin, dst_bin)
    print(f"  Copied: bin/ → {dst_bin.relative_to(SCRIPT_DIR)}/")

    # windeployqt 是 host 工具. arm64 交叉编译时, target Qt (msvc2022_arm64)
    # 的 bin/ 没 windeployqt.exe (host 工具只在 msvc2022_64). 找不到 target 的
    # 就 fallback 到 host x64 的, 但同时要让 host windeployqt 知道 target Qt
    # 在哪 — 通过 --qtpaths <target>/bin/qtpaths6.exe 或 --qmake <target>/bin/
    # qmake.exe (Qt 6 把 qtpaths/qmake 不一定都装齐, 哪个有就用哪个).
    target_bin = qt_root / "bin"
    target_windeployqt = target_bin / "windeployqt.exe"
    if target_windeployqt.exists():
        windeployqt = target_windeployqt
        qt_locator_arg: list[str] = []
        print(f"  windeployqt: target Qt's own ({windeployqt})")
    else:
        # fallback: 在 qt_root.parent (e.g. C:/Qt/6.8.3) 找 host msvc2022_64
        host_root = qt_root.parent / "msvc2022_64"
        host_bin = host_root / "bin"
        host_windeployqt = host_bin / "windeployqt.exe"
        if not host_windeployqt.exists():
            sys.exit(
                f"ERROR: windeployqt.exe 没找到.\n"
                f"  target Qt: {target_windeployqt} (不存在)\n"
                f"  host Qt fallback: {host_windeployqt} (也不存在)\n"
                f"  ARM64 交叉编译需要 Qt 同版本的 msvc2022_64 host 工具一并装."
            )
        # 找 target Qt 的定位器: qtpaths6.exe / qtpaths.exe / qmake.exe / qmake6.exe.
        # 给 host windeployqt 用 --qtpaths 或 --qmake 指过去.
        qt_locator_arg = []
        for name, flag in (("qtpaths6.exe", "--qtpaths"),
                           ("qtpaths.exe",  "--qtpaths"),
                           ("qmake6.exe",   "--qmake"),
                           ("qmake.exe",    "--qmake")):
            cand = target_bin / name
            if cand.exists():
                qt_locator_arg = [f"{flag}={cand}"]
                break
        if not qt_locator_arg:
            # ARM64 Qt 装时通常没附带任何 host 工具 (qtpaths/qmake 是 host 二进制,
            # ARM64 上跑不了). 走 sandbox 方案: 拷 host x64 的 qtpaths6.exe 到临时
            # 目录, 旁边丢一个 qt.conf 把 [Paths]/Prefix 重定向到 target ARM64 Qt
            # 根目录. windeployqt 调它时, 它用 qt.conf 报 ARM64 Qt 的 lib/plugins/
            # qml 路径; 但 qtpaths.exe 自己是 x64 能跑.
            host_qtpaths = None
            for n in ("qtpaths6.exe", "qtpaths.exe"):
                cand = host_bin / n
                if cand.exists():
                    host_qtpaths = cand
                    break
            if host_qtpaths is None:
                existing = sorted(p.name for p in target_bin.iterdir()
                                  if p.is_file()) if target_bin.is_dir() else []
                q_files = [n for n in existing
                           if n.lower().startswith("q") and n.lower().endswith(".exe")]
                sys.exit(
                    f"ERROR: target Qt 没装 qtpaths/qmake (ARM64 不带 host 工具),\n"
                    f"  且 host x64 也没找到 qtpaths6/qtpaths.exe.\n"
                    f"  target_bin: {target_bin}\n"
                    f"  target q*/Q*.exe: {q_files if q_files else '(全无)'}\n"
                    f"  host_bin: {host_bin}\n"
                    f"  装 Qt 时确保 'Qt 6.8.3 → MSVC 2022 64-bit' 也勾上."
                )
            sandbox = relink_dir / "_qtpaths_sandbox"
            sandbox.mkdir(parents=True, exist_ok=True)
            sb_qtpaths = sandbox / host_qtpaths.name
            shutil.copy2(host_qtpaths, sb_qtpaths)
            # 把 host x64 qtpaths 自身依赖的 Qt DLL 也拷一份到 sandbox.
            # 不拷的话 Windows DLL 搜索会沿 CWD (= dst_bin, 装着 ARM64 Qt6Core.dll)
            # 找上去, qtpaths6.exe (x64) 加载 ARM64 Qt6Core.dll 直接 0xc000007b 闪退.
            # 把 x64 Qt6Core.dll 放到 exe 同目录 (DLL 搜索第 1 顺位), 永远先命中.
            # qtpaths 只链 Qt6Core, 复制这一个就够; 其它 Qt6*.dll 顺手拷上 (如果将
            # 来 qtpaths 加链了也不会再翻车).
            for dll in host_bin.glob("Qt6*.dll"):
                if not (sandbox / dll.name).exists():
                    shutil.copy2(dll, sandbox / dll.name)
            # qt.conf 让 sandbox 里的 qtpaths 把 ARM64 Qt 当自己的 Prefix.
            # Qt 文档里 qt.conf 路径用正斜杠最稳, 反斜杠某些版本会被当转义.
            target_qt_fwd = str(qt_root.resolve()).replace("\\", "/")
            (sandbox / "qt.conf").write_text(
                f"[Paths]\nPrefix={target_qt_fwd}\n", encoding="utf-8")
            qt_locator_arg = [f"--qtpaths={sb_qtpaths}"]
            print(f"  qtpaths sandbox: {sandbox}")
            print(f"  → qt.conf Prefix = {target_qt_fwd}")
        windeployqt = host_windeployqt
        print(f"  windeployqt: host fallback ({windeployqt})")
        print(f"               {qt_locator_arg[0]} (target Qt)")

    exes = sorted(f for f in dst_bin.iterdir()
                  if f.suffix.lower() == ".exe"
                  and not f.name.lower().startswith(("qt", "windeployqt")))
    if not exes:
        sys.exit(f"ERROR: 没找到主 .exe in {dst_bin}")

    # plugin .dll (hotreload_plugin / dashboard_plugin / ...) 自身链的 Qt 模块
    # (Qt6Concurrent / Qt6WebSockets 等) 不会通过主 .exe 的 import table 透出,
    # windeployqt 默认只扫主 .exe, 那些 Qt 模块就漏了, 跑起来报
    # "Cannot load library xxx_plugin.dll: The specified module could not be
    # found". 把项目自带的 .dll (排除 Qt6*.dll 这些已经是 windeployqt 的输出)
    # 一起传给 windeployqt, 让它扫每一个的 import table.
    extra_bins = sorted(f for f in dst_bin.iterdir()
                        if f.suffix.lower() == ".dll"
                        and not f.name.lower().startswith("qt"))

    qml_stub = SCRIPT_DIR / "qml-imports-stub"
    qml_stub_arg = [f"--qmldir={qml_stub}"] if qml_stub.is_dir() else []
    if qml_stub_arg:
        print(f"  using QML import stub: {qml_stub.relative_to(SCRIPT_DIR)}/")

    targets = exes + extra_bins
    print(f"  windeployqt --release ({len(exes)} exe + {len(extra_bins)} dll)")
    for t in targets:
        print(f"    - {t.name}")
    subprocess.run([
        str(windeployqt), "--release",
        "--no-translations", "--no-system-d3d-compiler",
        "--no-compiler-runtime",
    ] + qt_locator_arg + qml_stub_arg + [str(t) for t in targets],
        check=True, cwd=str(dst_bin))

    print(f"\n=== Done ===")
    print(f"运行: {dst_bin}\\{exes[0].name}")


# ---------------------------------------------------------------------------
# ios
# ---------------------------------------------------------------------------

def relink_ios(arch: str, qt_root: Path) -> None:
    """clang++ 静态重链 .o + .a + Qt items → relink/<APP>.app, 然后开 Xcode 部署."""
    plat_dir = SCRIPT_DIR / "ios" / arch
    if not plat_dir.is_dir():
        sys.exit(f"ERROR: {plat_dir.relative_to(SCRIPT_DIR)} 不存在")

    link_info_path = plat_dir / "link_info.json"
    if not link_info_path.exists():
        sys.exit(f"ERROR: link_info.json not found")

    link_info = json.loads(link_info_path.read_text())
    objects_dir = plat_dir / "objects"
    resources_dir = plat_dir / "resources"
    if not objects_dir.exists():
        sys.exit(f"ERROR: objects/ not found")

    app_name = link_info["app_name"]
    deployment_target = link_info.get("deployment_target", "16.0")
    archs = link_info.get("architectures", [arch])
    sdk = link_info.get("sdk", "iphoneos")

    qt_lib = qt_root / "lib"
    if not qt_lib.exists():
        sys.exit(f"ERROR: Qt lib dir not found: {qt_lib}")

    sdk_path = subprocess.run(
        ["xcrun", "--sdk", sdk, "--show-sdk-path"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    clang = subprocess.run(
        ["xcrun", "--sdk", sdk, "--find", "clang++"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    link_cmd = [clang]
    for a in archs:
        link_cmd += ["-arch", a]
    link_cmd += [
        "-isysroot", sdk_path,
        f"-m{sdk}-version-min={deployment_target}",
        "-fobjc-arc", "-fobjc-link-runtime",
        # iOS .app 把 FFmpeg 等 .framework 嵌在 <App>.app/Frameworks/, 二进制
        # 里写的是 @rpath/lib*.framework/lib*. 必须加这个 rpath 让运行时找得到,
        # 否则 dyld 报 "Library not loaded: @rpath/libavcodec.framework/libavcodec".
        # 原 Xcode build 是从 LD_RUNPATH_SEARCH_PATHS 设的, 不在 OTHER_LDFLAGS,
        # capture 时漏了, 这里补.
        "-Wl,-rpath,@executable_path/Frameworks",
    ]
    link_cmd += link_info.get("linker_flags", [])

    for obj in link_info.get("app_objects", []):
        obj_path = objects_dir / obj
        if not obj_path.exists():
            print(f"  WARN: missing object: {obj}")
            continue
        link_cmd.append(str(obj_path))

    for qt_item in link_info.get("qt_items", []):
        resolved = qt_root / qt_item
        if not resolved.exists():
            print(f"  WARN: missing Qt item: {resolved}")
            continue
        link_cmd.append(str(resolved))

    for fw in link_info.get("system_frameworks", []):
        link_cmd += ["-framework", fw]
    for lib in link_info.get("system_libraries", []):
        link_cmd += [f"-l{lib}"]
    link_cmd += ["-F", str(qt_lib)]

    relink_dir = _relink_dir_for("ios", arch)
    _clean_relink_dir(relink_dir)
    output_app = relink_dir / f"{app_name}.app"
    output_app.mkdir(parents=True)
    output_binary = output_app / app_name
    link_cmd += ["-o", str(output_binary)]

    print(f"  Linking {app_name}...")
    result = subprocess.run(link_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  LINK FAILED (exit {result.returncode}):")
        print(result.stderr)
        cmd_file = relink_dir / "link_command.sh"
        cmd_file.write_text("#!/bin/sh\n" + " \\\n  ".join(link_cmd) + "\n")
        print(f"  完整 link 命令保存到: {cmd_file.relative_to(SCRIPT_DIR)}")
        sys.exit(1)
    print(f"  OK: {output_binary.relative_to(SCRIPT_DIR)}")

    if resources_dir.exists():
        for item in resources_dir.iterdir():
            dst = output_app / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
        print(f"  Copied resources/ into {output_app.relative_to(SCRIPT_DIR)}/")

    # Ad-hoc 签名整个 .app. clang 直接 link 出的二进制没 LC_CODE_SIGNATURE,
    # Xcode IDECodesign 在 install 时会因没占位空间签名失败 ("No code signature
    # found"). ad-hoc 签一遍把占位补上, Xcode 之后用用户 Team 重签 (codesign
    # --force --sign <Team>) 走 "replace existing signature" 路径, 不会失败.
    print(f"  ad-hoc 签 .app (codesign --sign -) 给 Xcode 重签时占位...")
    fw_dir = output_app / "Frameworks"
    if fw_dir.is_dir():
        for fw in sorted(fw_dir.glob("*.framework")):
            subprocess.run(["codesign", "--force", "--sign", "-",
                            "--timestamp=none", str(fw)], check=False)
    plugins_root = output_app / "PlugIns"
    if plugins_root.is_dir():
        for plug in sorted(plugins_root.iterdir()):
            if plug.is_dir() or plug.suffix in (".dylib", ".framework"):
                subprocess.run(["codesign", "--force", "--sign", "-",
                                "--timestamp=none", str(plug)], check=False)
    subprocess.run(["codesign", "--force", "--sign", "-",
                    "--timestamp=none", str(output_app)], check=False)

    file_check = subprocess.run(
        ["file", str(output_binary)], capture_output=True, text=True,
    )
    if arch in file_check.stdout:
        print(f"  OK: 二进制是 {arch}")

    xcodeproj = plat_dir / f"{app_name}.xcodeproj"
    print(f"\n=== Done. 下一步在 Xcode 部署到真机 ===")
    print()
    print(f"自动打开: {xcodeproj.relative_to(SCRIPT_DIR.parent.parent) if SCRIPT_DIR in xcodeproj.parents else xcodeproj}")
    print()
    print("Xcode 操作:")
    print("  1. 项目设置 → Signing & Capabilities → Team:")
    print("     - 有付费 Apple Developer 账号: 选你的 Team (一年期签名)")
    print("     - 没有: 选你的免费 Apple ID (signed 7 天, 到期重 build 即可)")
    print("  2. 用 USB / 无线 连接 iPhone/iPad, 选它作为 Run Target")
    print("  3. 第一次部署后, 设备上要去:")
    print("     设置 → 通用 → VPN与设备管理 → 信任你的开发者证书")
    print("  4. 回 Xcode 按 ⌘R 运行 (后续也是 ⌘R)")
    print()

    if xcodeproj.exists():
        try:
            subprocess.run(["open", str(xcodeproj)], check=False)
            print(f"  (已自动 open {xcodeproj.name})")
        except FileNotFoundError:
            pass  # not on macOS


# ---------------------------------------------------------------------------
# android
# ---------------------------------------------------------------------------

def relink_android(arch: str, qt_root: Path) -> None:
    """androiddeployqt 重新打 release APK, debug-keystore 签名 (sideload OK).

    流程: androiddeployqt 生成 apk-build/ → patchelf 把所有 .so 改 16KB 对齐
    → 注入 signingConfig (~/.android/debug.keystore) → ./gradlew assembleRelease.

    需要本机有: Android SDK / NDK / JDK 17+ / gradle (gradle wrapper 自带) /
    patchelf (lgpl_dist/pixi.toml 已声明).
    SDK / NDK 通过 ANDROID_SDK_ROOT / ANDROID_NDK_ROOT 或常见路径自动找.
    """
    plat_dir = SCRIPT_DIR / "android"
    abi = arch

    so_dir = plat_dir / "lib" / abi
    if not so_dir.is_dir():
        sys.exit(f"ERROR: android/lib/{abi}/ 不存在")
    ds_src = plat_dir / f"{abi}-deployment-settings.json"
    if not ds_src.exists():
        sys.exit(f"ERROR: {ds_src.name} 不存在")
    staging_src = plat_dir / "android-staging" / abi
    if not staging_src.is_dir():
        sys.exit(f"ERROR: android-staging/{abi}/ 不存在")

    # SDK / NDK 检测
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if not sdk:
        for c in [Path.home() / "Library/Android/sdk",
                  Path.home() / "Android/Sdk"]:
            if c.exists():
                sdk = str(c); break
    if not sdk:
        sys.exit("ERROR: 未找到 Android SDK. 设 ANDROID_SDK_ROOT 或 ANDROID_HOME")

    ndk = os.environ.get("ANDROID_NDK_ROOT")
    if not ndk:
        ndks_dir = Path(sdk) / "ndk"
        if ndks_dir.is_dir():
            ndks = sorted(ndks_dir.iterdir(), reverse=True)
            if ndks:
                ndk = str(ndks[0])
    if not ndk:
        sys.exit("ERROR: 未找到 Android NDK. 设 ANDROID_NDK_ROOT")

    qt_host = detect_qt_host_root(qt_root.parent)
    print(f"  SDK: {sdk}")
    print(f"  NDK: {ndk}")
    print(f"  Qt host: {qt_host}")

    # 准备 relink 工作目录: 拷 staging + .so → relink dir
    relink_dir = _relink_dir_for("android", abi)
    _clean_relink_dir(relink_dir)
    relink_dir.mkdir(parents=True)
    rel_relink = relink_dir.relative_to(SCRIPT_DIR)

    work_staging = relink_dir / "android-staging"
    shutil.copytree(staging_src, work_staging)
    print(f"  Copied: android-staging/{abi}/ → {rel_relink}/android-staging/")

    work_lib = relink_dir / "lib" / abi
    work_lib.mkdir(parents=True)
    for so in so_dir.iterdir():
        if so.is_file() and so.name.endswith(".so"):
            shutil.copy2(so, work_lib / so.name)
    print(f"  Copied: lib/{abi}/*.so → {rel_relink}/lib/{abi}/")

    # 改 deployment-settings.json — 替换所有绝对路径为 user 自己的
    ds = json.loads(ds_src.read_text())

    if isinstance(ds.get("qt"), dict):
        ds["qt"][abi] = str(qt_root)
    ds["sdk"] = sdk
    ds["ndk"] = ndk
    ds["android-package-source-directory"] = str(work_staging)
    ds["extraLibraryDirs"] = [str(work_lib)]
    ds["extraPrefixDirs"] = [str(qt_root)] * 3

    ds["qml-importscanner-binary"] = str(qt_host / "libexec" / "qmlimportscanner")
    ds["qml-dom-binary"] = str(qt_host / "bin" / "qmldom")
    ds["rcc-binary"] = str(qt_host / "libexec" / "rcc")
    # 用 lgpl 时生成的 stub Main.qml 给 qmlimportscanner 当输入, 让它知道
    # 需要 bundle 哪些 QML 模块到 APK.
    qml_stub = SCRIPT_DIR / "qml-imports-stub"
    if qml_stub.is_dir():
        ds["qml-root-path"] = [str(qml_stub)]
    else:
        ds["qml-root-path"] = []

    host_triplet = "darwin-x86_64" if sys.platform == "darwin" \
                   else "linux-x86_64" if sys.platform.startswith("linux") \
                   else "windows-x86_64"
    ds["ndk-host"] = host_triplet
    ds["stdcpp-path"] = str(
        Path(ndk) / "toolchains" / "llvm" / "prebuilt" / host_triplet
        / "sysroot" / "usr" / "lib"
    )

    # android-extra-libs: lgpl 阶段把原 build 的 OpenSSL .so 拷到 lib/<abi>/,
    # 名单记在 _lgpl_android_extra_lib_names. 这里按 user 的 work_lib 路径
    # 重拼绝对路径喂给 androiddeployqt; 它会把这些 .so 装进 APK lib/<abi>/
    # 并由 Qt loader 在启动时 dlopen (libKonan 链了 libcrypto_3.so).
    extra_names = ds.pop("_lgpl_android_extra_lib_names", []) or []
    extras: list[str] = []
    for name in extra_names:
        p = work_lib / name
        if p.exists():
            extras.append(str(p))
        else:
            print(f"  WARN: android-extra-lib 缺失: lib/{abi}/{name}")
    ds["android-extra-libs"] = ",".join(extras)
    if extras:
        print(f"  android-extra-libs ({len(extras)} .so):")
        for p in extras:
            print(f"    - {p}")

    # android-deploy-plugins: 用 user Qt 重新拼路径 (Qt 自带的 qmltooling/tls/imageformats 等)
    plugins = []
    user_plugins_dir = qt_root / "plugins"
    if user_plugins_dir.is_dir():
        for p in sorted(user_plugins_dir.rglob(f"*_{abi}.so")):
            plugins.append(str(p))
    ds["android-deploy-plugins"] = ";".join(plugins)

    # 取 release 构建当时用的 androidCompileSdkVersion (build.py 写 sidecar →
    # generate_lgpl 注入 ds.json → 这里 pop 出来). 严格使用, 不 fallback:
    # 不一致就让用户用 sdkmanager 装齐, 保证 relink 跟 release 一致.
    # 一并打印其它 gradle 配置 (build-tools / NDK / minSdk / targetSdk) 透明化.
    android_platform = ds.pop("_lgpl_android_platform", None)
    gradle_props = ds.pop("_lgpl_gradle_properties", None) or {}
    if not android_platform:
        sys.exit(
            "ERROR: deployment-settings.json 里没有 _lgpl_android_platform. "
            "正常应该由 build.py + generate_lgpl.py 注入. "
            "请重跑 pixi run android <arch> + pixi run lgpl 后再 relink."
        )
    sdk_platform_dir = Path(sdk) / "platforms" / android_platform
    if not sdk_platform_dir.is_dir():
        sys.exit(
            f"ERROR: 本机 SDK 没装 release 当时用的 {android_platform}.\n"
            f"  期望路径: {sdk_platform_dir}\n"
            f"  请装一下:\n"
            f"    yes | $ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager "
            f"\"platforms;{android_platform}\""
        )
    print(f"  release 当时构建配置 (源: gradle.properties):")
    print(f"    androidCompileSdkVersion = {android_platform}  ← --android-platform")
    for k in ("androidBuildToolsVersion", "androidNdkVersion",
              "qtMinSdkVersion", "qtTargetSdkVersion"):
        if k in gradle_props:
            print(f"    {k:<24} = {gradle_props[k]}")

    work_ds = relink_dir / "deployment-settings.json"
    work_ds.write_text(json.dumps(ds, indent=2))
    print(f"  Patched: relink/{abi}/deployment-settings.json")

    # 跑 androiddeployqt
    androiddeployqt = qt_host / "bin" / "androiddeployqt"
    if not androiddeployqt.exists():
        sys.exit(f"ERROR: androiddeployqt 没找到: {androiddeployqt}")

    apk_out_dir = relink_dir / "apk-build"
    apk_out_dir.mkdir()

    # androiddeployqt 期望主 app .so 在 <output>/libs/<abi>/lib<App>_<abi>.so.
    # 原 build 是 CMake qt6_finalize_executable 放进去的. 我们这里手动补:
    # 把 lgpl_dist/android/lib/<abi>/*.so 全拷到 apk-build/libs/<abi>/.
    apk_libs_abi = apk_out_dir / "libs" / abi
    apk_libs_abi.mkdir(parents=True)
    for so in sorted(work_lib.iterdir()):
        if so.is_file() and so.name.endswith(".so"):
            shutil.copy2(so, apk_libs_abi / so.name)
    print(f"  Pre-staged libs to apk-build/libs/{abi}/ "
          f"({len([f for f in apk_libs_abi.iterdir()])} .so)")

    # 分四步: 先 androiddeployqt **不带 --gradle** 只生成 apk-build/ (含
    # libs/<abi>/*.so + AndroidManifest.xml + build.gradle + gradle.properties),
    # 中间用 patchelf 把所有 .so 的 ELF LOAD 段 p_align 改成 16384, 再注入
    # release signingConfig 用 debug.keystore 签名, 最后 ./gradlew assembleRelease
    # 打 release-grade APK (debuggable=false, 优化过).
    # 为啥要 patchelf: Qt 6.8.x 自带 .so 是 4KB 对齐, 我们没法重编 Qt;
    # patchelf 只改 ELF metadata 不改文件内容, 兼容 4KB / 16KB 设备.
    cmd = [str(androiddeployqt),
           "--input", str(work_ds),
           "--output", str(apk_out_dir)]
    if android_platform:
        cmd += ["--android-platform", android_platform]
    print(f"  Step 1: androiddeployqt (无 --gradle, 仅生成 apk-build/)")
    subprocess.run(cmd, check=True, cwd=str(relink_dir))

    # Step 2: patchelf 16KB 对齐
    libs_dir = apk_out_dir / "libs" / abi
    patchelf = shutil.which("patchelf")
    if not patchelf:
        sys.exit(
            "ERROR: patchelf 没找到 (需要 >= 0.18, 支持 --page-size).\n"
            "  - mac: pixi.toml 已声明, 跑 `pixi install` 即可\n"
            "  - Ubuntu 24.04+: `sudo apt install patchelf`\n"
            "  - 老 Linux: 从 https://github.com/NixOS/patchelf 源码 build\n"
            "    (conda-forge linux 卡 0.17, pypi 的 0.18.0 被 yanked)"
        )
    if libs_dir.is_dir():
        patched = 0
        for so in sorted(libs_dir.iterdir()):
            if not so.is_file() or not so.name.endswith(".so"):
                continue
            r = subprocess.run([patchelf, "--page-size", "16384", str(so)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"    patchelf FAIL {so.name}: {r.stderr.strip()}")
            else:
                patched += 1
        print(f"  Step 2: patchelf --page-size 16384 → {patched} .so files in libs/{abi}/")

    # Step 3: 注入 release signingConfig 让 assembleRelease 用 ~/.android/
    # debug.keystore 签名. 这样最终 APK 是 release-grade (debuggable=false,
    # 优化过) 但用 debug 密钥签名, end user 可直接 adb install. 想用正式发布
    # 密钥, 改 storeFile/keyAlias 即可.
    debug_keystore = Path(os.path.expanduser("~/.android/debug.keystore"))
    if not debug_keystore.exists():
        # 没自动生成过的话, 跑一次任何 ADB 命令就会触发, 这里主动生成一份.
        debug_keystore.parent.mkdir(parents=True, exist_ok=True)
        keytool = shutil.which("keytool")
        if not keytool:
            sys.exit("ERROR: ~/.android/debug.keystore 不存在, 也没找到 keytool. "
                     "先开一下 Android Studio / 跑 `adb` 让它自动生成.")
        subprocess.run([
            keytool, "-genkey", "-v",
            "-keystore", str(debug_keystore),
            "-storepass", "android",
            "-alias", "androiddebugkey",
            "-keypass", "android",
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "10000",
            "-dname", "CN=Android Debug,O=Android,C=US",
        ], check=True)
        print(f"  Generated {debug_keystore}")

    build_gradle = apk_out_dir / "build.gradle"
    extra = f"""
// === Injected by lgpl_dist/relink.py ===
// 用 ~/.android/debug.keystore 给 release buildType 签名, end user 不需要
// 正式 release 密钥也能装到自己设备. 如果用户后面要发到 Google Play,
// 改 storeFile / keyAlias 指向他自己的密钥即可.
android {{
    signingConfigs {{
        relinkRelease {{
            storeFile file('{debug_keystore}')
            storePassword 'android'
            keyAlias 'androiddebugkey'
            keyPassword 'android'
        }}
    }}
    buildTypes {{
        release {{
            signingConfig signingConfigs.relinkRelease
            minifyEnabled false
            shrinkResources false
        }}
    }}
}}
"""
    build_gradle.write_text(build_gradle.read_text() + extra)
    print(f"  Injected release signingConfig (debug.keystore) into build.gradle")

    # Step 4: 跑 gradle 打 release APK
    gradlew = apk_out_dir / "gradlew"
    if not gradlew.exists():
        sys.exit(f"ERROR: gradlew 不存在: {gradlew}")
    gradlew.chmod(0o755)
    print(f"  Step 4: ./gradlew assembleRelease")
    subprocess.run([str(gradlew), "assembleRelease"], check=True, cwd=str(apk_out_dir))

    apks = list(apk_out_dir.rglob("*.apk"))
    if apks:
        # assembleRelease 产物在 apk-build/build/outputs/apk/release/. 取最新.
        best_apk = max(apks, key=lambda p: p.stat().st_mtime)
        final_apk = relink_dir / f"{ds.get('application-binary', 'app')}-{abi}.apk"
        shutil.copy2(best_apk, final_apk)
        print(f"\n=== Done ===")
        print(f"APK: {final_apk}")
        print(f"安装到设备: adb install -r {final_apk}")
        print(f"或拷到手机后, 设备上设置 → 允许此应用安装未知应用 → 安装.")
    else:
        print(f"\n  WARNING: APK 未生成 (apk-build/ 里没找到 *.apk)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pixi run relink",
        description=(
            "用你自己的 Qt 把 lgpl_dist 里的二进制重新组合成可运行的发行包.\n"
            "全平台 release-grade. 签名: macos→ad-hoc; ios→Xcode Team;\n"
            "android→assembleRelease+debug-keystore; windows→不签."
        ),
        epilog=(
            "示例:\n"
            "  pixi run relink macos   --arch arm64       --qt-dir ~/Qt/6.8.3\n"
            "  pixi run relink ios     --arch arm64       --qt-dir ~/Qt/6.8.3\n"
            "  pixi run relink android --arch arm64-v8a   --qt-dir ~/Qt/6.8.3\n"
            "  pixi run relink windows --arch x86_64      --qt-dir C:/Qt/6.8.3\n"
            "  pixi run relink --clear                    # 清空 lgpl_dist/relink/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # platform/arch/qt-dir 在 --clear 模式下不需要, 改成可选 + 后面手动校验.
    parser.add_argument("platform", nargs="?",
                        choices=["macos", "ios", "android", "windows"],
                        help="目标平台 (--clear 时不传)")
    parser.add_argument("--arch",
                        help=("目标架构 (跟 lgpl_dist 子目录名一致):\n"
                              "  macos / windows: arm64 / x86_64\n"
                              "  ios: arm64\n"
                              "  android: arm64-v8a / armeabi-v7a / x86_64 / x86"))
    parser.add_argument("--qt-dir", dest="qt_dir",
                        help=("Qt 版本根 (含各平台子目录), e.g.:\n"
                              "  ~/Qt/6.8.3              (mac/iOS/Android)\n"
                              "  C:/Qt/6.8.3             (Windows)\n"
                              "也接受平台子目录直接路径 (~/Qt/6.8.3/macos)."))
    parser.add_argument("--clear", action="store_true",
                        help="删整个 lgpl_dist/relink/ 后退出, 不需 platform/arch/qt-dir.")
    args = parser.parse_args()

    if args.clear:
        relink_root = SCRIPT_DIR / "relink"
        if relink_root.exists():
            print(f"rm -rf {relink_root.relative_to(SCRIPT_DIR)}/")
            shutil.rmtree(relink_root)
            print("Done.")
        else:
            print(f"{relink_root.relative_to(SCRIPT_DIR)}/ 不存在, 没要删的.")
        return

    missing = [name for name, val in
               (("platform", args.platform), ("--arch", args.arch),
                ("--qt-dir", args.qt_dir))
               if not val]
    if missing:
        parser.error(f"缺参数 {', '.join(missing)} (除非用 --clear)")

    qt_dir = Path(os.path.expanduser(args.qt_dir)).resolve()
    if not qt_dir.exists():
        sys.exit(f"ERROR: --qt-dir 不存在: {qt_dir}")
    qt_target = resolve_qt_for_target(qt_dir, args.platform, args.arch)

    print(f"\n=== Relink: {args.platform}/{args.arch} ===\n")

    dispatch = {
        "macos":   relink_macos,
        "ios":     relink_ios,
        "android": relink_android,
        "windows": relink_windows,
    }
    dispatch[args.platform](args.arch, qt_target)


if __name__ == "__main__":
    main()
