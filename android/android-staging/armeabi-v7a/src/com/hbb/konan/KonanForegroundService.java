// ============================================================================
// KonanForegroundService —— 进程保活前台服务。
//
// 为什么需要这个 Java 文件:
//   android.app.Service 必须是 Java/Kotlin 子类(系统通过反射拉起),纯
//   C++ 绕不过去。这个类只做最小的"挂上常驻通知 + 保住进程"的工作,
//   真正的同步引擎、剪贴板写入仍然在 Qt 主线程的 C++ 侧。
//
// 为什么需要保活进程:
//   Konan 切到后台后,Android 在内存压力下会清掉应用进程。一旦进程被杀,
//   DirectorySyncEngine 和 ClipboardSyncService 全死,别的设备推过来的
//   .clipboard 文件没人接,用户切回来时只看到陈旧状态。挂一个 fgs 让
//   ActivityManager 把本进程当"用户感知的服务"对待,不在常规回收名单。
//
// 写入系统剪贴板的兼容性:
//   Android 10+ 限制只针对剪贴板"读"(getPrimaryClip),"写"
//   (setPrimaryClip)在背景进程也允许。但前提是进程还活着 —— 这恰好是
//   本服务的作用。
//
// 通知 channel:
//   IMPORTANCE_LOW 让通知不发声、不弹横幅,只在状态栏常驻一行。用户可以
//   在系统设置 → App → 通知里再细调,或点通知里的 "Stop" action 主动
//   把同步停掉(同时 stopForeground 把通知摘掉,让出进程被回收的权利)。
//
// **本文件是 .java.in 模板**:`com.hbb.konan` 占位符在 CMake build 时
// 替换成顶层 APP_BUNDLE_ID 值,Java 包名跟应用 ID 自动保持一致。
// ============================================================================
package com.hbb.konan;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;

public class KonanForegroundService extends Service {

    private static final String CHANNEL_ID = "konan-sync";
    private static final int    NOTIF_ID   = 1001;
    private static final int    REQ_POST_NOTIFICATIONS = 1002;

    /// 由 C++ 侧 AndroidForegroundService::ensureNotificationsPermission 调用。
    /// 必须传 Activity(QtNative.activity()),Service / 普通 Context 调
    /// requestPermissions 没用 —— 弹窗依赖 Activity 上下文。
    ///
    /// 行为:
    ///   - SDK < 33:无该权限概念,直接返回
    ///   - 已授权:返回
    ///   - 未授权:异步弹系统对话框(不阻塞调用方);用户的选择走
    ///     QtActivity.onRequestPermissionsResult,默认转给 Qt 的 androidx
    ///     回调,我们这边 fire-and-forget。下次 App 启动时 checkSelfPermission
    ///     会反映最新结果,通知可见性自然恢复。
    public static void requestPostNotificationsPermission(final Activity activity) {
        if (Build.VERSION.SDK_INT < 33) return;
        if (activity == null) return;
        try {
            int granted = activity.checkSelfPermission(
                "android.permission.POST_NOTIFICATIONS");
            if (granted == PackageManager.PERMISSION_GRANTED) return;

            // **必须**回到 Android UI 线程调 requestPermissions。从 Qt 主线程
            // (qtMainLoopThread)直接调,Android 14+ 内部 ActivityResultRegistry
            // 走 LiveData 时会撞 IllegalStateException;更隐蔽的是弹窗时序与
            // QtActivity 的 surface 生命周期错位,导致 QSGRenderLoop 第一次
            // 创建 EGL context 时拿到无效 ANativeWindow,直接 qFatal 闪退
            // ("Failed to initialize graphics backend for OpenGL")。
            activity.runOnUiThread(new Runnable() {
                @Override public void run() {
                    try {
                        activity.requestPermissions(
                            new String[]{"android.permission.POST_NOTIFICATIONS"},
                            REQ_POST_NOTIFICATIONS);
                    } catch (Throwable t) { }
                }
            });
        } catch (Throwable t) {
            // 旧 Activity 实现少了 checkSelfPermission/requestPermissions
            // (理论上 API 23+ 都有),就当用户已拒绝,服务继续跑只是没通知。
        }
    }

    /// 用户在通知里点 Stop 时,系统通过这个 action 广播回来。
    private static final String ACTION_STOP = "com.hbb.konan.KonanForegroundService.STOP";

    /// startForegroundService(intent) 触发的命令,和 ACTION_STOP 区分。
    private static final String ACTION_START = "com.hbb.konan.KonanForegroundService.START";

    private BroadcastReceiver mStopReceiver;

    @Override
    public void onCreate() {
        super.onCreate();
        ensureChannel();

        // 订阅 Stop 广播。注意 Android 14+ 必须在 registerReceiver 时显式
        // 声明 RECEIVER_NOT_EXPORTED,否则启动直接抛 SecurityException。
        mStopReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context ctx, Intent intent) {
                stopSelf();
            }
        };
        IntentFilter filter = new IntentFilter(ACTION_STOP);
        if (Build.VERSION.SDK_INT >= 33) {
            // Context.RECEIVER_NOT_EXPORTED == 4,常量在低版本 SDK 上没有
            registerReceiver(mStopReceiver, filter, /*RECEIVER_NOT_EXPORTED*/ 4);
        } else {
            registerReceiver(mStopReceiver, filter);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = buildNotification();

        // Android 14+ 必须显式传 foregroundServiceType,且要和 manifest
        // 声明的 dataSync 一致,不然抛 InvalidForegroundServiceTypeException。
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIF_ID, n);
        }

        // START_STICKY:进程被系统强杀后重启服务,继续保活。重启时 intent
        // 为 null,onStartCommand 不会重新跑用户态业务(那些挂在 Activity
        // 启动里),但通知和保活仍然恢复 —— 用户下次拉起 Activity 时无缝。
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;  // 不允许 bind
    }

    @Override
    public void onDestroy() {
        if (mStopReceiver != null) {
            try { unregisterReceiver(mStopReceiver); } catch (Throwable t) { }
            mStopReceiver = null;
        }
        super.onDestroy();
    }

    /// 创建低优先级通知 channel(只创一次,重复调 createNotificationChannel
    /// 是无副作用的)。
    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return;  // pre-Oreo 不需要 channel
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel ch = new NotificationChannel(
            CHANNEL_ID, "Konan 后台同步", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("保持剪贴板/收藏在后台同步,关闭后切到别的应用将不再收到推送。");
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }

    /// 构造常驻通知:点击主体回到 Activity,右侧 Stop action 停服务。
    private Notification buildNotification() {
        // 主体 PendingIntent:回到主 Activity(KonanQtActivity:QtActivity 的
        // 子类,fix 了分享 intent 的 setIntent 问题)。
        Intent activityIntent = new Intent(this,
            com.hbb.konan.KonanQtActivity.class);
        activityIntent.setFlags(
            Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) piFlags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent contentPi = PendingIntent.getActivity(
            this, 0, activityIntent, piFlags);

        // Stop action 的 PendingIntent:发本进程内的 STOP 广播。
        Intent stopIntent = new Intent(ACTION_STOP).setPackage(getPackageName());
        PendingIntent stopPi = PendingIntent.getBroadcast(
            this, 0, stopIntent, piFlags);

        // 用 Notification.Builder 而不是 NotificationCompat,避免再拉
        // androidx.core 依赖 —— manifest 已经把 androidx.core 1.13.1 引入,
        // 但前台服务是冷启动路径,能少 import 一个就少一个。
        // 直接用带 channelId 的构造函数: minSdk = qtMinSdkVersion (28) > 26,
        // pre-Oreo 分支永远走不到, 还会触发 deprecation warning, 删掉.
        Notification.Builder b = new Notification.Builder(this, CHANNEL_ID);
        b.setContentTitle("Konan 同步运行中")
         .setContentText("后台保持剪贴板/收藏同步")
         .setSmallIcon(getApplicationInfo().icon)
         .setOngoing(true)
         .setContentIntent(contentPi);

        // Stop action: addAction(int, CharSequence, PendingIntent) API 23 起
        // 已 deprecated, 用 Notification.Action.Builder. Icon 传 null 表示
        // 这个 action 没图标 (UI 通常忽略).
        Notification.Action stopAction = new Notification.Action.Builder(
            (Icon) null, "停止", stopPi).build();
        b.addAction(stopAction);

        return b.build();
    }
}
