// ============================================================================
// TokenRefreshWorker —— Android WorkManager 后台刷新 OIDC token 的最小 Worker。
//
// 为什么存在这个 Java 文件:
//   androidx.work.Worker 必须是 Java/Kotlin 类(WorkManager 通过反射加载),
//   无法用纯 C++ 绕开。doWork() 通过 JNI 把控制权交给 C++ 侧
//   (BackgroundRefreshAndroid.cpp),具体的 refresh 流程(Keychain 读写、
//   HTTP 调 Keycloak /token)全部在 Qt 主线程跑,Java 这边只是入口。
//
// 构建依赖:需要在 android/build.gradle 里加
//   implementation "androidx.work:work-runtime:2.9.1"
// (或更新版本)。Qt 默认 build.gradle 不含这个依赖,构建失败时按需追加。
//
// 原生库加载:WorkManager 可能在用户没打开 App 时触发 Worker,此时 Qt 的
// .so 还没加载,nativeRefreshToken() 会 UnsatisfiedLinkError。构造函数
// 里按 AndroidManifest.xml 里的 android.app.lib_name 主动 loadLibrary,
// 让 Worker 自己把 Qt app 的原生库拉起来。
//
// **本文件是 .java.in 模板**:`com.hbb.konan` 在 CMake build 时替换。
// ============================================================================
package com.hbb.konan;

import android.content.ComponentName;
import android.content.Context;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.os.Bundle;
import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

public class TokenRefreshWorker extends Worker {

    private static volatile boolean sNativeLoaded = false;

    public TokenRefreshWorker(@NonNull Context ctx, @NonNull WorkerParameters params) {
        super(ctx, params);
        loadQtNativeLib(ctx);
    }

    /// 读取 manifest 里 QtActivity 上的 android.app.lib_name meta-data,
    /// 据此 loadLibrary。和 Qt 本身加载 .so 的机制保持一致,不硬编码
    /// APP_NAME,改 APP_NAME 时也不需要同步改这个 Java 文件。
    private static synchronized void loadQtNativeLib(Context ctx) {
        if (sNativeLoaded) return;
        try {
            // 注:manifest 里现在用的是 KonanQtActivity(QtActivity 子类,
            // override onNewIntent 修分享 intent)。lib_name meta-data 在
            // 子类的 activity 节点上,这里查它即可。
            ComponentName comp = new ComponentName(
                ctx, "com.hbb.konan.KonanQtActivity");
            ActivityInfo info = ctx.getPackageManager()
                .getActivityInfo(comp, PackageManager.GET_META_DATA);
            Bundle meta = info.metaData;
            if (meta != null) {
                String libName = meta.getString("android.app.lib_name");
                if (libName != null && !libName.isEmpty()) {
                    System.loadLibrary(libName);
                    sNativeLoaded = true;
                }
            }
        } catch (Throwable t) {
            // 没加载到也没关系,doWork 会 catch UnsatisfiedLinkError 让
            // WorkManager 按指数退避重试。
        }
    }

    @NonNull
    @Override
    public Result doWork() {
        try {
            if (nativeRefreshToken()) {
                return Result.success();
            } else {
                return Result.retry();
            }
        } catch (UnsatisfiedLinkError e) {
            // Qt 原生库不可用(App 从未打开过,或系统把 .so 卸载了)
            return Result.retry();
        } catch (Throwable t) {
            return Result.retry();
        }
    }

    /// 由 BackgroundRefreshAndroid.cpp 实现。阻塞直到 refresh 完成,
    /// 返回 true 表示成功、false 表示失败(WorkManager 按指数退避重试)。
    private static native boolean nativeRefreshToken();
}
