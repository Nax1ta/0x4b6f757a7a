package dev.kouzz.console;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.Callable;

/** Only the four documented routes are exposed. No credentials or caller supplied URLs. */
final class NativeApi {
    private final SharedPreferences prefs;
    private final ExecutorService roomsPool = Executors.newFixedThreadPool(4);
    private final Object lock = new Object();
    private final Object refreshLock = new Object();
    private final Object healthLock = new Object();
    private final java.util.LinkedHashMap<String, RunSnapshot> runsCache = new java.util.LinkedHashMap<>(8, 0.75f, true);
    private final java.util.LinkedHashMap<String, RunSnapshot> healthCache = new java.util.LinkedHashMap<>(8, 0.75f, true);
    private JSONObject settings;
    private JSONObject observations;

    private static final class RunSnapshot {
        final long at = System.nanoTime();
        final String checkedAt = now();
        final JSONObject data;
        final String error;
        RunSnapshot(JSONObject data, String error) { this.data = data; this.error = error; }
    }

    NativeApi(Context context) {
        prefs = context.getSharedPreferences("console-v1", Context.MODE_PRIVATE);
        try {
            settings = prefs.contains("settings") ? validateSettings(new JSONObject(prefs.getString("settings", "{}"))) : bundledDefaults(context);
            observations = new JSONObject(prefs.getString("observations", "{}"));
        } catch (Exception e) {
            // Corrupt state is surfaced rather than silently replacing user settings.
            settings = null;
            observations = new JSONObject();
        }
    }

    Object handle(String method, String path, String body) throws Exception {
        if (settings == null) throw new IllegalStateException("本地设置损坏，请在系统应用设置清除数据后重试");
        if ("GET".equals(method) && "/api/settings".equals(path)) {
            synchronized (lock) { return copy(settings); }
        }
        if ("PUT".equals(method) && "/api/settings".equals(path)) {
            JSONObject input = new JSONObject(body == null ? "{}" : body);
            synchronized (refreshLock) {
                synchronized (lock) {
                    JSONObject merged = copy(settings);
                    java.util.Iterator<String> keys = input.keys();
                    while (keys.hasNext()) {
                        String key = keys.next();
                        if (!isSetting(key)) throw new IllegalArgumentException("不支持的设置: " + key);
                        merged.put(key, input.get(key));
                    }
                    JSONObject updated = validateSettings(merged);
                    if (!prefs.edit().putString("settings", updated.toString()).commit()) throw new IllegalStateException("设置保存失败");
                    settings = updated;
                    return copy(settings);
                }
            }
        }
        if ("GET".equals(method) && "/api/status".equals(path)) return status();
        if ("POST".equals(method) && "/api/refresh".equals(path)) return refresh();
        if ("GET".equals(method) && "/api/health".equals(path)) return health();
        throw new IllegalArgumentException("不支持的原生 API: " + method + " " + path);
    }

    private static boolean isSetting(String key) {
        return "rooms".equals(key) || "repository".equals(key) || "refresh_interval".equals(key) ||
                "recipients".equals(key) || "workflow".equals(key);
    }

    private static JSONObject defaults() throws Exception {
        return new JSONObject().put("rooms", new JSONArray())
                .put("repository", "Nax1ta/0x4b6f757a7a").put("refresh_interval", 60)
                .put("recipients", new JSONArray()).put("workflow", "poll-notify.yml");
    }

    private static JSONObject bundledDefaults(Context context) throws Exception {
        JSONObject base = defaults();
        try (InputStream input = context.getAssets().open("client-defaults.json");
             ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int count;
            while ((count = input.read(buffer)) != -1) {
                if (bytes.size() + count > 65536) throw new IllegalArgumentException("预填设置文件过大");
                bytes.write(buffer, 0, count);
            }
            JSONObject bundled = new JSONObject(bytes.toString("UTF-8"));
            // The optional build artifact exports only public identifiers, never credentials.
            if (bundled.has("rooms")) base.put("rooms", bundled.getJSONArray("rooms"));
            if (bundled.has("repository")) base.put("repository", bundled.getString("repository"));
        } catch (java.io.FileNotFoundException ignored) {
            // Public source builds intentionally start with an empty watchlist.
        }
        return validateSettings(base);
    }

    private static JSONObject validateSettings(JSONObject input) throws Exception {
        JSONObject out = defaults();
        String repo = input.getString("repository");
        if (!repo.matches("[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}") || repo.endsWith("/.."))
            throw new IllegalArgumentException("repository 格式应为 owner/repo");
        out.put("repository", repo);
        String workflow = input.optString("workflow", "poll-notify.yml");
        if (!workflow.matches("[A-Za-z0-9_.-]{1,100}\\.ya?ml")) throw new IllegalArgumentException("workflow 文件名无效");
        out.put("workflow", workflow);
        int interval = input.optInt("refresh_interval", 60);
        if (interval < 15 || interval > 3600) throw new IllegalArgumentException("刷新间隔应为 15–3600 秒");
        out.put("refresh_interval", interval);
        JSONArray rooms = input.getJSONArray("rooms");
        if (rooms.length() > 30) throw new IllegalArgumentException("最多配置 30 个房间");
        JSONArray clean = new JSONArray();
        Set<String> ids = new HashSet<>();
        for (int i = 0; i < rooms.length(); i++) {
            JSONObject room = rooms.getJSONObject(i);
            String id = room.optString("id", "");
            if (!id.matches("[1-9][0-9]{0,19}") || !ids.add(id)) throw new IllegalArgumentException("房间号无效或重复");
            String name = room.optString("name", id).trim();
            if (name.length() > 100) throw new IllegalArgumentException("房间名称过长");
            clean.put(new JSONObject().put("id", id).put("name", name.isEmpty() ? id : name));
        }
        out.put("rooms", clean);
        JSONArray recipients = input.optJSONArray("recipients");
        JSONArray emails = new JSONArray();
        if (recipients != null) {
            if (recipients.length() > 50) throw new IllegalArgumentException("收件人过多");
            for (int i = 0; i < recipients.length(); i++) {
                String email = recipients.getString(i).trim();
                if (email.length() > 254 || !email.matches("[^\\s@]+@[^\\s@]+\\.[^\\s@]+"))
                    throw new IllegalArgumentException("收件人地址无效");
                emails.put(email);
            }
        }
        out.put("recipients", emails);
        return out;
    }

    private JSONObject status() throws Exception {
        synchronized (lock) {
            JSONArray rooms = new JSONArray();
            JSONArray configured = settings.getJSONArray("rooms");
            for (int i = 0; i < configured.length(); i++) {
                JSONObject config = configured.getJSONObject(i);
                String id = config.getString("id");
                JSONObject previous = observations.optJSONObject(id);
                JSONObject room = previous == null ? blank(config) : copy(previous);
                room.put("name", config.getString("name"));
                // Cached observations must not appear as a fresh network result after launch.
                room.put("stale", previous == null || "unknown".equals(room.optString("status")) || age(room.optString("checked_at", "")) > settings.getInt("refresh_interval") * 2L);
                rooms.put(room);
            }
            return new JSONObject().put("rooms", rooms).put("settings", copy(settings))
                    .put("platform", "android").put("checked_at", nullable(prefs.getString("checked_at", null)))
                    .put("mail_available", false).put("mail_notice", "安卓仅保存本机设置；邮件由仓库 Actions 发送，本机配置不会同步到仓库。");
        }
    }

    private JSONObject refresh() throws Exception {
        synchronized (refreshLock) {
            JSONArray configured;
            JSONObject previous;
            synchronized (lock) {
                configured = new JSONArray(settings.getJSONArray("rooms").toString());
                previous = copy(observations);
            }
            List<Callable<JSONObject>> tasks = new ArrayList<>();
            for (int i = 0; i < configured.length(); i++) {
                JSONObject config = configured.getJSONObject(i);
                JSONObject old = previous.optJSONObject(config.getString("id"));
                tasks.add(() -> fetchRoom(config, old));
            }
            List<Future<JSONObject>> results = roomsPool.invokeAll(tasks, 22, java.util.concurrent.TimeUnit.SECONDS);
            JSONObject updated = new JSONObject();
            for (int i = 0; i < results.size(); i++) {
                JSONObject config = configured.getJSONObject(i);
                JSONObject room;
                try { room = results.get(i).get(); }
                catch (Exception e) {
                    JSONObject old = previous.optJSONObject(config.getString("id"));
                    room = old == null ? blank(config) : copy(old);
                    room.put("status", "unknown").put("live_status", JSONObject.NULL)
                            .put("error", "查询超时或已取消，请重试").put("attempted_at", now()).put("stale", true);
                }
                updated.put(config.getString("id"), room);
            }
            synchronized (lock) {
                String checked = now();
                if (!prefs.edit().putString("observations", updated.toString()).putString("checked_at", checked).commit())
                    throw new IllegalStateException("观测结果保存失败");
                observations = updated;
            }
            return status();
        }
    }

    private static JSONObject blank(JSONObject config) throws Exception {
        return copy(config).put("status", "unknown").put("live_status", JSONObject.NULL)
                .put("title", "").put("cover", "").put("url", "https://live.bilibili.com/" + config.getString("id"))
                .put("live_started_at", JSONObject.NULL).put("last_live_at", JSONObject.NULL)
                .put("checked_at", JSONObject.NULL).put("error", JSONObject.NULL).put("metadata_partial", true);
    }

    private static JSONObject fetchRoom(JSONObject config, JSONObject previous) throws Exception {
        JSONObject result = previous == null ? blank(config) : copy(previous);
        String id = config.getString("id");
        result.put("name", config.getString("name")).put("attempted_at", now());
        JSONObject info = null;
        String source = null;
        String failure = "";
        try {
            info = roomData("/room/v1/Room/get_info?room_id=" + id);
            source = "/room/v1/Room/get_info";
        } catch (Exception e) { failure = e.getMessage(); }
        if (info == null) {
            try {
                info = roomData("/room/v1/Room/room_init?id=" + id);
                source = "/room/v1/Room/room_init";
                // room_init often lacks title/cover; retry get_info with the canonical ID.
                String canonical = info.optString("room_id", id);
                if (canonical.matches("[1-9][0-9]{0,19}")) {
                    try {
                        JSONObject metadata = roomData("/room/v1/Room/get_info?room_id=" + canonical);
                        if (metadata.getInt("live_status") == info.getInt("live_status")) {
                            for (String key : new String[]{"title", "user_cover", "cover", "keyframe", "live_time", "live_start_time"})
                                if (metadata.has(key)) info.put(key, metadata.get(key));
                        }
                    } catch (Exception ignored) { }
                }
            } catch (Exception e) { failure += "; " + e.getMessage(); }
        }
        if (info == null) {
            return result.put("status", "unknown").put("live_status", JSONObject.NULL)
                    .put("error", failure).put("stale", true);
        }
        int liveStatus = info.getInt("live_status");
        boolean live = liveStatus == 1;
        String checked = now();
        String started = parseTime(info.opt("live_start_time"));
        if (started == null) started = parseTime(info.opt("live_time"));
        String title = info.opt("title") instanceof String ? info.getString("title") : "";
        String cover = "";
        for (String key : new String[]{"user_cover", "cover", "keyframe"}) {
            String candidate = info.optString(key, "");
            if (candidate.startsWith("//")) candidate = "https:" + candidate;
            if (candidate.startsWith("https://")) { cover = candidate; break; }
        }
        if (!title.isEmpty()) result.put("title", title);
        if (!cover.isEmpty()) result.put("cover", cover);
        result.put("metadata_partial", title.isEmpty() || cover.isEmpty());
        result.put("status", live ? "live" : "offline").put("live_status", liveStatus)
                .put("source", source).put("error", JSONObject.NULL).put("checked_at", checked)
                .put("last_success_at", checked).put("stale", false)
                .put("live_started_at", live ? nullable(started) : JSONObject.NULL);
        if (started != null) {
            result.put("last_live_at", started).put("last_live_estimated", false);
        } else if (live && !(previous != null && previous.optBoolean("observed_live"))) {
            result.put("last_live_at", checked).put("last_live_estimated", true);
        }
        return result.put("observed_live", live);
    }

    private static JSONObject roomData(String path) throws Exception {
        JSONObject response = getJson("https://api.live.bilibili.com" + path);
        if (response.optInt("code", -1) != 0) throw new IllegalStateException("B站 API code " + response.optInt("code", -1));
        JSONObject data = response.getJSONObject("data");
        Object status = data.opt("live_status");
        if (!(status instanceof Integer || status instanceof Long) || ((Number) status).longValue() < 0 || ((Number) status).longValue() > 2)
            throw new IllegalStateException("B站返回无效 live_status");
        return data;
    }

    private JSONObject health() throws Exception {
        JSONObject config;
        synchronized (lock) { config = copy(settings); }
        synchronized (healthLock) {
            String key = config.getString("repository").toLowerCase(java.util.Locale.ROOT) + "/" + config.getString("workflow");
            RunSnapshot cached = healthCache.get(key);
            if (cached == null || System.nanoTime() - cached.at >= java.util.concurrent.TimeUnit.SECONDS.toNanos(180)) {
                cached = new RunSnapshot(fetchHealth(config), null);
                healthCache.put(key, cached);
                if (healthCache.size() > 8) healthCache.remove(healthCache.keySet().iterator().next());
            }
            JSONObject result = copy(cached.data);
            // A cached heartbeat may cross the freshness boundary before its cache expires.
            if (!result.isNull("last_checked_at") && age(result.optString("last_checked_at")) > 900)
                result.put("stale", true).put("status", "stale");
            return result;
        }
    }

    private JSONObject fetchHealth(JSONObject config) throws Exception {
        String repo = config.getString("repository");
        JSONObject result = new JSONObject().put("repository", repo).put("checked_at", now())
                .put("source", "github_public_api").put("mail_available", false)
                .put("mail_status", "unknown")
                .put("last_checked_at", JSONObject.NULL).put("last_mail_at", JSONObject.NULL)
                .put("pending", JSONObject.NULL).put("failures", JSONObject.NULL)
                .put("repository_url", "https://github.com/" + repo).put("stale", true)
                .put("delivery_note", "accepted 表示发件服务器接受，不代表已进入收件箱");
        String heartbeatError = null;
        try {
            JSONObject heartbeat = getJson("https://api.github.com/repos/" + repo + "/contents/health.json?ref=orbit-state",
                    "application/vnd.github.raw+json");
            if (heartbeat.has("content")) {
                if (!"base64".equalsIgnoreCase(heartbeat.optString("encoding", "base64")) || !(heartbeat.opt("content") instanceof String))
                    throw new IllegalStateException("不支持的 GitHub Contents 编码");
                byte[] decoded = java.util.Base64.getDecoder().decode(heartbeat.getString("content").replaceAll("\\s", ""));
                heartbeat = new JSONObject(new String(decoded, java.nio.charset.StandardCharsets.UTF_8));
            }
            String checked = heartbeat.optString("checked_at", "");
            boolean stale = age(checked) > 900;
            String reported = heartbeat.optString("status", "unknown");
            if (!("ok".equals(reported) || "degraded".equals(reported))) reported = "unknown";
            result.put("status", stale ? "stale" : reported).put("stale", stale)
                    .put("last_checked_at", age(checked) == Long.MAX_VALUE ? JSONObject.NULL : checked);
            String mailed = heartbeat.optString("last_mail_at", "");
            result.put("last_mail_at", age(mailed) == Long.MAX_VALUE ? JSONObject.NULL : mailed);
            Object pending = heartbeat.opt("pending");
            if (pending instanceof Number || pending instanceof Boolean || pending instanceof JSONArray) result.put("pending", pending);
            if (heartbeat.opt("mail_status") instanceof String) result.put("mail_status", heartbeat.getString("mail_status"));
            for (String key : new String[]{"run_url", "saved_at"}) result.put(key, heartbeat.opt(key));
        } catch (Exception e) {
            heartbeatError = "尚未取得云端心跳：" + e.getMessage();
            result.put("status", "unknown");
        }
        try {
            RunSnapshot snapshot = actionsRuns(repo, config.getString("workflow"));
            result.put("actions_checked_at", snapshot.checkedAt);
            if (snapshot.error != null) throw new IllegalStateException(snapshot.error);
            JSONObject data = snapshot.data;
            JSONArray all = data.getJSONArray("workflow_runs");
            JSONArray runs = new JSONArray();
            JSONObject latest = null;
            int failures = 0;
            for (int i = 0; i < all.length(); i++) {
                JSONObject run = all.getJSONObject(i);
                JSONObject safe = new JSONObject();
                for (String key : new String[]{"id", "name", "status", "conclusion", "created_at", "updated_at", "html_url", "event", "run_number"})
                    safe.put(key, run.opt(key));
                runs.put(safe);
                if ("failure".equals(run.optString("conclusion"))) failures++;
                if (latest == null) latest = safe;
            }
            if (!result.optBoolean("stale", true) && latest != null && ("failure".equals(latest.optString("conclusion")) || "timed_out".equals(latest.optString("conclusion"))))
                result.put("status", "degraded");
            return result.put("runs", runs).put("latest_run", latest == null ? JSONObject.NULL : latest)
                    .put("failures", failures).put("error", nullable(heartbeatError));
        } catch (Exception e) {
            return result.put("runs", new JSONArray()).put("latest_run", JSONObject.NULL)
                    .put("error", (heartbeatError == null ? "" : heartbeatError + "; ") + "Actions 查询失败：" + e.getMessage());
        }
    }

    private RunSnapshot actionsRuns(String repo, String workflow) {
        synchronized (healthLock) {
            String key = repo.toLowerCase(java.util.Locale.ROOT) + "/" + workflow;
            RunSnapshot cached = runsCache.get(key);
            if (cached != null && System.nanoTime() - cached.at < java.util.concurrent.TimeUnit.SECONDS.toNanos(600)) return cached;
            RunSnapshot current;
            try {
                current = new RunSnapshot(getJson("https://api.github.com/repos/" + repo + "/actions/workflows/" + workflow + "/runs?per_page=15"), null);
            } catch (Exception e) {
                // Cache failures too: repeating unauthenticated 403s cannot repair a rate limit.
                current = new RunSnapshot(null, e.getMessage());
            }
            runsCache.put(key, current);
            if (runsCache.size() > 8) runsCache.remove(runsCache.keySet().iterator().next());
            return current;
        }
    }

    private static JSONObject getJson(String address) throws Exception {
        return getJson(address, "application/json");
    }

    private static JSONObject getJson(String address, String accept) throws Exception {
        boolean github = address.startsWith("https://api.github.com/");
        if (github) address += (address.contains("?") ? "&" : "?") + "_ts=" + System.currentTimeMillis();
        HttpURLConnection connection = (HttpURLConnection) new URL(address).openConnection();
        if (github) {
            connection.setUseCaches(false);
            connection.setRequestProperty("Cache-Control", "no-cache");
        }
        connection.setConnectTimeout(4000);
        connection.setReadTimeout(4000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("User-Agent", "Mozilla/5.0 KouzzConsole/1.0");
        connection.setRequestProperty("Accept", accept);
        if (address.startsWith("https://api.live.bilibili.com/")) connection.setRequestProperty("Referer", "https://live.bilibili.com/");
        try {
            int status = connection.getResponseCode();
            if (status != 200) throw new IllegalStateException("HTTP " + status);
            try (InputStream input = connection.getInputStream(); ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int count;
                long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
                while ((count = input.read(buffer)) != -1) {
                    if (bytes.size() + count > 4_000_000) throw new IllegalStateException("API 响应过大");
                    if (System.nanoTime() > deadline || Thread.currentThread().isInterrupted()) throw new IllegalStateException("请求超时或已取消");
                    bytes.write(buffer, 0, count);
                }
                return new JSONObject(bytes.toString("UTF-8"));
            }
        } finally { connection.disconnect(); }
    }

    private static String parseTime(Object value) {
        if (value == null || value == JSONObject.NULL) return null;
        try {
            String text = value.toString().trim();
            Instant instant;
            if (text.matches("[0-9]+")) {
                long seconds = Long.parseLong(text);
                if (seconds < 1_000_000_000L) return null;
                instant = Instant.ofEpochSecond(seconds);
            } else {
                if (text.isEmpty() || text.startsWith("0000")) return null;
                instant = LocalDateTime.parse(text, DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")).toInstant(ZoneOffset.ofHours(8));
            }
            if (instant.isAfter(Instant.now().plusSeconds(300))) return null;
            return instant.toString();
        } catch (Exception ignored) { return null; }
    }
    private static long age(String value) {
        try {
            long seconds = Instant.now().getEpochSecond() - Instant.parse(value).getEpochSecond();
            return seconds < -300 ? Long.MAX_VALUE : Math.max(0, seconds);
        }
        catch (Exception e) { return Long.MAX_VALUE; }
    }
    private static Object nullable(String value) { return value == null ? JSONObject.NULL : value; }
    private static String now() { return Instant.now().toString(); }
    private static JSONObject copy(JSONObject value) throws Exception { return new JSONObject(value.toString()); }
    void close() { roomsPool.shutdownNow(); }
}
