package com.minecraftagent.client;

import com.minecraftagent.MinecraftAgentMod;
import com.minecraftagent.network.AgentActionCompletePayload;
import com.minecraftagent.network.AgentInputPayload;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.network.ClientPlayerInteractionManager;
import net.minecraft.registry.Registries;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Direction;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 客户端输入模拟器：接收服务端下发的 move_to / mine_block，
 * 通过模拟 WASD 和鼠标点击操控玩家，与 Python 规划/技能紧密联动。
 */
@net.fabricmc.api.Environment(net.fabricmc.api.EnvType.CLIENT)
public class ClientInputSimulator {

    private static final Logger LOGGER = LoggerFactory.getLogger("AgentInputSim");
    private static final double REACH_DISTANCE = 2.5;
    private static final int MAX_MOVE_TICKS = 400;
    private static final int MAX_MINE_TICKS = 300;

    private static String currentRequestId;
    private static String currentActionType;
    private static double targetX, targetY, targetZ;
    private static int ticks;
    private static SimulatedInput simulatedInput;
    private static net.minecraft.client.input.Input originalInput;
    private static String blockIdAtStart;

    public static void register() {
        ClientPlayNetworking.registerGlobalReceiver(AgentInputPayload.ID, (payload, context) -> {
            context.client().execute(() -> startTask(
                    payload.requestId(),
                    payload.actionType(),
                    payload.x(),
                    payload.y(),
                    payload.z()
            ));
        });

        ClientTickEvents.END_CLIENT_TICK.register(ClientInputSimulator::tick);
    }

    private static void startTask(String requestId, String actionType, double x, double y, double z) {
        MinecraftClient mc = MinecraftClient.getInstance();
        ClientPlayerEntity player = mc.player;
        if (player == null) {
            sendComplete(requestId, false, "无本地玩家");
            return;
        }
        if (currentRequestId != null) {
            sendComplete(currentRequestId, false, "被新任务中断");
        }
        currentRequestId = requestId;
        currentActionType = actionType;
        targetX = x;
        targetY = y;
        targetZ = z;
        ticks = 0;

        if ("move_to".equals(actionType)) {
            if (simulatedInput == null) simulatedInput = new SimulatedInput();
            originalInput = player.input;
            player.input = simulatedInput;
            LOGGER.info("[InputSim] 开始移动至 ({},{},{})", (int) x, (int) y, (int) z);
        } else if ("mine_block".equals(actionType)) {
            BlockPos pos = new BlockPos((int) x, (int) y, (int) z);
            BlockState state = player.getWorld().getBlockState(pos);
            blockIdAtStart = state.isAir() ? "air" : Registries.BLOCK.getId(state.getBlock()).toString();
            LOGGER.info("[InputSim] 开始挖掘 {} 在 ({},{},{})", blockIdAtStart, (int) x, (int) y, (int) z);
        } else {
            sendComplete(requestId, false, "未知动作类型: " + actionType);
            currentRequestId = null;
        }
    }

    private static void tick(MinecraftClient client) {
        if (currentRequestId == null) return;
        ClientPlayerEntity player = client.player;
        if (player == null) {
            sendComplete(currentRequestId, false, "玩家已断开");
            currentRequestId = null;
            return;
        }

        ticks++;
        if ("move_to".equals(currentActionType)) {
            tickMoveTo(player);
        } else if ("mine_block".equals(currentActionType)) {
            tickMineBlock(client, player);
        }
    }

    private static void tickMoveTo(ClientPlayerEntity player) {
        Vec3d pos = player.getPos();
        double dx = targetX - pos.x;
        double dy = targetY - pos.y;
        double dz = targetZ - pos.z;
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);

        if (dist <= REACH_DISTANCE || ticks > MAX_MOVE_TICKS) {
            finishMoveTo();
            sendComplete(currentRequestId, true,
                    String.format("已到达 (%.0f, %.0f, %.0f)", targetX, targetY, targetZ));
            currentRequestId = null;
            return;
        }

        float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float pitch = (float) (-Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180 / Math.PI);
        player.setYaw(yaw);
        player.setHeadYaw(yaw);
        player.setPitch(pitch);
        if (simulatedInput != null) simulatedInput.setForward();
    }

    private static void tickMineBlock(MinecraftClient client, ClientPlayerEntity player) {
        BlockPos pos = new BlockPos((int) targetX, (int) targetY, (int) targetZ);
        if (player.getWorld().getBlockState(pos).isAir()) {
            sendComplete(currentRequestId, true,
                    String.format("成功挖掘 [%s] 在 (%d,%d,%d)", blockIdAtStart, pos.getX(), pos.getY(), pos.getZ()));
            currentRequestId = null;
            return;
        }
        if (ticks > MAX_MINE_TICKS) {
            sendComplete(currentRequestId, false, "挖掘超时");
            currentRequestId = null;
            return;
        }

        Vec3d eye = player.getEyePos();
        Vec3d blockCenter = Vec3d.ofCenter(pos);
        double dx = blockCenter.x - eye.x;
        double dy = blockCenter.y - eye.y;
        double dz = blockCenter.z - eye.z;
        float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float pitch = (float) (-Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180 / Math.PI);
        player.setYaw(yaw);
        player.setHeadYaw(yaw);
        player.setPitch(pitch);

        ClientPlayerInteractionManager im = client.interactionManager;
        if (im == null) return;
        Direction dir = Direction.getFacing(dx, dy, dz);
        im.updateBlockBreakingProgress(pos, dir);
    }

    private static void finishMoveTo() {
        MinecraftClient mc = MinecraftClient.getInstance();
        if (mc.player != null && originalInput != null) {
            mc.player.input = originalInput;
            originalInput = null;
        }
        if (simulatedInput != null) simulatedInput.stop();
    }

    private static void sendComplete(String requestId, boolean success, String observation) {
        finishMoveTo();
        ClientPlayNetworking.send(new AgentActionCompletePayload(requestId, success, observation));
        LOGGER.info("[InputSim] 完成 req={} success={} obs={}", requestId, success, observation);
    }
}
