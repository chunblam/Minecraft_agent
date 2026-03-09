package com.minecraftagent.event;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.minecraftagent.network.AgentWebSocketClient;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.event.player.PlayerBlockBreakEvents;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.math.BlockPos;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 演示录制：监听玩家移动与破坏方块，生成 trajectory 列表并回传 Python 存入技能库。
 *
 * 触发：Python 发送 record_demo { command: "start"|"stop", name?: "技能名" }
 * 回传：Mod 发送 demonstration_trajectory { name, trajectory: [{ action, action_params, observation }] }
 */
public class DemoRecordingManager {

    private static final Logger LOGGER = LoggerFactory.getLogger("DemoRecordingManager");
    private static final double MOVE_RECORD_THRESHOLD = 2.0;

    private static volatile boolean recording;
    private static volatile String recordingPlayerName;
    private static volatile String skillName;
    private static volatile AgentWebSocketClient wsClientRef;
    private static volatile double lastX, lastY, lastZ;
    private static final java.util.List<JsonObject> trajectory = new java.util.ArrayList<>();
    private static int tickCounter;

    public static void register(MinecraftServer server) {
        ServerTickEvents.END_SERVER_TICK.register(s -> tick(server));
        PlayerBlockBreakEvents.AFTER.register((world, player, pos, state, blockEntity) -> {
            if (!recording) return;
            if (!player.getName().getString().equals(recordingPlayerName)) return;
            synchronized (trajectory) {
                JsonObject step = new JsonObject();
                step.addProperty("action", "mine_block");
                JsonObject params = new JsonObject();
                params.addProperty("x", pos.getX());
                params.addProperty("y", pos.getY());
                params.addProperty("z", pos.getZ());
                step.add("action_params", params);
                step.addProperty("observation", "已挖掘");
                trajectory.add(step);
            }
            LOGGER.debug("[录制] mine_block ({},{},{})", pos.getX(), pos.getY(), pos.getZ());
        });
    }

    private static void tick(MinecraftServer server) {
        if (!recording || recordingPlayerName == null) return;
        tickCounter++;
        if (tickCounter < 20) return;
        tickCounter = 0;

        ServerPlayerEntity player = server.getPlayerManager().getPlayer(recordingPlayerName);
        if (player == null) return;

        double x = player.getX();
        double y = player.getY();
        double z = player.getZ();
        if (Double.isNaN(lastX)) {
            lastX = x;
            lastY = y;
            lastZ = z;
            return;
        }
        double dx = x - lastX, dy = y - lastY, dz = z - lastZ;
        if (dx * dx + dy * dy + dz * dz < MOVE_RECORD_THRESHOLD * MOVE_RECORD_THRESHOLD) return;

        synchronized (trajectory) {
            JsonObject step = new JsonObject();
            step.addProperty("action", "move_to");
            JsonObject params = new JsonObject();
            params.addProperty("x", Math.round(x * 10) / 10.0);
            params.addProperty("y", Math.round(y * 10) / 10.0);
            params.addProperty("z", Math.round(z * 10) / 10.0);
            step.add("action_params", params);
            step.addProperty("observation", "已到达");
            trajectory.add(step);
        }
        lastX = x;
        lastY = y;
        lastZ = z;
    }

    public static void start(AgentWebSocketClient wsClient, String playerName, String name) {
        synchronized (trajectory) {
            recording = true;
            recordingPlayerName = playerName;
            skillName = name != null && !name.isBlank() ? name : "demo_" + System.currentTimeMillis();
            wsClientRef = wsClient;
            trajectory.clear();
            tickCounter = 0;
        }
        lastX = lastY = lastZ = Double.NaN;
        LOGGER.info("[录制] 开始 recording 玩家={} 技能名={}", playerName, skillName);
    }

    public static void stop() {
        if (!recording) return;
        AgentWebSocketClient client;
        String name;
        JsonArray arr;
        synchronized (trajectory) {
            recording = false;
            client = wsClientRef;
            name = skillName;
            arr = new JsonArray();
            for (JsonObject o : trajectory) arr.add(o);
            trajectory.clear();
            recordingPlayerName = null;
            skillName = null;
            wsClientRef = null;
        }
        if (client != null && client.isConnected()) {
            JsonObject msg = new JsonObject();
            msg.addProperty("type", "demonstration_trajectory");
            msg.addProperty("name", name);
            msg.add("trajectory", arr);
            client.sendToAgent(msg);
            LOGGER.info("[录制] 已结束并发送 trajectory 共 {} 步 -> Python", arr.size());
        }
    }
}
