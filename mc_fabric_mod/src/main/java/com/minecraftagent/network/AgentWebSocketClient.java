package com.minecraftagent.network;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.minecraftagent.action.ActionExecutor;
import com.minecraftagent.event.DemoRecordingManager;
import com.minecraftagent.util.AgentLogger;
import net.minecraft.server.MinecraftServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Minecraft Mod 侧的 WebSocket 客户端。
 *
 * 架构说明：
 *   Mod (Client) ←──── ws://localhost:8765 ────► Python (Server)
 *
 * 消息流向：
 *   Mod → Python : player_chat（玩家发言 + 游戏状态）
 *   Python → Mod : action（执行游戏行动指令）
 *   Mod → Python : observation（行动结果回报）
 *   Python → Mod : final_response（最终对话回复，显示给玩家）
 *
 * 重连策略：连接断开后每 5 秒自动重试，直到 disconnect() 被调用。
 *
 * 线程安全说明：
 *   - onText/onClose/onError 在 WebSocket 的内部线程调用
 *   - ActionExecutor.execute() 必须切换到 MC 服务器主线程（server.execute()）
 */
public class AgentWebSocketClient {

    private static final Logger LOGGER = LoggerFactory.getLogger("AgentWSClient");
    private static final Gson GSON = new Gson();
    private static final int RECONNECT_DELAY_MS = 5_000;
    private static final int CONNECT_TIMEOUT_SEC = 10;

    private final String serverUri;
    private final MinecraftServer server;
    private final ActionExecutor actionExecutor;
    private final HttpClient httpClient;

    private WebSocket webSocket;
    private final AtomicBoolean connected = new AtomicBoolean(false);
    private final AtomicBoolean shouldRun = new AtomicBoolean(true);

    // 用于拼接分帧到达的 WebSocket 文本消息
    private final StringBuilder messageBuffer = new StringBuilder();

    public AgentWebSocketClient(String serverUri, MinecraftServer server) {
        this.serverUri = serverUri;
        this.server = server;
        this.actionExecutor = new ActionExecutor(server, this);
        this.httpClient = HttpClient.newHttpClient();
    }

    // ──────────────────────────────────────────────────────────────────────
    // 连接管理
    // ──────────────────────────────────────────────────────────────────────

    /**
     * 在后台线程中启动 WebSocket 连接（含自动重连循环）。
     */
    public void connectAsync() {
        Thread thread = new Thread(() -> {
            while (shouldRun.get()) {
                try {
                    LOGGER.info("正在连接 Python Agent 服务器: {}", serverUri);
                    doConnect();
                } catch (Exception e) {
                    LOGGER.warn("连接失败，{}ms 后重试: {}", RECONNECT_DELAY_MS, e.getMessage());
                    connected.set(false);
                    sleepSilently(RECONNECT_DELAY_MS);
                }
            }
            LOGGER.info("WebSocket 客户端已停止");
        }, "agent-ws-client");
        thread.setDaemon(true);
        thread.start();
    }

    private void doConnect() throws Exception {
        webSocket = httpClient.newWebSocketBuilder()
                .buildAsync(URI.create(serverUri), new Listener())
                .get(CONNECT_TIMEOUT_SEC, TimeUnit.SECONDS);

        // 保持线程存活，直到连接断开或停止信号
        while (connected.get() && shouldRun.get()) {
            sleepSilently(500);
        }
    }

    /**
     * 优雅断开连接，停止重连循环。
     */
    public void disconnect() {
        shouldRun.set(false);
        connected.set(false);
        if (webSocket != null) {
            webSocket.sendClose(WebSocket.NORMAL_CLOSURE, "Server stopping").join();
        }
        LOGGER.info("WebSocket 连接已关闭");
    }

    public boolean isConnected() {
        return connected.get();
    }

    // ──────────────────────────────────────────────────────────────────────
    // 消息发送
    // ──────────────────────────────────────────────────────────────────────

    /**
     * 向 Python Agent 发送 JSON 消息（线程安全）。
     */
    public void sendToAgent(JsonObject message) {
        if (!connected.get() || webSocket == null) {
            LOGGER.warn("未连接到 Python 服务器，消息丢弃: type={}", message.get("type"));
            return;
        }
        try {
            webSocket.sendText(GSON.toJson(message), true);
        } catch (Exception e) {
            LOGGER.error("发送消息失败: {}", e.getMessage());
        }
    }

    /**
     * 向 Python 发送行动观察结果 + 游戏状态快照（v2）。
     *
     * game_state_update 会被 Python 的 connection_manager.resolve_observation() 提取，
     * 并在 react_agent._execute_action() 中 merge 进当前 game_state，
     * 使 LLM 在下一步推理时能看到背包/位置的最新变化。
     *
     * @param requestId       对应行动的请求 ID
     * @param success         行动是否成功
     * @param observation     人类可读的观察结果字符串
     * @param gameStateUpdate 行动后的游戏状态快照（背包 + Agent 位置等）
     */
    public void sendObservationWithState(
            String requestId, boolean success,
            String observation, JsonObject gameStateUpdate) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "observation");
        msg.addProperty("request_id", requestId);
        msg.addProperty("success", success);
        msg.addProperty("observation", observation);
        if (gameStateUpdate != null && gameStateUpdate.size() > 0) {
            msg.add("game_state_update", gameStateUpdate);
        }
        sendToAgent(msg);
    }

    /** 兼容旧调用（无状态快照版本，保留给外部代码使用） */
    public void sendObservation(String requestId, boolean success, String observation) {
        sendObservationWithState(requestId, success, observation, null);
    }

    // ──────────────────────────────────────────────────────────────────────
    // 消息处理
    // ──────────────────────────────────────────────────────────────────────

    private void handleMessage(String rawJson) {
        JsonObject json;
        try {
            json = JsonParser.parseString(rawJson).getAsJsonObject();
        } catch (Exception e) {
            LOGGER.error("消息 JSON 解析失败: {} | 原文: {}", e.getMessage(), rawJson.substring(0, Math.min(200, rawJson.length())));
            return;
        }

        if (!json.has("type")) {
            LOGGER.warn("收到无 type 字段的消息，忽略");
            return;
        }

        String type = json.get("type").getAsString();
        LOGGER.debug("收到消息类型: {}", type);

        switch (type) {

            case "action" -> {
                // Python 要求执行一个游戏行动，执行后将 observation + game_state_update 回传
                String requestId   = json.has("request_id")    ? json.get("request_id").getAsString()    : "unknown";
                String actionType  = json.has("action_type")   ? json.get("action_type").getAsString()   : "chat";
                String displayMsg  = json.has("display_message")? json.get("display_message").getAsString(): "";
                JsonObject params  = json.has("action_params") && json.get("action_params").isJsonObject()
                        ? json.getAsJsonObject("action_params")
                        : new JsonObject();

                AgentLogger.ws("RECV", "action", actionType + " | req=" + requestId);

                // 切换到 MC 服务器主线程执行（Minecraft API 非线程安全）
                server.execute(() -> {
                    // v3：move_to/mine_block 通过客户端模拟 WASD+鼠标输入执行
                    actionExecutor.executeWithState(requestId, actionType, params, displayMsg, (result) -> {
                        AgentLogger.ws("SEND", "observation", "req=" + requestId + " | success=" + result.success() + " | " + result.observation());
                        sendObservationWithState(requestId, result.success(), result.observation(), result.gameStateUpdate());
                    });
                });
            }

            case "final_response" -> {
                // ReAct 循环结束，Python 发来最终对话消息显示给玩家
                String displayMsg = json.has("display_message") ? json.get("display_message").getAsString() : "";
                if (!displayMsg.isEmpty()) {
                    server.execute(() -> actionExecutor.broadcastAgentMessage(displayMsg));
                }
            }

            case "record_demo" -> {
                String cmd = json.has("command") ? json.get("command").getAsString() : "";
                String skillName = json.has("name") ? json.get("name").getAsString() : null;
                String playerName = json.has("player_name") ? json.get("player_name").getAsString() : null;
                if ("start".equalsIgnoreCase(cmd)) {
                    if (playerName == null || playerName.isBlank()) {
                        var first = server.getPlayerManager().getPlayerList().stream().findFirst();
                        playerName = first.map(p -> p.getName().getString()).orElse(null);
                    }
                    if (playerName != null) {
                        DemoRecordingManager.start(this, playerName, skillName);
                    }
                } else if ("stop".equalsIgnoreCase(cmd)) {
                    DemoRecordingManager.stop();
                }
            }

            default -> LOGGER.warn("未知消息类型: {}", type);
        }
    }

    // ──────────────────────────────────────────────────────────────────────
    // WebSocket Listener（内部类）
    // ──────────────────────────────────────────────────────────────────────

    private class Listener implements WebSocket.Listener {

        @Override
        public void onOpen(WebSocket ws) {
            LOGGER.info("已连接到 Python Agent 服务器 ✓");
            AgentLogger.info("WebSocket", "已成功连接到 Python Agent: " + serverUri);
            connected.set(true);
            ws.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket ws, CharSequence data, boolean last) {
            messageBuffer.append(data);
            if (last) {
                String fullMessage = messageBuffer.toString();
                messageBuffer.setLength(0);
                handleMessage(fullMessage);
            }
            ws.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket ws, int statusCode, String reason) {
            LOGGER.warn("WebSocket 已关闭: {} - {}", statusCode, reason);
            AgentLogger.warn("WebSocket", "连接关闭: " + statusCode + " - " + reason);
            connected.set(false);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public void onError(WebSocket ws, Throwable error) {
            LOGGER.error("WebSocket 错误: {}", error.getMessage());
            AgentLogger.error("WebSocket", "连接错误: " + error.getMessage());
            connected.set(false);
        }
    }

    // ──────────────────────────────────────────────────────────────────────
    // 工具方法
    // ──────────────────────────────────────────────────────────────────────

    private static void sleepSilently(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
