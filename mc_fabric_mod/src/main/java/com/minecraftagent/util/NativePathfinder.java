package com.minecraftagent.util;

import net.minecraft.entity.EntityType;
import net.minecraft.entity.ai.pathing.EntityNavigation;
import net.minecraft.entity.ai.pathing.Path;
import net.minecraft.entity.mob.ZombieEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 使用原版生物寻路 API（如僵尸索敌导航）计算路径，实现避障、绕路、上台阶。
 * 内部使用 LandPathNodeMaker + A*，会结合世界方块碰撞与可行走节点（落脚点、高度差）。
 * 通过临时假生物获取 Navigation 的 Path，再转为路径点供玩家跟随。
 * 运行中客户端可经 PathUpdateRequestPayload 请求服务端按「当前坐标→最终目标」重新寻路，
 * 以结合当前游戏状态更新路径，避免单次规划在复杂地形（如高山、悬崖）下失效。
 */
public final class NativePathfinder {

    private static final Logger LOGGER = LoggerFactory.getLogger("NativePathfinder");

    /** 寻路最大探索距离（节点数），与旧 A* 的 MAX_PATH_LENGTH 对齐 */
    private static final int MAX_PATH_DISTANCE = 200;

    private NativePathfinder() {}

    /**
     * 从起点到终点计算一条可行走路径（原版 LandPathNodeMaker + A*）。
     *
     * @param world 世界
     * @param from  起点（玩家脚下方块或附近）
     * @param to    终点
     * @return 路径点列表（含起点与终点），从 from 到 to 依次；若无法到达则返回 null
     */
    public static List<Vec3d> findPath(ServerWorld world, BlockPos from, BlockPos to) {
        if (world == null || from == null || to == null) return null;
        if (from.equals(to)) {
            return Collections.singletonList(Vec3d.ofCenter(to));
        }

        ZombieEntity mob = new ZombieEntity(EntityType.ZOMBIE, world);
        try {
            mob.setPosition(from.getX() + 0.5, from.getY(), from.getZ() + 0.5);
            EntityNavigation navigation = mob.getNavigation();
            Path path = navigation.findPathTo(
                    to.getX() + 0.5, to.getY(), to.getZ() + 0.5,
                    MAX_PATH_DISTANCE
            );
            if (path == null || !path.reachesTarget()) {
                LOGGER.debug("[NativePathfinder] 未找到路径 from={} to={}", from, to);
                return null;
            }
            int len = path.getLength();
            if (len <= 0) return null;
            List<Vec3d> waypoints = new ArrayList<>(len);
            for (int i = 0; i < len; i++) {
                waypoints.add(Vec3d.ofCenter(path.getNodePos(i)));
            }
            LOGGER.debug("[NativePathfinder] 找到路径，长度 {} 格", waypoints.size());
            return waypoints;
        } finally {
            mob.discard();
        }
    }
}
