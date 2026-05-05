// ============================================================================
// KonanQtActivity —— QtActivity 的最小子类,fix onNewIntent 不 setIntent。
//
// 为什么需要这个 Java 文件:
//   Qt 6.8 的 org.qtproject.qt.android.bindings.QtActivity 的默认 onNewIntent
//   实现 **没有调 setIntent(intent)**,导致后续 getIntent() 永远返回最初启动
//   Activity 的那条 intent(通常是 ACTION_MAIN)。
//
//   我们的 ShareReceiverAndroid 通过 QtNative.activity().getIntent() 读取分享
//   payload。如果 Konan 已经在前台 / 后台进程还活着,从其它 App 分享图片
//   过来 Android 走 onNewIntent(launchMode=singleTop)而非 onCreate,
//   此时不调 setIntent 我们就永远看不到那条 ACTION_SEND intent —— 表现就是
//   "分享图片到 Konan 完全没反应,日志一行不打"。
//
//   修复就一行:onNewIntent 里 super.onNewIntent 之后立刻 setIntent,把
//   activity 的当前 intent 替换成新来的那条,后续 getIntent() 就正确了。
//   ApplicationStateChanged → Active 的钩子在 ShareReceiverAndroid 里负责
//   重新 checkIntent,把 SEND payload 入库。
//
// AndroidManifest 把 activity 的 android:name 从 QtActivity 换成本类即可,
// 别的 meta-data(android.app.lib_name 等)都保持不变,Qt 内部启动逻辑
// 完全沿用父类。
//
// **本文件是 .java.in 模板**:真正参与编译的 .java 由 CMake configure_file 在
// build 时生成到 ${BIN_DIR}/android-staging/src/${APP_JAVA_PKG_PATH}/。
// 下面 `com.hbb.konan` 占位符会被替换成顶层 CMakeLists.txt 里的 APP_BUNDLE_ID 值,
// 保证 Java 包名跟应用 ID 始终一致,改 APP_BUNDLE_ID 时不需要 grep 改这里。
// ============================================================================
package com.hbb.konan;

import android.content.Intent;
import org.qtproject.qt.android.bindings.QtActivity;

public class KonanQtActivity extends QtActivity {

    private static final String LOG_TAG = "KonanShare";

    @Override
    public void onCreate(android.os.Bundle savedInstanceState) {
        // **swipe + 立刻重开** 死锁修复:
        //
        // 用户向上划掉 Konan 时进程不是瞬间死的,FG service / Qt 后台线程都
        // 在 tear down 中。如果用户立刻点图标重开,Android 看到进程**还
        // 没死**就把新 Activity 挂到这个半死进程上跑 onCreate。
        //
        // Qt 默认的 QtActivityBase.onCreate 看到 isStarted=true,会调
        // restartApplication():
        //     makeRestartActivityTask → startActivity → QtNative.quitApp() → Runtime.exit(0)
        // 其中 quitApp() 要等内部 queue / event loop / 已存在的线程 drain
        // 干净 —— 这些东西本来就在 swipe-shutdown 过程里相互纠缠,直接死锁
        // (用户表现:点开 Konan 卡黑屏不动)。等几秒再点就好,因为那时
        // 进程已经完全死透,Android 拉的是干净 cold start。
        //
        // 这里**抢在 super.onCreate 之前**检测 isStarted。命中就 queue 一个
        // 重启 intent + SIGKILL 自己,绕开 quitApp 的 drain 路径。Android
        // 收到 queued intent 给我们拉一个干净进程,onCreate 在新进程里
        // isStarted=false,正常走 super 流程。
        if (isQtAlreadyStartedInThisProcess()) {
            android.util.Log.w(LOG_TAG,
                "onCreate: Qt isStarted=true (stale process from swipe), "
                + "fast-relaunching via SIGKILL to bypass quitApp() deadlock");
            Intent restart = Intent.makeRestartActivityTask(getComponentName());
            startActivity(restart);
            // SIGKILL,跳过 Qt 的 quitApp drain。Activity onCreate 没走完
            // Android 会记一条警告,无害(进程都没了)。
            android.os.Process.killProcess(android.os.Process.myPid());
            return;
        }

        super.onCreate(savedInstanceState);
        Intent i = getIntent();
        android.util.Log.i(LOG_TAG, "onCreate: action=" +
            (i != null ? i.getAction() : "null"));
    }

    @Override
    protected void onDestroy() {
        // Activity 真的要结束(swipe / 用户从最近任务划掉 / 显式 finish())
        // 时,**抢在 super.onDestroy 之前 SIGKILL 自己**,绕开 Qt 的同步阻塞
        // 清理路径。
        //
        // 背景:QtActivityBase.onDestroy 的字节码:
        //     super.onDestroy()              ← Activity.onDestroy,快
        //     QtNative.terminateQt()         ← **阻塞**等 Qt event loop 退出
        //     QtNative.setActivity(null)
        //     QtNative.getQtThread().exit()  ← **阻塞**等 QtThread join
        //     System.exit(0)
        // 我们的 ClipboardMonitor polling timer / DirectorySyncEngine socket
        // 线程 / QtConcurrent thumbnail worker 都没跟 Activity 生命周期挂钩
        // → terminateQt + getQtThread().exit() 等不到这些线程退出 → 卡 10 秒
        // → ATM 触发 "Destroy timeout of remove-task" 强杀。这 10 秒里如果
        // 用户立刻重开 Konan,新 activity 启动被排队等老 task 销毁完 → 用户
        // 视角就是"卡黑屏"。
        //
        // isFinishing()=true 表示 Activity **真的**不再回来(不是 config 改变
        // 临时销毁),这种情况下我们对进程做硬杀:
        //   - 不调 super.onDestroy:跳过 Qt 的 terminateQt/QtThread.exit 阻塞
        //   - SIGKILL 整个进程:Activity / Qt / 后台线程一起干净走人
        //   - 不抛 SuperNotCalledException:进程已死,框架检查触不到
        //   - 下次重开是干净 cold start,FG service 已经因 stopWithTask=true
        //     停掉,无残留
        if (isFinishing()) {
            android.util.Log.i(LOG_TAG,
                "onDestroy: isFinishing=true, SIGKILL **before** super.onDestroy "
                + "to bypass Qt's synchronous terminateQt + QtThread.exit() drain "
                + "(those block 10s on background threads not tied to Activity lifecycle).");
            android.os.Process.killProcess(android.os.Process.myPid());
            return;  // unreachable but Java requires it
        }
        // 非 finishing(config change 等)走正常路径,Qt 状态需要保留。
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        // 让 getIntent() 返回最新这条 intent。
        setIntent(intent);
        android.util.Log.i(LOG_TAG, "onNewIntent: action=" +
            (intent != null ? intent.getAction() : "null"));
        // 主动通知 native 立刻重读 intent。**不能**只靠 applicationStateChanged
        // → Active 钩子 —— Konan 已经在前台时(用户分享分屏 / share chooser
        // 直接命中前台 task)Android 不一定让 Qt 发出 Active 变更,checkIntent
        // 永远等不到触发,表现就是"分享图片必须 force-stop 才能让 Konan 看到"。
        try {
            nativeOnNewIntent();
            android.util.Log.i(LOG_TAG, "onNewIntent: nativeOnNewIntent dispatched");
        } catch (UnsatisfiedLinkError e) {
            // libKonan_app.so 还没加载完(理论上 onNewIntent 时已经加载,这里
            // 只是兜底)。冷启动那条路径不依赖这个回调,会丢失这一拍但下次
            // share 应该正常。
            android.util.Log.w(LOG_TAG, "onNewIntent: nativeOnNewIntent UnsatisfiedLinkError", e);
        }
    }

    /// 由 ShareReceiverAndroid 实现的 JNI 导出符号
    /// (Java_<pkg>_KonanQtActivity_nativeOnNewIntent)。功能就是把 checkIntent
    /// queued 到 Qt 主线程,立刻读 getIntent() 拿到的最新 SEND payload。
    private static native void nativeOnNewIntent();

    /// 探测当前进程里 Qt 是否已经初始化过(用反射,因为
    /// QtNative.getStateDetails() / ApplicationStateDetails.isStarted 都是
    /// package-private,我们这个 com.hbb.konan 包够不着)。
    /// getDeclaredField + setAccessible(true) 才能拿到 private/默认访问的
    /// 字段(普通 getField 只能拿 public)。
    /// 任何反射失败都返回 false,走默认 super.onCreate 路径,不阻断启动。
    private static boolean isQtAlreadyStartedInThisProcess() {
        try {
            Class<?> qtNative = Class.forName("org.qtproject.qt.android.QtNative");
            java.lang.reflect.Method getStateDetails =
                qtNative.getDeclaredMethod("getStateDetails");
            getStateDetails.setAccessible(true);
            Object details = getStateDetails.invoke(null);
            if (details == null) return false;
            java.lang.reflect.Field f =
                details.getClass().getDeclaredField("isStarted");
            f.setAccessible(true);
            return f.getBoolean(details);
        } catch (Throwable t) {
            android.util.Log.w(LOG_TAG,
                "isQtAlreadyStartedInThisProcess: reflection failed", t);
            return false;
        }
    }
}
