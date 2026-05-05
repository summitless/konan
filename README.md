# Konan — LGPL Compliance Distribution

*[English](#english) · [中文](#中文)*

<a id="english"></a>

This branch ships **Konan** binaries together with a relink toolchain so
you can **re-link the application against your own Qt 6 build/install** and
produce a runnable distribution. This is how we satisfy the LGPLv3 *"user must
be able to replace the Qt libraries"* clause: you can pick any compatible Qt
version and re-link `Konan` yourself, without us shipping Qt source or
redistributing Qt binaries.

Qt itself is **not** in this repo — install Qt 6.8.3 (or the same minor
series) yourself from [qt.io](https://www.qt.io/download-qt-installer).

## Why relink

1. **LGPL compliance** — almost every Qt 6 module is LGPLv3 (with a few
   GPL-only ones like QtVirtualKeyboard, which the relink flow filters out).
   LGPL requires that users can **swap** these libraries, so the upstream
   binaries cannot statically embed Qt — relink space must be left.
2. **Native deploy tools** — `macdeployqt` (macOS), `windeployqt` (Windows),
   Xcode (iOS), `androiddeployqt` (Android) are each tied to one concrete Qt
   install. The relink flow runs them on your machine so they can locate the
   right framework / DLL / xcframework files for the final bundle.

## Platform / arch matrix

- `android/` — arm64-v8a, armeabi-v7a
- `ios/` — arm64
- `macos/` — arm64, x86_64
- `windows/` — x86_64, arm64

## Prerequisites

**All platforms**:

- [Pixi](https://pixi.sh) (project manager + Python + cmake). After installing
  pixi, the first command run inside `lgpl_dist/` automatically resolves the
  dependencies in `pixi.toml`.
- Qt 6.8.x desktop / mobile components (use the [Qt Online Installer](https://www.qt.io/download-qt-installer)).
  See the per-platform notes below for which components to tick. `--qt-dir`
  takes the Qt version root, e.g. `~/Qt/6.8.3` or `C:/Qt/6.8.3`; the script
  picks the platform subdirectory itself.

**Per-platform extras**:

| Platform | Required |
|----------|----------|
| macOS | Xcode command-line tools (`xcode-select --install`) |
| iOS | Xcode (with a free Apple ID Personal Team or paid Developer Program) |
| Android | Android SDK + NDK (Android Studio includes both) + JDK 17+ |
| Windows | Visual Studio 2022 (MSVC + Windows SDK) |

Qt installer notes:

- macOS / desktop: tick `Qt 6.8.x → macOS`.
- iOS: tick `Qt 6.8.x → iOS`.
- Android: tick `Qt 6.8.x → Android <abi>` matching the abi you want to
  relink (e.g. `arm64-v8a` → `Qt 6.8.x → Android ARM64-v8a`).
- Windows x86_64: tick `Qt 6.8.x → MSVC 2022 64-bit`.
- Windows ARM64: tick `Qt 6.8.x → MSVC 2022 ARM64` **and also**
  `MSVC 2022 64-bit` — the ARM64 install lacks the host tools
  (`windeployqt`, `qtpaths` are host binaries and must come from x64).
  The relink script automatically uses the x64 qtpaths sandbox to redirect
  to the ARM64 Qt prefix.

## Usage

```bash
# 1. Clone this branch (lgpl is an orphan branch — its tree is this directory)
git clone -b lgpl --depth 1 <repo-url> konan-lgpl
cd konan-lgpl

# 2. Run relink, passing the version root of your Qt install
pixi run relink macos   --arch arm64       --qt-dir ~/Qt/6.8.3
pixi run relink macos   --arch x86_64      --qt-dir ~/Qt/6.8.3
pixi run relink ios     --arch arm64       --qt-dir ~/Qt/6.8.3
pixi run relink android --arch arm64-v8a   --qt-dir ~/Qt/6.8.3
pixi run relink android --arch armeabi-v7a --qt-dir ~/Qt/6.8.3
pixi run relink windows --arch x86_64      --qt-dir C:/Qt/6.8.3
pixi run relink windows --arch arm64       --qt-dir C:/Qt/6.8.3

# Wipe outputs (not required between runs — relink already cleans its own subdir)
pixi run relink --clear
```

**Output**: `relink/<plat>/<arch>/` (gitignored, won't pollute the branch).

## Per-platform notes

### macOS

- Tooling: `macdeployqt` (bundled with Qt) copies Qt frameworks + plugins +
  qml into the .app, then ad-hoc-signs it so it can launch on Apple Silicon
  (unsigned .app is refused; the script does this automatically).
- Output: `relink/macos/<arch>/Konan.app/`. Double-click in Finder or
  `open Konan.app`.
- The script removes GPL-only `QtVirtualKeyboard` and the unused Qt3D suite
  (`Qt3DCore` / `Qt3DRender` etc.) that macdeployqt drags along — keeping
  them would add tens of MB of dead code.

### iOS

- Tooling: static re-link via clang++ direct invocation + an auto-generated
  Xcode project.
- Output: `relink/ios/arm64/Konan.app/` plus `lgpl_dist/ios/arm64/Konan.xcodeproj`
  (the xcodeproj sits at the branch root — open it directly).
- Steps to install on a real device:
  1. Run `pixi run relink ios --arch arm64 --qt-dir ~/Qt/6.8.3`.
  2. `open ios/arm64/Konan.xcodeproj` to launch Xcode.
  3. Project settings → Signing & Capabilities → choose your own Apple ID
     Team (a free Personal Team is fine — 7-day cert).
  4. Pick your real device → Run. Xcode re-signs the entire .app plus the
     embedded frameworks / PlugIns with your Team.

### Android

- Tooling: `androiddeployqt` (bundled with Qt) + Gradle (assembleRelease).
- Output: `relink/android/<abi>/Konan-<abi>.apk` — install with `adb install -r`.
- Signing: uses `~/.android/debug.keystore` (debug keystore, but
  buildType=release — that is, a release-grade APK with the dev signature).
  The script auto-generates the keystore via `keytool` if missing. **For
  Google Play distribution**, edit `apk-build/build.gradle` `signingConfig`
  to point at your own release keystore.
- OpenSSL: the Qt Android SDK only ships an FFmpeg stub, not real
  libcrypto/libssl. We bundle the [KDAB android_openssl](https://github.com/KDAB/android_openssl)
  `.so` files into the APK at `lib/<abi>/libcrypto_3.so` + `libssl_3.so`.
- 16 KB page size: Android 15+ on Pixel 8/9 and other 16-KB-kernel devices
  requires `.so` files aligned to 16 KB. The script uses
  `patchelf --page-size 16384` to re-align the bundled Qt `.so` files.
  Linux users running android relink need `patchelf >= 0.18` themselves
  (Ubuntu 24.04+ ships it via `apt install patchelf`; older distros need a
  source build). On macOS pixi installs it automatically.

### Windows

- Tooling: `windeployqt` (bundled with Qt) copies Qt DLLs / plugins / qml
  next to `bin/`.
- Output: `relink/windows/<arch>/bin/Konan.exe`, double-click to launch
  (no console window).
- ARM64 cross-compile (relinking ARM64 from an x64 host): the script
  1. Runs `windeployqt.exe` from the host x64 install (the ARM64 Qt install
     ships no host tools).
  2. Copies host x64 `qtpaths6.exe` + `Qt6Core.dll` to
     `relink/windows/arm64/_qtpaths_sandbox/`, drops a `qt.conf` next to
     them that redirects `Prefix` to the ARM64 Qt.
  3. The host windeployqt invokes
     `--qtpaths=<sandbox>/qtpaths6.exe` so it picks up the ARM64
     lib/plugins/qml paths while the executables it runs are still x64
     (no architecture mismatch).
  In short: ARM64 relink requires host x64 Qt to be installed too.

## Troubleshooting

- **Wrong Qt path** — `--qt-dir` should be the Qt version root (e.g.
  `~/Qt/6.8.3`); the script appends platform suffixes
  (`macos` / `msvc2022_64` / `android_arm64_v8a` etc.). It also accepts a
  platform subdirectory directly (e.g. `~/Qt/6.8.3/macos`).
- **Plugin .dll fails to load (Windows)** — usually the plugin links Qt
  modules the main exe doesn't (`Qt6Concurrent`, `Qt6WebSockets`, …) and
  windeployqt only scans the main `.exe` by default. The script already
  feeds every non-Qt `.dll` (plugin / juice / etc.) to windeployqt; if it
  still complains, check that
  `relink/windows/<arch>/_qtpaths_sandbox/qt.conf` points at the right
  prefix.
- **macOS double-click does nothing** — ad-hoc signing failed. Reproduce
  with `codesign --force --sign - relink/macos/<arch>/Konan.app`.
- **Android crashes on launch / 16 KB error** — `patchelf >= 0.18` not
  installed or didn't run. The script logs patchelf invocations near the
  end of the run.
- **iOS "invalid signature" / "No code signature found" on real device** —
  Xcode didn't re-sign embedded frameworks. The auto-generated xcodeproj
  ships a build-phase shell script that runs
  `codesign --force --sign $EXPANDED_CODE_SIGN_IDENTITY` on every framework
  + PlugIn. If the error persists, double-check that a Team is selected
  under Signing & Capabilities.

## Layout

```
lgpl_dist/                      ← branch root == this directory
├── README.md                   ← this file
├── NOTICE                      ← Qt LGPL attribution
├── LICENSE.LGPLv3              ← full Qt LGPLv3 license text
├── pixi.toml                   ← pixi tasks (relink entry point)
├── relink.py                   ← per-platform relink driver
├── qml-imports-stub/           ← stubs of project .qml imports for
│                                 macdeployqt / qmlimportscanner
├── macos/<arch>/<APP>.app/     ← no _CodeSignature, no Qt frameworks
├── ios/<arch>/                 ← objects + resources + link_info.json + xcodeproj
├── android/                    ← lib/<abi>/*.so + deployment-settings.json + staging
├── windows/<arch>/bin/         ← Konan.exe + project plugin .dll, no Qt6*.dll
└── relink/                     ← created by relink runs (gitignored)
    ├── macos/<arch>/<APP>.app/
    ├── ios/<arch>/<APP>.app/
    ├── android/<abi>/*.apk
    └── windows/<arch>/bin/
```

## Legal

- Qt is © The Qt Company Ltd. Each Qt module is distributed under LGPLv3 —
  see [NOTICE](NOTICE) and [LICENSE.LGPLv3](LICENSE.LGPLv3).
- Konan's own source / license live in the upstream main repo (this
  branch is solely an LGPL-compliance distribution).

---

<a id="中文"></a>

# Konan — LGPL 合规分发

本仓库分支提供 **Konan** 的二进制产物 + 一套 relink 工具链,
让你 **用自己编译/下载的 Qt 6 重新链接** 出可运行的发行包。这是 Qt 在 LGPLv3
下要求的 **"用户可替换 Qt 库"** 条款的实现:你随时可以拿一份不同版本的 Qt
重新链接 `Konan`,而不需要本仓库提供 Qt 源码或重新分发 Qt 二进制。

Qt 自身不在这个仓库里 —— 你要自己去 [qt.io](https://www.qt.io/download-qt-installer)
下载并安装 Qt 6.8.3 (或同小版本系列) 的对应平台/架构组件。

## 为什么要 relink

1. **LGPL 合规** —— Qt 6 各模块基本都是 LGPLv3 (除了 QtVirtualKeyboard 之类
   GPL-only,relink 流程会主动剔除)。LGPL 要求用户 **能替换** 这些库,所以
   原始二进制不能把 Qt 写死,必须留出重新链接的空间。
2. **平台原生工具** —— macOS 的 `macdeployqt`、Windows 的 `windeployqt`、
   iOS 的 Xcode、Android 的 `androiddeployqt`,这些工具只跟一份具体安装的
   Qt 绑定。relink 流程在你的本机调它们,自动找到合适的 framework / DLL /
   xcframework 完成最终包装。

## 平台 / 架构 矩阵

- `android/` — arm64-v8a, armeabi-v7a
- `ios/` — arm64
- `macos/` — arm64, x86_64
- `windows/` — x86_64, arm64

## 准备工作

**所有平台共用**:

- [Pixi](https://pixi.sh) (项目管理 + Python + cmake)。装 pixi 后第一次进
  `lgpl_dist/` 跑命令,pixi 会自动按 `pixi.toml` 拉好依赖。
- Qt 6.8.x 桌面 / 移动端组件 (用 [Qt Online Installer](https://www.qt.io/download-qt-installer))。
  装哪些 components 看下面各平台分节。`--qt-dir` 传 Qt 版本根,
  e.g. `~/Qt/6.8.3` 或 `C:/Qt/6.8.3`,脚本自己定位平台子目录。

**额外平台依赖**:

| 平台 | 必装 |
|------|------|
| macOS | Xcode 命令行工具 (`xcode-select --install`) |
| iOS | Xcode (装到 Apple Developer 免费 Team / 付费) |
| Android | Android SDK + NDK (Android Studio 自带) + Java 17+ JDK |
| Windows | Visual Studio 2022 (含 MSVC + Windows SDK) |

Qt Installer 装包提示:

- macOS / 桌面端:勾 `Qt 6.8.x → macOS` 即可。
- iOS:勾 `Qt 6.8.x → iOS`。
- Android:勾 `Qt 6.8.x → Android <abi>` (按你 relink 的目标 abi 选,
  `arm64-v8a` 装 `Qt 6.8.x → Android ARM64-v8a`,armeabi-v7a 同理)。
- Windows x86_64:勾 `Qt 6.8.x → MSVC 2022 64-bit`。
- Windows ARM64:勾 `Qt 6.8.x → MSVC 2022 ARM64` **同时也要勾** 
  `MSVC 2022 64-bit` —— ARM64 安装包不带 host 工具 (windeployqt /
  qtpaths 是 host 二进制,必须从 x64 拷)。relink 脚本会自动做 x64
  qtpaths 沙箱重定向 ARM64 Qt。

## 用法

```bash
# 1. 拉本分支 (lgpl 是孤儿分支, 内容就是这个目录树)
git clone -b lgpl --depth 1 <repo-url> konan-lgpl
cd konan-lgpl

# 2. 跑 relink, 传你自己 Qt 安装的版本根
pixi run relink macos   --arch arm64       --qt-dir ~/Qt/6.8.3
pixi run relink macos   --arch x86_64      --qt-dir ~/Qt/6.8.3
pixi run relink ios     --arch arm64       --qt-dir ~/Qt/6.8.3
pixi run relink android --arch arm64-v8a   --qt-dir ~/Qt/6.8.3
pixi run relink android --arch armeabi-v7a --qt-dir ~/Qt/6.8.3
pixi run relink windows --arch x86_64      --qt-dir C:/Qt/6.8.3
pixi run relink windows --arch arm64       --qt-dir C:/Qt/6.8.3

# 清理产物 (重新跑前不必, relink 自己每次也会清自己的子目录)
pixi run relink --clear
```

**输出位置**:`relink/<plat>/<arch>/` (gitignored,不会污染分支)。

## 各平台注意事项

### macOS

- 工具链:`macdeployqt` (Qt 自带) 把 Qt frameworks + plugins + qml 拷进 .app,
  ad-hoc 签名后双击就能跑。Apple Silicon 上未签名 .app 直接拒跑,所以 ad-hoc
  是必须的 (脚本会自动做)。
- 输出:`relink/macos/<arch>/Konan.app/`,Finder 双击或 `open Konan.app`。
- 脚本会自动剔除 GPL-only 的 `QtVirtualKeyboard` 和本应用没用到的 Qt3D 全套
  (`Qt3DCore` / `Qt3DRender` 等) —— 这些是 macdeployqt 顺手拽进来的,留着
  会让 .app 多出几十 MB 没用到的代码。

### iOS

- 工具链:静态重链 (clang++ 直接调) + 自动生成的 Xcode 工程。
- 输出:`relink/ios/arm64/Konan.app/` + 旁边 `lgpl_dist/ios/arm64/Konan.xcodeproj`
  (这个 xcodeproj 在分支根目录里,直接 open)。
- 装真机的步骤:
  1. 跑 `pixi run relink ios --arch arm64 --qt-dir ~/Qt/6.8.3` 完成 relink。
  2. `open ios/arm64/Konan.xcodeproj`,Xcode 会打开。
  3. 在 Xcode → 项目 setting → Signing & Capabilities → 选你自己的 Apple ID
     Team (免费 Personal Team 7 天证书也行)。
  4. 选目标真机 → Run。Xcode 会用你的 Team 重签整个 .app + 嵌入的
     frameworks / PlugIns。

### Android

- 工具链:`androiddeployqt` (Qt 自带) + Gradle (assembleRelease)。
- 输出:`relink/android/<abi>/Konan-<abi>.apk`,直接 `adb install -r` 装。
- 签名:用 `~/.android/debug.keystore` (debug-keystore 但 buildType=release,
  即 release-grade APK + 开发签名)。脚本会自动 `keytool` 生成这个 keystore
  如果不存在。**想发 Google Play** 自己改 `apk-build/build.gradle` 里
  signingConfig 指向你自己的 release keystore。
- OpenSSL:Qt Android SDK 只带 FFmpeg stub,不带真正的 libcrypto/libssl。我们
  把 [KDAB android_openssl](https://github.com/KDAB/android_openssl) 的 .so
  跟应用一起打进 APK,自动放 `lib/<abi>/libcrypto_3.so` + `libssl_3.so`。
- 16KB page size:Android 15+ 在 Pixel 8/9 等 16KB 内核设备上要求 .so
  按 16KB 对齐。脚本会用 `patchelf --page-size 16384` 把 Qt 自带 .so 重对齐。
  Linux 用户跑 android relink 需要自己装 `patchelf >= 0.18` (Ubuntu 24.04+
  `apt install patchelf` 直接给;旧发行版从 GitHub 源码 build)。Mac 用户由
  pixi 自动装,不用管。

### Windows

- 工具链:`windeployqt` (Qt 自带) 把 Qt DLLs / plugins / qml 拷到 bin/。
- 输出:`relink/windows/<arch>/bin/Konan.exe`,双击运行 (无控制台窗口)。
- ARM64 cross-compile (在 x64 host 上 relink ARM64):脚本会:
  1. 用 host x64 的 `windeployqt.exe` 跑 (ARM64 安装不带 host 工具)。
  2. 拷一份 host x64 `qtpaths6.exe` + `Qt6Core.dll` 到 `relink/windows/arm64/_qtpaths_sandbox/`,
     旁边写一个 `qt.conf` 把 `Prefix` 重定向到 ARM64 Qt。
  3. host windeployqt 通过 `--qtpaths=<sandbox>/qtpaths6.exe` 拿到 ARM64
     的 lib/plugins/qml 路径,但运行的是 x64 二进制不会架构冲突。
  也就是说 ARM64 relink 的前提是 **host x64 Qt 也装了**。

## 故障排查

- **Qt 路径错** —— `--qt-dir` 要传 Qt 版本根 (如 `~/Qt/6.8.3`)。脚本
  会自己加平台后缀 (macos / msvc2022_64 / android_arm64_v8a 等)。也接受
  直接传平台子目录 (如 `~/Qt/6.8.3/macos`)。
- **plugin .dll 加载失败 (Windows)** —— 通常是 plugin 链了 Qt6Concurrent /
  Qt6WebSockets 之类主程序没用的 Qt 模块,而 windeployqt 默认只扫主 .exe。
  脚本已经把所有非 Qt 的 .dll (plugin / juice / 等) 一起传给 windeployqt 扫。
  如果还是报,看 `relink/windows/<arch>/_qtpaths_sandbox/qt.conf` 是否指对。
- **macOS 双击没反应** —— ad-hoc 签名失败。手动跑
  `codesign --force --sign - relink/macos/<arch>/Konan.app` 验证。
- **Android 闪退 / 16KB 报错** —— 没装 `patchelf >= 0.18` 或 patchelf 没起
  作用。脚本结尾会提示 patchelf 调用情况,看 console 日志。
- **iOS 装真机 "invalid signature" / "No code signature found"** —— Xcode
  没把嵌入 framework 一起重签。脚本生成的 xcodeproj 已经塞了 build phase
  shell script 自动跑 `codesign --force --sign $EXPANDED_CODE_SIGN_IDENTITY`
  到所有 frameworks + PlugIns。如果还报,确认 Xcode 的 "Signing & Capabilities"
  里 Team 选了。

## 文件布局

```
lgpl_dist/                      ← 本分支根 == 这个目录
├── README.md                   ← 你正在看的这份
├── NOTICE                      ← Qt LGPL attribution
├── LICENSE.LGPLv3              ← Qt LGPLv3 license 全文
├── pixi.toml                   ← pixi 任务定义 (relink 脚本入口)
├── relink.py                   ← 各平台 relink 主脚本
├── qml-imports-stub/           ← 项目 .qml import 抽取的 stub, 给
│                                 macdeployqt / qmlimportscanner 扫
├── macos/<arch>/<APP>.app/     ← 无 _CodeSignature, 无 Qt frameworks
├── ios/<arch>/                 ← objects + resources + link_info.json + xcodeproj
├── android/                    ← lib/<abi>/*.so + deployment-settings.json + staging
├── windows/<arch>/bin/         ← Konan.exe + 项目 plugin .dll, 无 Qt6*.dll
└── relink/                     ← 你跑 relink 后产生 (gitignored)
    ├── macos/<arch>/<APP>.app/
    ├── ios/<arch>/<APP>.app/
    ├── android/<abi>/*.apk
    └── windows/<arch>/bin/
```

## 法律

- Qt 是 The Qt Company Ltd. 的 © 注册商标,各模块在 LGPLv3 下分发,见
  [NOTICE](NOTICE) 和 [LICENSE.LGPLv3](LICENSE.LGPLv3)。
- Konan 自身的源码 / 许可见上游主仓 (本分支只承担 LGPL 合规分发)。
