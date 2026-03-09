package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import com.minecraftagent.util.NativePathfinder;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 网络包注册与客户端输入模拟完成回调管理。
 */
public class AgentNetworking {

    private static final Logger LOGGER = LoggerFactory.getLogger("AgentNetworking");

    /** 待完成的客户端动作：requestId -> (onComplete, gameStateBuilder) */
    private static final Map<String, PendingCompletion> PENDING = new ConcurrentHashMap<>();

    public record PendingCompletion(
            java.util.function.Consumer<com.minecraftagent.action.ActionExecutor.ActionResult> onComplete,
            java.util.function.Supplier<com.google.gson.JsonObject> gameStateSupplier
    ) {}

    /** 注册网络包类型（在 Mod 初始化时调用，客户端和服务端均需） */
    public static void registerPayloadTypes() {
        PayloadTypeRegistry.playS2C().register(AgentInputPayload.ID, AgentInputPayload.CODEC);
        PayloadTypeRegistry.playS2C().register(AgentPathPayload.ID, AgentPathPayload.CODEC);
        PayloadTypeRegistry.playC2S().register(AgentActionCompletePayload.ID, AgentActionCompletePayload.CODEC);
        PayloadTypeRegistry.playC2S().register(PathUpdateRequestPayload.ID, PathUpdateRequestPayload.CODEC);
    }

    /** 注册服务端接收器（在服务器启动时调用） */
    public static void registerServerReceiver() {
        ServerPlayNetworking.registerGlobalReceiver(AgentActionCompletePayload.ID, (payload, context) -> {
            context.server().execute(() -> {
                PendingCompletion p = PENDING.remove(payload.requestId());
                if (p != null) {
                    var result = new com.minecraftagent.action.ActionExecutor.ActionResult(
                            payload.observation(),
                            p.gameStateSupplier().get(),
                            payload.success()
                    );
                    p.onComplete().accept(result);
                    LOGGER.debug("[Net] 客户端完成 req={} obs={}", payload.requestId(), payload.observation());
                } else {
                    LOGGER.warn("[Net] 未知 requestId: {}", payload.requestId());
                }
            });
        });

        // move_to 执行中客户端请求根据当前游戏状态重新规划路径
        ServerPlayNetworking.registerGlobalReceiver(PathUpdateRequestPayload.ID, (payload, context) -> {
            context.server().execute(() -> {
                ServerPlayerEntity player = context.player();
                if (player == null || !(player.getWorld() instanceof ServerWorld world)) return;
                BlockPos from = new BlockPos(
                        (int) Math.floor(payload.currentX()),
                        (int) Math.floor(payload.currentY()),
                        (int) Math.floor(payload.currentZ())
                );
                BlockPos to = new BlockPos(
                        (int) Math.round(payload.finalTargetX()),
                        (int) Math.round(payload.finalTargetY()),
                        (int) Math.round(payload.finalTargetZ())
                );
                List<Vec3d> path = NativePathfinder.findPath(world, from, to);
                if (path != null && path.size() >= 2 && ServerPlayNetworking.canSend(player, AgentPathPayload.ID)) {
                    ServerPlayNetworking.send(player, new AgentPathPayload(
                            payload.requestId(), "move_to", path));
                    LOGGER.debug("[Net] 路径更新 req={} waypoints={}", payload.requestId(), path.size());
                }
            });
        });
    }

    /** 一次性注册（payload 类型 + 服务端接收器） */
    public static void register() {
        registerPayloadTypes();
        registerServerReceiver();
    }

    /**
     * 发送输入模拟请求到客户端，并注册完成回调。
     * @return true 表示已发送到客户端；false 表示客户端不支持，调用方应回退到服务端直接操控
     */
    public static boolean sendInputToClient(
            ServerPlayerEntity player,
            String requestId,
            String actionType,
            double x, double y, double z,
            PendingCompletion completion) {

        if (!ServerPlayNetworking.canSend(player, AgentInputPayload.ID)) {
            LOGGER.warn("[Net] 客户端不支持 agent_input，将回退到服务端直接操控");
            return false;
        }

        PENDING.put(requestId, completion);
        ServerPlayNetworking.send(player, new AgentInputPayload(requestId, actionType, x, y, z));
        LOGGER.debug("[Net] 已发送 agent_input req={} type={} target=({},{},{})",
                requestId, actionType, (int) x, (int) y, (int) z);
        return true;
    }

    /**
     * 发送寻路路径到客户端（按路径点依次移动，避障、绕路、上台阶）。
     * @return true 表示已发送
     */
    public static boolean sendPathToClient(
            ServerPlayerEntity player,
            String requestId,
            String actionType,
            List<Vec3d> waypoints,
            PendingCompletion completion) {

        if (!ServerPlayNetworking.canSend(player, AgentPathPayload.ID)) {
            LOGGER.warn("[Net] 客户端不支持 agent_path，将回退到单点移动");
            return false;
        }
        if (waypoints == null || waypoints.isEmpty()) return false;

        PENDING.put(requestId, completion);
        ServerPlayNetworking.send(player, new AgentPathPayload(requestId, actionType, waypoints));
        LOGGER.debug("[Net] 已发送 agent_path req={} type={} waypoints={}",
                requestId, actionType, waypoints.size());
        return true;
    }

    /** 生成唯一 requestId（当 Python 未提供时） */
    public static String generateRequestId() {
        return "agent-" + UUID.randomUUID().toString().substring(0, 8);
    }
}
