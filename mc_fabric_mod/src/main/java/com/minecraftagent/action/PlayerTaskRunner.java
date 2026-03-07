package com.minecraftagent.action;

import net.minecraft.block.BlockState;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 玩家任务运行器（参考 VOYAGER 的 tick 级动作）。
 *
 * 将高层动作（move_to, mine_block）分解为逐 tick 执行的低层操作，
 * 使玩家移动、挖掘等行为具有真实耗时，与普通玩家操控一致。
 *
 * 时间参数（与 Minecraft 原版一致）：
 *   - 移动：4.317 格/秒（原版行走速度）
 *   - 挖掘：使用 player.getBlockBreakingSpeed() + block.getHardness() 计算真实耗时
 *
 * 每 tick 调用 tick()，当前任务完成后完成 Future 并清空。
 */
public class PlayerTaskRunner {

    private static final Logger LOGGER = LoggerFactory.getLogger("PlayerTaskRunner");

    // 原版行走速度 4.317 格/秒，20 tick/秒 → 每 tick 约 0.216 格
    private static final double MOVE_SPEED = 0.216;
    private static final double REACH_DISTANCE = 2.0;     // 到达判定（略小于攻击距离 3 格）
    private static final int MAX_MOVE_TICKS = 400;        // 移动最大 tick 数（约 20 秒）
    private static final int MAX_MINE_TICKS = 300;       // 挖掘最大 tick 数（约 15 秒，应对黑曜石等）

    private final AtomicReference<PlayerTask> currentTask = new AtomicReference<>();

    /**
     * 每服务器 tick 调用，推进当前任务。
     */
    public void tick() {
        PlayerTask task = currentTask.get();
        if (task == null) return;

        if (task.tick()) {
            currentTask.set(null);
            task.complete();
        }
    }

    /**
     * 启动移动任务：玩家逐步走向目标，每 tick 移动一小段。
     */
    public CompletableFuture<String> runMoveTo(ServerPlayerEntity player, double tx, double ty, double tz) {
        CompletableFuture<String> future = new CompletableFuture<>();
        currentTask.set(new MoveToTask(player, tx, ty, tz, future));
        return future;
    }

    /**
     * 启动挖掘任务：玩家面向方块并持续挖掘直到破坏。
     */
    public CompletableFuture<String> runMineBlock(ServerPlayerEntity player, int bx, int by, int bz) {
        CompletableFuture<String> future = new CompletableFuture<>();
        currentTask.set(new MineBlockTask(player, bx, by, bz, future));
        return future;
    }

    public boolean hasActiveTask() {
        return currentTask.get() != null;
    }

    public void cancelCurrent() {
        PlayerTask t = currentTask.getAndSet(null);
        if (t != null) t.completeExceptionally(new RuntimeException("任务被取消"));
    }

    // ── 内部任务类 ────────────────────────────────────────────────────────────

    private abstract static class PlayerTask {
        protected final ServerPlayerEntity player;
        protected int ticks;

        PlayerTask(ServerPlayerEntity player) {
            this.player = player;
        }

        /** 返回 true 表示任务完成 */
        abstract boolean tick();

        abstract void complete();
        abstract void completeExceptionally(Throwable t);
    }

    private static class MoveToTask extends PlayerTask {
        private final double targetX, targetY, targetZ;
        private final CompletableFuture<String> future;

        MoveToTask(ServerPlayerEntity player, double tx, double ty, double tz, CompletableFuture<String> future) {
            super(player);
            this.targetX = tx;
            this.targetY = ty;
            this.targetZ = tz;
            this.future = future;
        }

        @Override
        boolean tick() {
            ticks++;
            if (ticks > MAX_MOVE_TICKS) return true;

            Vec3d pos = player.getPos();
            double dx = targetX - pos.x;
            double dy = targetY - pos.y;
            double dz = targetZ - pos.z;
            double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);

            if (dist <= REACH_DISTANCE) return true;

            double scale = MOVE_SPEED / Math.max(dist, 0.01);
            double vx = dx * scale;
            double vy = dy * scale;
            double vz = dz * scale;

            player.setPosition(pos.x + vx, pos.y + vy, pos.z + vz);

            // 面向移动方向
            float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
            player.setYaw(yaw);
            player.setHeadYaw(yaw);

            return false;
        }

        @Override
        void complete() {
            future.complete(String.format("已到达 (%.0f, %.0f, %.0f)", targetX, targetY, targetZ));
        }

        @Override
        void completeExceptionally(Throwable t) {
            future.completeExceptionally(t);
        }
    }

    private static class MineBlockTask extends PlayerTask {
        private final BlockPos target;
        private final CompletableFuture<String> future;
        private final ServerWorld world;
        private final int requiredTicks;  // 根据方块硬度和玩家工具计算的真实挖掘 tick 数
        private String blockIdAtStart;

        MineBlockTask(ServerPlayerEntity player, int x, int y, int z, CompletableFuture<String> future) {
            super(player);
            this.target = new BlockPos(x, y, z);
            this.future = future;
            this.world = (ServerWorld) player.getWorld();
            BlockState state = world.getBlockState(target);
            this.blockIdAtStart = state.isAir() ? "air" : net.minecraft.registry.Registries.BLOCK.getId(state.getBlock()).toString();
            this.requiredTicks = computeBreakTicks(player, state, world, target);
        }

        /** 使用原版公式计算挖掘所需 tick 数：hardness / getBlockBreakingSpeed，与普通玩家一致 */
        private static int computeBreakTicks(ServerPlayerEntity player, BlockState state,
                                            ServerWorld world, BlockPos pos) {
            if (state.isAir()) return 1;
            float hardness = state.getHardness(world, pos);
            if (hardness < 0) return MAX_MINE_TICKS;  // 不可破坏（如基岩）
            if (hardness == 0) return 1;              // 瞬间破坏（如火把、花）
            float speed = player.getBlockBreakingSpeed(state);
            if (speed <= 0) return MAX_MINE_TICKS;
            int ticks = (int) Math.ceil(20.0f * hardness / Math.max(0.0001f, speed));
            return Math.max(1, Math.min(ticks, MAX_MINE_TICKS));
        }

        @Override
        boolean tick() {
            ticks++;
            if (world.getBlockState(target).isAir()) return true;

            // 面向方块
            Vec3d pos = player.getPos();
            Vec3d blockCenter = Vec3d.ofCenter(target);
            double dx = blockCenter.x - pos.x;
            double dy = blockCenter.y - (pos.y + player.getStandingEyeHeight() * 0.9);
            double dz = blockCenter.z - pos.z;
            float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
            float pitch = (float) (-Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) * 180 / Math.PI);
            player.setYaw(yaw);
            player.setHeadYaw(yaw);
            player.setPitch(pitch);

            // 达到真实挖掘时间后破坏方块（与普通玩家一致）
            if (ticks >= requiredTicks || ticks > MAX_MINE_TICKS) {
                world.breakBlock(target, true, player);
                return true;
            }
            return false;
        }

        @Override
        void complete() {
            future.complete(String.format("成功挖掘 [%s] 在 (%d,%d,%d)", blockIdAtStart, target.getX(), target.getY(), target.getZ()));
        }

        @Override
        void completeExceptionally(Throwable t) {
            future.completeExceptionally(t);
        }
    }
}
