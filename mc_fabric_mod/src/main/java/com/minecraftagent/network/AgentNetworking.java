package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import net.minecraft.server.network.ServerPlayerEntity;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

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
        PayloadTypeRegistry.playC2S().register(AgentActionCompletePayload.ID, AgentActionCompletePayload.CODEC);
    }

    /** 注册服务端接收器（在服务器启动时调用） */
    public static void registerServerReceiver() {
        ServerPlayNetworking.registerGlobalReceiver(AgentActionCompletePayload.ID, (payload, context) -> {
            context.server().execute(() -> {
                PendingCompletion p = PENDING.remove(payload.requestId());
                if (p != null) {
                    var result = new com.minecraftagent.action.ActionExecutor.ActionResult(
                            payload.observation(),
                            p.gameStateSupplier().get()
                    );
                    p.onComplete().accept(result);
                    LOGGER.debug("[Net] 客户端完成 req={} obs={}", payload.requestId(), payload.observation());
                } else {
                    LOGGER.warn("[Net] 未知 requestId: {}", payload.requestId());
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

    /** 生成唯一 requestId（当 Python 未提供时） */
    public static String generateRequestId() {
        return "agent-" + UUID.randomUUID().toString().substring(0, 8);
    }
}
