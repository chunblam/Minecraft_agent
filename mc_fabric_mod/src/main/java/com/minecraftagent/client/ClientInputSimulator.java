package com.minecraftagent.client;

import com.minecraftagent.network.AgentActionCompletePayload;
import com.minecraftagent.network.AgentInputPayload;
import com.minecraftagent.network.AgentPathPayload;
import com.minecraftagent.network.PathUpdateRequestPayload;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.network.ClientPlayerInteractionManager;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.registry.Registries;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Direction;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 客户端输入模拟器：接收服务端下发的 move_to / mine_block，
 * 通过模拟 WASD（含自动跳跃）和鼠标点击操控玩家，与 Python 规划/技能紧密联动。
 *
 * 改进：
 *  - move_to: 每 tick 最多旋转 YAW_LERP_STEP° 平滑转向，避免瞬间跳视角
 *  - move_to: 检测前方障碍物（脚部方块为实体方块时），自动触发跳跃
 */
@net.fabricmc.api.Environment(net.fabricmc.api.EnvType.CLIENT)
public class ClientInputSimulator {

    private static final Logger LOGGER = LoggerFactory.getLogger("AgentInputSim");
    private static final double REACH_DISTANCE = 2.5;
    private static final int MAX_MOVE_TICKS = 400;
    private static final int MAX_MINE_TICKS = 300;

    /** 每 tick 视角最大偏转角度（度），控制转向平滑度 */
    private static final float YAW_LERP_STEP = 12.0f;
    private static final float PITCH_LERP_STEP = 8.0f;

    private static String currentRequestId;
    private static String currentActionType;
    private static double targetX, targetY, targetZ;
    private static int ticks;
    private static SimulatedInput simulatedInput;
    private static net.minecraft.client.input.Input originalInput;
    private static String blockIdAtStart;

    /** 卡住检测：距离未改善的连续 tick 数，0.6s 内触发（20 TPS 下 12 tick≈0.6s） */
    private static final int STUCK_TICKS_THRESHOLD = 12;
    private static double lastDist = Double.MAX_VALUE;
    private static int stuckTicks = 0;
    /** 自动挖掘子阶段：卡住时按路径方向多格挖掘，脚→头、近→远 */
    private static java.util.List<BlockPos> autoMineQueue = null;
    private static int autoMineTicks = 0;
    /** 卡住后先尝试左右移动+跳跃绕过，该阶段持续 tick 数 */
    private static final int STUCK_RECOVERY_TICKS = 20;
    private static int stuckRecoveryTicks = 0;

    /** 路径方向障碍检测：前方格数、高度层数（脚/膝/头） */
    private static final int OBSTRUCTION_CHECK_AHEAD = 2;
    private static final int OBSTRUCTION_HEIGHTS = 3;
    /** 沿路径朝前时 pitch 固定略向下（度），不锁目标点 */
    private static final float PITCH_RUN_FORWARD = -8.0f;

    /** 寻路模式：按路径点依次移动，到达当前点后切到下一路径点 */
    private static java.util.List<Vec3d> waypoints = null;
    private static int currentWaypointIndex = 0;
    /** 最终目标（路径末点），用于运行中请求服务端重新规划路径 */
    private static double finalTargetX, finalTargetY, finalTargetZ;
    /** 每 N tick 根据当前游戏状态请求服务端更新路径（间隔越短越实时，20 TPS 下 20 tick≈1s） */
    private static final int PATH_UPDATE_INTERVAL = 20;
    private static int ticksSinceLastPathUpdate = 0;

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

        ClientPlayNetworking.registerGlobalReceiver(AgentPathPayload.ID, (payload, context) -> {
            context.client().execute(() -> {
                if (currentRequestId != null && currentRequestId.equals(payload.requestId()) && waypoints != null) {
                    updatePath(payload.waypoints());
                } else {
                    startPathTask(payload.requestId(), payload.actionType(), payload.waypoints());
                }
            });
        });

        ClientTickEvents.END_CLIENT_TICK.register(ClientInputSimulator::tick);
    }

    private static void startPathTask(String requestId, String actionType, java.util.List<Vec3d> path) {
        if (path == null || path.isEmpty()) {
            sendComplete(requestId, false, "路径为空");
            return;
        }
        waypoints = new java.util.ArrayList<>(path);
        currentWaypointIndex = 0;
        Vec3d last = waypoints.get(waypoints.size() - 1);
        finalTargetX = last.x;
        finalTargetY = last.y;
        finalTargetZ = last.z;
        Vec3d first = waypoints.get(0);
        startTask(requestId, "move_to", first.getX(), first.getY(), first.getZ());
        ticksSinceLastPathUpdate = 0;
        LOGGER.info("[InputSim] 开始寻路，共 {} 个路径点", waypoints.size());
    }

    /** 运行中收到服务端下发的路径更新，替换当前路径并从头跟随 */
    private static void updatePath(java.util.List<Vec3d> path) {
        if (path == null || path.isEmpty()) return;
        waypoints = new java.util.ArrayList<>(path);
        currentWaypointIndex = 0;
        Vec3d first = waypoints.get(0);
        targetX = first.x;
        targetY = first.y;
        targetZ = first.z;
        lastDist = Double.MAX_VALUE;
        stuckTicks = 0;
        ticksSinceLastPathUpdate = 0;
        LOGGER.info("[InputSim] 路径已更新，共 {} 个路径点", waypoints.size());
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
        waypoints = null;
        stuckRecoveryTicks = 0;
        currentRequestId = requestId;
        currentActionType = actionType;
        targetX = x;
        targetY = y;
        targetZ = z;
        ticks = 0;

        if ("move_to".equals(actionType)) {
            finalTargetX = x;
            finalTargetY = y;
            finalTargetZ = z;
            if (simulatedInput == null) simulatedInput = new SimulatedInput();
            originalInput = player.input;
            player.input = simulatedInput;
            lastDist = Double.MAX_VALUE;
            stuckTicks = 0;
            autoMineQueue = null;
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

        if (dist <= REACH_DISTANCE) {
            if (waypoints != null && currentWaypointIndex < waypoints.size() - 1) {
                currentWaypointIndex++;
                Vec3d next = waypoints.get(currentWaypointIndex);
                targetX = next.getX();
                targetY = next.getY();
                targetZ = next.getZ();
                lastDist = Double.MAX_VALUE;
                stuckTicks = 0;
                return;
            }
            finishMoveTo();
            String obs = waypoints != null
                    ? String.format("已沿路径到达 (%.0f, %.0f, %.0f)，共 %d 个路径点", targetX, targetY, targetZ, waypoints.size())
                    : String.format("已到达 (%.0f, %.0f, %.0f)", targetX, targetY, targetZ);
            sendComplete(currentRequestId, true, obs);
            waypoints = null;
            currentRequestId = null;
            return;
        }

        // 超时：报告失败，便于 Python 端触发重规划（挖方块/绕路）
        if (ticks > MAX_MOVE_TICKS) {
            finishMoveTo();
            java.util.List<BlockPos> obstructions = getObstructionBlocksInPathDirection(player);
            String obs = buildMoveTimeoutObservation(dist, obstructions);
            waypoints = null;
            sendComplete(currentRequestId, false, obs);
            currentRequestId = null;
            return;
        }

        // ── 卡住检测 + 自动挖掘：距离未改善时，自动挖掉前方障碍，无需 LLM 重规划 ──
        if (dist < lastDist - 0.1) {
            stuckTicks = 0;
        } else {
            stuckTicks++;
        }
        lastDist = dist;

        // 运行中按当前游戏状态定期请求服务端重新规划路径（结合地形、落脚点、高度差）
        if (waypoints != null && autoMineQueue == null) {
            ticksSinceLastPathUpdate++;
            if (ticksSinceLastPathUpdate >= PATH_UPDATE_INTERVAL && ClientPlayNetworking.canSend(PathUpdateRequestPayload.ID)) {
                ticksSinceLastPathUpdate = 0;
                ClientPlayNetworking.send(new PathUpdateRequestPayload(
                        currentRequestId, pos.x, pos.y, pos.z, finalTargetX, finalTargetY, finalTargetZ));
            }
        }

        if (autoMineQueue != null && !autoMineQueue.isEmpty()) {
            tickAutoMineDuringMove(MinecraftClient.getInstance(), player);
            return;
        }

        // 卡住后仅往当前路径点方向移动+偶尔跳跃（不自动挖，避免与计划中的 mine_block 混在一起）
        if (stuckRecoveryTicks > 0) {
            stuckRecoveryTicks++;
            float targetYaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
            float smoothYaw = lerpYaw(player.getYaw(), targetYaw, YAW_LERP_STEP);
            player.setYaw(smoothYaw);
            player.setHeadYaw(smoothYaw);
            player.setPitch(lerpAngle(player.getPitch(), PITCH_RUN_FORWARD, PITCH_LERP_STEP));
            if (simulatedInput != null) {
                simulatedInput.setForward();
                // 每 5 tick 跳一次，避免一直跳
                simulatedInput.setJump(stuckRecoveryTicks % 5 == 0);
            }
            if (stuckRecoveryTicks >= STUCK_RECOVERY_TICKS) {
                stuckRecoveryTicks = 0;
                // move_to 期间不再自动挖障碍，由 Python 超时后重规划插入 mine_block
            }
            return;
        }
        if (stuckTicks >= STUCK_TICKS_THRESHOLD) {
            stuckRecoveryTicks = 1;
            LOGGER.info("[InputSim] 卡住检测：往当前路径点方向移动+跳跃");
        }

        // 沿路径朝前：yaw 朝向当前目标（移动方向），pitch 固定略向下，不锁死目标点
        float targetYaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float currentYaw = player.getYaw();
        float smoothYaw = lerpYaw(currentYaw, targetYaw, YAW_LERP_STEP);
        player.setYaw(smoothYaw);
        player.setHeadYaw(smoothYaw);
        player.setPitch(lerpAngle(player.getPitch(), PITCH_RUN_FORWARD, PITCH_LERP_STEP));

        if (simulatedInput != null) {
            simulatedInput.setForward();
            // 仅当路径方向脚前有 1 格高障碍且可跳过时自动跳；接近目标时不跳（前方往往是目标方块，会导致一直跳）
            boolean nearTarget = dist < 3.5;
            boolean blocked = !nearTarget && isBlockedAhead(player, smoothYaw);
            simulatedInput.setJump(blocked);
        }
    }

    /**
     * move_to 过程中的自动挖掘子阶段：按队列顺序挖掉路径方向上的障碍（脚→头、近→远）。
     */
    private static void tickAutoMineDuringMove(MinecraftClient client, ClientPlayerEntity player) {
        if (autoMineQueue == null || autoMineQueue.isEmpty() || !(player.getWorld() instanceof ClientWorld world)) return;

        BlockPos current = autoMineQueue.get(0);
        if (world.getBlockState(current).isAir()) {
            autoMineQueue.remove(0);
            if (autoMineQueue.isEmpty()) {
                autoMineQueue = null;
                LOGGER.info("[InputSim] 障碍已清除，恢复移动");
            }
            return;
        }
        autoMineTicks++;
        if (autoMineTicks > MAX_MINE_TICKS) {
            autoMineQueue.remove(0);
            if (autoMineQueue.isEmpty()) autoMineQueue = null;
            autoMineTicks = 0;
            return;
        }

        Vec3d eye = player.getEyePos();
        Vec3d blockCenter = Vec3d.ofCenter(current);
        double dx = blockCenter.x - eye.x;
        double dy = blockCenter.y - eye.y;
        double dz = blockCenter.z - eye.z;
        float targetYaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float targetPitch = (float) (-Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180 / Math.PI);
        float smoothYaw = lerpYaw(player.getYaw(), targetYaw, YAW_LERP_STEP);
        float smoothPitch = lerpAngle(player.getPitch(), targetPitch, PITCH_LERP_STEP);
        player.setYaw(smoothYaw);
        player.setHeadYaw(smoothYaw);
        player.setPitch(smoothPitch);

        ClientPlayerInteractionManager im = client.interactionManager;
        if (im != null) {
            Direction dir = Direction.getFacing(dx, dy, dz);
            im.updateBlockBreakingProgress(current, dir);
        }
    }

    /** 当前移动方向（朝当前目标/路径点）的水平单位向量，用于障碍检测。 */
    private static Vec3d directionTowardCurrentTarget(ClientPlayerEntity player) {
        double dx = targetX - player.getX();
        double dz = targetZ - player.getZ();
        double len = Math.sqrt(dx * dx + dz * dz);
        if (len < 1e-6) return new Vec3d(0, 0, -1);
        return new Vec3d(dx / len, 0, dz / len);
    }

    /**
     * 沿路径方向检测前方 1~2 格 × 脚/膝/头 的固体方块，按脚→头、近→远排序。
     * 排除：① 当前移动目标所在格（避免把要去的树当成“障碍”挖掉）；② 原木/树干（砍树由计划中的 mine_block 执行，不在 move_to 里自动挖）。
     */
    private static java.util.List<BlockPos> getObstructionBlocksInPathDirection(ClientPlayerEntity player) {
        java.util.List<BlockPos> out = new java.util.ArrayList<>();
        if (!(player.getWorld() instanceof ClientWorld world)) return out;
        int targetBlockX = (int) Math.floor(targetX);
        int targetBlockY = (int) Math.floor(targetY);
        int targetBlockZ = (int) Math.floor(targetZ);
        Vec3d dir = directionTowardCurrentTarget(player);
        double yawRad = Math.atan2(-dir.x, dir.z);
        double fdx = -Math.sin(yawRad);
        double fdz = Math.cos(yawRad);
        Vec3d pos = player.getPos();
        int baseY = (int) Math.floor(pos.y);
        for (int a = 1; a <= OBSTRUCTION_CHECK_AHEAD; a++) {
            int checkX = (int) Math.floor(pos.x + fdx * (a * 1.2));
            int checkZ = (int) Math.floor(pos.z + fdz * (a * 1.2));
            for (int h = 0; h < OBSTRUCTION_HEIGHTS; h++) {
                int py = baseY + h;
                BlockPos bp = new BlockPos(checkX, py, checkZ);
                if (bp.getX() == targetBlockX && bp.getY() == targetBlockY && bp.getZ() == targetBlockZ) {
                    continue;
                }
                BlockState state = world.getBlockState(bp);
                if (!state.isSolidBlock(world, bp)) continue;
                String blockId = Registries.BLOCK.getId(state.getBlock()).toString();
                if (blockId.endsWith("_log") || blockId.endsWith("_stem")) {
                    continue;
                }
                out.add(bp);
            }
        }
        return out;
    }

    /**
     * 平滑 yaw 插值（处理 ±180° 边界回绕）。
     *
     * @param current  当前 yaw（度）
     * @param target   目标 yaw（度）
     * @param maxStep  每 tick 最大变化量（度）
     * @return 本 tick 应设置的 yaw
     */
    private static float lerpYaw(float current, float target, float maxStep) {
        float diff = target - current;
        // 规范化到 [-180, 180]
        while (diff > 180)  diff -= 360;
        while (diff < -180) diff += 360;
        if (Math.abs(diff) <= maxStep) return target;
        return current + Math.signum(diff) * maxStep;
    }

    /** pitch 线性插值（-90~90，无需绕圈处理） */
    private static float lerpAngle(float current, float target, float maxStep) {
        float diff = target - current;
        if (Math.abs(diff) <= maxStep) return target;
        return current + Math.signum(diff) * maxStep;
    }

    /**
     * 检测玩家朝向 smoothYaw 方向前方一格是否有实体方块（需要跳跃）。
     * 若前方方块正是当前移动目标所在格，不视为障碍（避免接近目标时一直跳）。
     */
    private static boolean isBlockedAhead(ClientPlayerEntity player, float yawDeg) {
        if (!(player.getWorld() instanceof ClientWorld world)) return false;

        double yawRad = Math.toRadians(yawDeg);
        double fdx = -Math.sin(yawRad);
        double fdz = Math.cos(yawRad);

        Vec3d pos = player.getPos();
        int checkX = (int) Math.floor(pos.x + fdx * 0.7);
        int checkY = (int) Math.floor(pos.y);
        int checkZ = (int) Math.floor(pos.z + fdz * 0.7);

        BlockPos footFront = new BlockPos(checkX, checkY, checkZ);
        // 前方一格若是当前目标所在格，不跳（目标点往往是方块中心，接近时会被判为“障碍”）
        int targetBlockX = (int) Math.floor(targetX);
        int targetBlockY = (int) Math.floor(targetY);
        int targetBlockZ = (int) Math.floor(targetZ);
        if (footFront.getX() == targetBlockX && footFront.getY() == targetBlockY && footFront.getZ() == targetBlockZ) {
            return false;
        }

        BlockState stateAtFoot = world.getBlockState(footFront);
        if (stateAtFoot.isSolidBlock(world, footFront)) {
            BlockPos aboveFront = footFront.up();
            boolean canJumpOver = world.getBlockState(aboveFront).isAir()
                    && world.getBlockState(aboveFront.up()).isAir();
            return canJumpOver;
        }
        return false;
    }

    /**
     * 获取玩家朝向正前方一格的方块坐标（脚部高度）。
     * 用于 move_to 超时时，将障碍方块坐标告知 Python，便于执行 mine_block。
     */
    private static BlockPos getBlockPosAhead(ClientPlayerEntity player) {
        if (!(player.getWorld() instanceof ClientWorld world)) return null;
        float yawDeg = player.getYaw();
        double yawRad = Math.toRadians(yawDeg);
        double fdx = -Math.sin(yawRad);
        double fdz = Math.cos(yawRad);
        Vec3d pos = player.getPos();
        int checkX = (int) Math.floor(pos.x + fdx * 0.7);
        int checkY = (int) Math.floor(pos.y);
        int checkZ = (int) Math.floor(pos.z + fdz * 0.7);
        BlockPos footFront = new BlockPos(checkX, checkY, checkZ);
        if (world.getBlockState(footFront).isSolidBlock(world, footFront)) {
            return footFront;
        }
        // 检查膝盖高度（两格高墙）
        BlockPos kneeFront = footFront.up();
        if (world.getBlockState(kneeFront).isSolidBlock(world, kneeFront)) {
            return kneeFront;
        }
        return null;
    }

    private static String buildMoveTimeoutObservation(double remainDist, java.util.List<BlockPos> obstructions) {
        String base = String.format(
                "移动超时，距目标尚有 %.0f 格，可能被障碍物阻挡。当前 game_state.position 已更新。建议：",
                remainDist);
        if (obstructions != null && !obstructions.isEmpty()) {
            StringBuilder sb = new StringBuilder(base);
            int max = Math.min(obstructions.size(), 4);
            for (int i = 0; i < max; i++) {
                BlockPos bp = obstructions.get(i);
                sb.append(String.format(" mine_block {\"x\":%d,\"y\":%d,\"z\":%d}；", bp.getX(), bp.getY(), bp.getZ()));
            }
            sb.append("再 move_to 继续。");
            return sb.toString();
        }
        return base + "mine_block 打掉前方障碍，或 find_resource + 新坐标绕路，再 move_to。";
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
        float targetYaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float targetPitch = (float) (-Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180 / Math.PI);

        // 平滑视角：与 move_to 一致，避免切换目标时瞬间跳转
        float smoothYaw = lerpYaw(player.getYaw(), targetYaw, YAW_LERP_STEP);
        float smoothPitch = lerpAngle(player.getPitch(), targetPitch, PITCH_LERP_STEP);
        player.setYaw(smoothYaw);
        player.setHeadYaw(smoothYaw);
        player.setPitch(smoothPitch);

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
