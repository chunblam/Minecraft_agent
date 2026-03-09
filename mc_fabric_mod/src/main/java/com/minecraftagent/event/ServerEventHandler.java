package com.minecraftagent.event;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.minecraftagent.action.PlayerContext;
import com.minecraftagent.network.AgentWebSocketClient;
import com.minecraftagent.util.WorldScanner;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.minecraft.entity.Entity;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Comparator;

/**
 * 服务器事件监听器（v3）。
 *
 * 事件：
 * 1. CHAT_MESSAGE  - 玩家聊天时，构建完整 game_state 发给 Python Agent。
 *
 * game_state 字段说明：
 *   player_name    - 玩家名
 *   health/hunger  - 血量/饥饿
 *   xp_level       - 经验等级（附魔前确认）
 *   dimension      - 当前维度（overworld/nether/end）
 *   time           - 游戏时间（0-24000，12000+ 为夜晚）
 *   position       - 玩家坐标 {x,y,z}
 *   inventory      - 全背包 41 格非空物品
 *   nearby_blocks  - 立即周围 7×7×5 非空气方块（了解站立环境）
 *   nearby_resources  - 24格内矿石/树木/设施/水源的坐标（按类别分组）
 *   nearby_entities   - 20格内生物的类型、名称、坐标
 *   environment    - Y层/深度上下文/光照/生物群系（当前位置）
 *   horizon_scan   - 8方向 × {48,96,192}格 的生物群系+地形感知
 *                    用于 Agent 在远距离感知资源分布（"地平线视野"）
 *   agent_position - 与 position 一致（兼容字段）
 */
public class ServerEventHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(ServerEventHandler.class);

    /** 注册所有事件监听。在 MinecraftAgentMod.onInitialize() 中调用。 */
    public static void register(MinecraftServer server, AgentWebSocketClient wsClient) {

        // ── 玩家聊天事件 ────────────────────────────────────────────────────
        ServerMessageEvents.CHAT_MESSAGE.register((signedMessage, sender, params) -> {

            String senderName = sender.getName().getString();
            if (senderName.equals("晨曦")) return;

            if (!wsClient.isConnected()) {
                LOGGER.warn("Python 服务器未连接，忽略玩家消息");
                return;
            }

            String messageContent = signedMessage.getSignedContent();
            if (messageContent == null || messageContent.isBlank())
                messageContent = signedMessage.getContent().getString();
            if (messageContent.isBlank()) return;

            // 玩家输入「自主探索」时，仅推送 game_state_update 触发自主探索，不当作普通聊天
            if (messageContent.trim().equals("自主探索")) {
                JsonObject payload = new JsonObject();
                payload.addProperty("type", "game_state_update");
                payload.add("game_state", buildGameState(sender, server));
                wsClient.sendToAgent(payload);
                LOGGER.info("玩家 {} 触发自主探索", senderName);
                return;
            }

            LOGGER.info("玩家 {} 发送消息: {}", senderName, messageContent);
            PlayerContext.setCurrentPlayer(sender);

            JsonObject payload = new JsonObject();
            payload.addProperty("type", "player_chat");
            payload.addProperty("player_message", messageContent);
            payload.add("game_state", buildGameState(sender, server));

            wsClient.sendToAgent(payload);
        });

    }

    // ── 游戏状态采集 ─────────────────────────────────────────────────────────

    /**
     * 采集完整游戏状态并序列化为 JSON 发给 Python。
     *
     * 新增 horizon_scan 字段，提供 8 方向 × 3 距离的远程生物群系感知，
     * 让 LLM 在任务规划时能感知到 192 格内的地形类型和资源分布方向。
     */
    public static JsonObject buildGameState(ServerPlayerEntity player, MinecraftServer server) {
        JsonObject state = new JsonObject();

        // ── 基础信息 ──────────────────────────────────────────────────────
        state.addProperty("player_name", player.getName().getString());
        state.addProperty("health",    Math.round(player.getHealth()));
        state.addProperty("hunger",    player.getHungerManager().getFoodLevel());
        state.addProperty("xp_level",  player.experienceLevel);
        state.addProperty("dimension", player.getWorld().getRegistryKey().getValue().toString());
        state.addProperty("time",      server.getOverworld().getTimeOfDay() % 24000);

        // ── 玩家坐标 ─────────────────────────────────────────────────────
        Vec3d pos = player.getPos();
        JsonObject playerPos = new JsonObject();
        playerPos.addProperty("x", Math.round(pos.x));
        playerPos.addProperty("y", Math.round(pos.y));
        playerPos.addProperty("z", Math.round(pos.z));
        state.add("position", playerPos);

        // ── 完整背包 ─────────────────────────────────────────────────────
        JsonArray inventory = new JsonArray();
        for (int i = 0; i < player.getInventory().size(); i++) {
            var stack = player.getInventory().getStack(i);
            if (!stack.isEmpty()) {
                JsonObject item = new JsonObject();
                item.addProperty("slot",     i);
                item.addProperty("item",     Registries.ITEM.getId(stack.getItem()).toString());
                item.addProperty("count",    stack.getCount());
                item.addProperty("is_hotbar", i < 9);
                inventory.add(item);
            }
        }
        state.add("inventory", inventory);

        ServerWorld world = (ServerWorld) player.getWorld();
        BlockPos playerBlock = player.getBlockPos();

        // ── 立即周围方块（7×7×5）────────────────────────────────────────
        state.add("nearby_blocks", scanImmediateBlocks(world, playerBlock));

        // ── 近程资源坐标（24格，按类别分组）─────────────────────────────
        state.add("nearby_resources", scanNearbyResources(world, playerBlock));

        // ── 周围实体（20格）──────────────────────────────────────────────
        state.add("nearby_entities", scanNearbyEntities(world, player));

        // ── 当前环境信息 ─────────────────────────────────────────────────
        state.add("environment", buildEnvironmentInfo(world, playerBlock));

        // ── 地平线扫描（8方向 × {48,96,192}格，核心新功能）─────────────
        // 利用 WorldScanner 向 8 个方向各采样 3 个距离的生物群系，
        // 使 LLM 能在任务规划时感知到 192 格内的地形类型：
        //   "东北 48 格是森林" → Agent 知道往东北走去砍木
        //   "正北 96 格是沙漠" → Agent 知道北边有沙子
        //   "正东 192 格是石峰" → Agent 知道东边有裸露矿石
        state.add("horizon_scan", WorldScanner.scanHorizon(world, playerBlock));

        // ── agent_position（与 position 一致）────────────────────────────
        JsonObject agentPos = new JsonObject();
        agentPos.addProperty("x", playerPos.get("x").getAsInt());
        agentPos.addProperty("y", playerPos.get("y").getAsInt());
        agentPos.addProperty("z", playerPos.get("z").getAsInt());
        state.add("agent_position", agentPos);

        return state;
    }

    // ── 内部扫描方法 ─────────────────────────────────────────────────────────

    /**
     * 扫描立即周围 7×7×5（-3~+3 x/z，-1~+3 y）的非空气方块。
     * 最多 60 个，用于 Agent 了解当前站立的微环境（地面/洞穴/室内）。
     */
    private static JsonArray scanImmediateBlocks(ServerWorld world, BlockPos center) {
        JsonArray blocks = new JsonArray();
        outer:
        for (int dx = -3; dx <= 3; dx++) {
            for (int dz = -3; dz <= 3; dz++) {
                for (int dy = -1; dy <= 3; dy++) {
                    BlockPos p = center.add(dx, dy, dz);
                    var bState = world.getBlockState(p);
                    if (!bState.isAir()) {
                        JsonObject b = new JsonObject();
                        b.addProperty("id", Registries.BLOCK.getId(bState.getBlock()).toString());
                        b.addProperty("x", p.getX());
                        b.addProperty("y", p.getY());
                        b.addProperty("z", p.getZ());
                        blocks.add(b);
                        if (blocks.size() >= 60) break outer;
                    }
                }
            }
        }
        return blocks;
    }

    /**
     * 扫描半径 24 格内的各类资源坐标，按类别分组。
     * 每类别最多 5 个最近坐标，供 Agent 直接 move_to 使用。
     *
     * 类别：ores（矿石）、logs（原木）、water（水源）、lava（熔岩）、
     *       gravel（沙砾）、sand（沙子）、crafting（制作设施）、farmable（农业）
     */
    private static JsonObject scanNearbyResources(ServerWorld world, BlockPos center) {
        java.util.Map<String, java.util.List<BlockPos>> found = new java.util.HashMap<>();
        for (String cat : new String[]{"ores","logs","water","lava","gravel","sand","crafting","farmable"})
            found.put(cat, new java.util.ArrayList<>());

        final int RADIUS       = 24;
        final int MAX_PER_CAT  = 5;
        BlockPos.Mutable mutable = new BlockPos.Mutable();

        for (int r = 1; r <= RADIUS; r++) {
            if (found.values().stream().allMatch(l -> l.size() >= MAX_PER_CAT)) break;

            for (int dx = -r; dx <= r; dx++) {
                for (int dz = -r; dz <= r; dz++) {
                    if (Math.abs(dx) < r && Math.abs(dz) < r) continue;
                    for (int dy = -r; dy <= r; dy++) {
                        mutable.set(center.getX()+dx, center.getY()+dy, center.getZ()+dz);
                        if (!world.isChunkLoaded(mutable)) continue;

                        var bState = world.getBlockState(mutable);
                        if (bState.isAir()) continue;

                        String bid = Registries.BLOCK.getId(bState.getBlock()).toString();
                        String cat = classifyBlock(bid);
                        if (cat == null) continue;

                        java.util.List<BlockPos> list = found.get(cat);
                        if (list != null && list.size() < MAX_PER_CAT)
                            list.add(mutable.toImmutable());
                    }
                }
            }
        }

        JsonObject result = new JsonObject();
        for (var entry : found.entrySet()) {
            if (entry.getValue().isEmpty()) continue;
            JsonArray arr = new JsonArray();
            entry.getValue().stream()
                    .sorted(java.util.Comparator.comparingInt(p -> (int) center.getSquaredDistance(p)))
                    .forEach(p -> {
                        JsonObject coord = new JsonObject();
                        coord.addProperty("x", p.getX());
                        coord.addProperty("y", p.getY());
                        coord.addProperty("z", p.getZ());
                        coord.addProperty("block",
                                Registries.BLOCK.getId(world.getBlockState(p).getBlock()).toString());
                        arr.add(coord);
                    });
            result.add(entry.getKey(), arr);
        }
        return result;
    }

    /** 将方块 ID 归入资源类别，null 表示不关心 */
    private static String classifyBlock(String blockId) {
        if (blockId.contains("_ore"))                                   return "ores";
        if (blockId.endsWith("_log") || blockId.endsWith("_stem"))      return "logs";
        if (blockId.equals("minecraft:water"))                          return "water";
        if (blockId.equals("minecraft:lava"))                           return "lava";
        if (blockId.equals("minecraft:gravel"))                         return "gravel";
        if (blockId.equals("minecraft:sand") ||
            blockId.equals("minecraft:red_sand"))                       return "sand";
        if (blockId.equals("minecraft:crafting_table") ||
            blockId.equals("minecraft:furnace")        ||
            blockId.equals("minecraft:anvil")          ||
            blockId.equals("minecraft:enchanting_table")||
            blockId.equals("minecraft:chest")          ||
            blockId.equals("minecraft:barrel")         ||
            blockId.equals("minecraft:smoker")         ||
            blockId.equals("minecraft:blast_furnace"))                  return "crafting";
        if (blockId.equals("minecraft:farmland")       ||
            blockId.equals("minecraft:grass_block")    ||
            blockId.contains("wheat")                  ||
            blockId.contains("carrot")                 ||
            blockId.contains("potato")                 ||
            blockId.contains("beetroot"))               return "farmable";
        return null;
    }

    /**
     * 扫描半径 20 格内的所有非 Agent 实体。
     * 最多 20 个，按距离由近到远排列。
     */
    private static JsonArray scanNearbyEntities(ServerWorld world, ServerPlayerEntity player) {
        Box searchBox = player.getBoundingBox().expand(20);
        JsonArray arr = new JsonArray();
        world.getOtherEntities(player, searchBox).stream()
                .filter(e -> !e.getName().getString().equals("晨曦"))
                .sorted(Comparator.comparingDouble(e -> e.squaredDistanceTo(player)))
                .limit(20)
                .forEach(e -> {
                    JsonObject info = new JsonObject();
                    info.addProperty("type",     e.getType().toString());
                    info.addProperty("name",     e.getName().getString());
                    info.addProperty("distance", (int) e.distanceTo(player));
                    info.addProperty("x", Math.round(e.getX()));
                    info.addProperty("y", Math.round(e.getY()));
                    info.addProperty("z", Math.round(e.getZ()));
                    arr.add(info);
                });
        return arr;
    }

    /**
     * 当前位置的环境信息：Y层、深度上下文、光照、生物群系。
     *
     * depth_context 说明：
     *   y >= 60  → 地表
     *   0 ~ 59   → 地下洞穴
     *   y < 0    → 深层/深板岩（钻石矿最佳范围 y = -58）
     */
    private static JsonObject buildEnvironmentInfo(ServerWorld world, BlockPos pos) {
        JsonObject env = new JsonObject();
        env.addProperty("y_level", pos.getY());

        String depthCtx;
        if (pos.getY() >= 60)      depthCtx = "地表（y>=60）";
        else if (pos.getY() >= 0)  depthCtx = "地下洞穴（0<=y<60）";
        else                       depthCtx = "深层/深板岩（y<0，钻石矿集中于y=-58附近）";
        env.addProperty("depth_context", depthCtx);

        env.addProperty("light_level", world.getLightLevel(pos));
        env.addProperty("is_dark", world.getLightLevel(pos) < 8);

        // 当前位置生物群系
        var biome = world.getBiome(pos);
        String biomeId = biome.getKey().map(k -> k.getValue().toString()).orElse("unknown");
        env.addProperty("biome", biomeId);
        env.addProperty("biome_hint", WorldScanner.getBiomeHint(biomeId));

        return env;
    }
}
