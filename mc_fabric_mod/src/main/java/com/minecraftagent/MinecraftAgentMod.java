package com.minecraftagent;

import com.google.gson.JsonObject;
import com.minecraftagent.event.DemoRecordingManager;
import com.minecraftagent.event.ServerEventHandler;
import com.minecraftagent.network.AgentNetworking;
import com.minecraftagent.network.AgentWebSocketClient;
import com.minecraftagent.util.AgentLogger;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Minecraft Agent Mod 主入口（服务端）。
 *
 * 仅操控主角（玩家），不生成任何 Agent 实体。
 * 初始化：服务器启动后连接 Python、注册聊天事件监听。
 *
 * Python 服务器地址可通过修改 PYTHON_WS_URL 常量调整。
 */
public class MinecraftAgentMod implements ModInitializer {

    public static final String MOD_ID = "minecraft_agent";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    /** Python Agent WebSocket Server 地址（与 Python main.py 中的端口保持一致） */
    private static final String PYTHON_WS_URL = "ws://localhost:8765";

    /** 全局 WebSocket 客户端实例（服务器运行期间有效） */
    private static AgentWebSocketClient wsClient;

    @Override
    public void onInitialize() {
        LOGGER.info("════════════════════════════════════════");
        LOGGER.info("  Minecraft Agent Mod 正在初始化...");
        LOGGER.info("════════════════════════════════════════");

        AgentNetworking.registerPayloadTypes();

        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            AgentNetworking.registerServerReceiver();
            LOGGER.info("服务器已启动，正在初始化 Agent...");

            AgentLogger.init();
            AgentLogger.info("MinecraftAgentMod", "服务器已启动，Agent 开始初始化");

            wsClient = new AgentWebSocketClient(PYTHON_WS_URL, server);
            wsClient.connectAsync();
            AgentLogger.info("MinecraftAgentMod", "WebSocket 客户端启动，目标: " + PYTHON_WS_URL);

            ServerEventHandler.register(server, wsClient);
            DemoRecordingManager.register(server);

            LOGGER.info("Agent 初始化完成（操控主角模式），等待 Python 连接: {}", PYTHON_WS_URL);
            AgentLogger.info("MinecraftAgentMod", "初始化完成，等待 Python 连接...");
        });

        // ③ 服务器停止前关闭 WebSocket 和日志
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            LOGGER.info("服务器正在停止，关闭 WebSocket 连接...");
            AgentLogger.info("MinecraftAgentMod", "服务器停止，关闭 WebSocket");
            if (wsClient != null) {
                wsClient.disconnect();
            }
            AgentLogger.close();  // 最后关闭日志文件
        });

        LOGGER.info("Mod 初始化完成（等待服务器启动）");
    }

    /** 获取当前 WebSocket 客户端（供其他模块使用） */
    public static AgentWebSocketClient getWsClient() {
        return wsClient;
    }
}
