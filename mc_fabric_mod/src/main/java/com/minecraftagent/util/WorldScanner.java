package com.minecraftagent.util;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.minecraft.registry.Registries;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.Heightmap;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 世界扫描工具类（WorldScanner）。
 *
 * 提供两个核心能力：
 *
 * 1. scanHorizon()：以玩家为中心，向 8 个方向各采样若干距离的
 *    生物群系（Biome）和地表信息，构建"地平线感知"数据。
 *    - 生物群系查询 = world.getBiome()，即使区块未满载也可用。
 *    - 地表高度/方块 = Heightmap，需要区块已加载（server view distance 内通常已加载）。
 *    - 只扫描已加载区块，不触发区块生成，不影响服务器性能。
 *
 * 2. generateSummary()：将 scanHorizon() 的结果格式化为
 *    人类可读的文字摘要，并生成"导航建议"，供 look_around 行动返回给 LLM。
 *
 * 同时被 ServerEventHandler（自动上报）和 ActionExecutor（主动行动）使用。
 */
public class WorldScanner {

    // ── 方向定义（8方向，dx/dz 单位向量）────────────────────────────────────
    private static final int[][] DIRS = {
        { 0, -1}, {1, -1}, {1,  0}, {1,  1},
        { 0,  1}, {-1, 1}, {-1, 0}, {-1, -1}
    };
    private static final String[] DIR_EN = {
        "north", "northeast", "east", "southeast",
        "south", "southwest", "west", "northwest"
    };
    private static final String[] DIR_ZH = {
        "正北", "东北", "正东", "东南",
        "正南", "西南", "正西", "西北"
    };

    /** 自动上报时使用的默认采样距离（格）*/
    private static final int[] DEFAULT_DISTANCES = {48, 96, 192};

    // ── 公共接口 ─────────────────────────────────────────────────────────────

    /**
     * 地平线扫描（自动上报版）：8方向 × {48, 96, 192} 格，返回 JSON。
     *
     * 供 ServerEventHandler.buildGameState() 调用，结果放入 game_state.horizon_scan。
     */
    public static JsonObject scanHorizon(ServerWorld world, BlockPos center) {
        return scanHorizon(world, center, DEFAULT_DISTANCES);
    }

    /**
     * 地平线扫描（自定义距离版）：8方向 × distances 格，返回 JSON。
     *
     * 每个方向返回一个 JsonArray，每个元素是一个采样点的信息：
     * {
     *   "distance": 48,
     *   "biome": "minecraft:forest",
     *   "hint": "橡木森林（大量橡木，可砍伐）",
     *   "surface_y": 68,
     *   "surface_block": "minecraft:grass_block",
     *   "terrain": "草地（正常平原地形）"
     * }
     * 若区块未加载则只含 {"distance": 48, "status": "未探索区域"}，
     * 并停止该方向更远距离的采样。
     */
    public static JsonObject scanHorizon(ServerWorld world, BlockPos center, int[] distances) {
        JsonObject result = new JsonObject();

        for (int i = 0; i < DIRS.length; i++) {
            int dx = DIRS[i][0], dz = DIRS[i][1];
            JsonArray dirArr = new JsonArray();

            for (int dist : distances) {
                int sx = center.getX() + dx * dist;
                int sz = center.getZ() + dz * dist;
                BlockPos sampleXZ = new BlockPos(sx, center.getY(), sz);

                JsonObject sample = new JsonObject();
                sample.addProperty("distance", dist);

                if (!world.isChunkLoaded(sampleXZ)) {
                    // 区块未加载，更远处也必然未加载，停止此方向采样
                    sample.addProperty("status", "未探索区域（区块未加载）");
                    dirArr.add(sample);
                    break;
                }

                // 生物群系（使用地表 Y 附近采样点）
                var biomeEntry = world.getBiome(sampleXZ);
                String biomeId = biomeEntry.getKey()
                        .map(k -> k.getValue().toString())
                        .orElse("unknown");
                sample.addProperty("biome", biomeId);
                sample.addProperty("hint", getBiomeHint(biomeId));

                // 地表高度（WORLD_SURFACE 类型排除树叶等透明方块）
                int surfaceY = world.getTopY(Heightmap.Type.WORLD_SURFACE, sx, sz);
                sample.addProperty("surface_y", surfaceY);

                // 地表方块 ID（surfaceY - 1 是最高的实心方块）
                String surfaceBlockId = Registries.BLOCK.getId(
                        world.getBlockState(new BlockPos(sx, surfaceY - 1, sz)).getBlock()
                ).toString();
                sample.addProperty("surface_block", surfaceBlockId);
                sample.addProperty("terrain", getTerrainHint(surfaceBlockId, surfaceY));

                dirArr.add(sample);
            }

            result.add(DIR_EN[i], dirArr);
        }

        return result;
    }

    /**
     * 生成地平线扫描的人类可读文字摘要。
     *
     * 供 look_around 行动调用，返回给 Python Agent 的 LLM 推理使用。
     * 格式：方向概览 + 导航建议（包含具体目标坐标）。
     *
     * @param world     当前服务器世界
     * @param center    扫描中心（Agent 坐标）
     * @param maxRadius 最大扫描半径（格），最大 256
     */
    public static String generateSummary(ServerWorld world, BlockPos center, int maxRadius) {
        // 根据 maxRadius 选取采样距离列表
        List<Integer> distList = new ArrayList<>();
        for (int d : new int[]{48, 96, 128, 192, 256}) {
            if (d <= maxRadius) distList.add(d);
        }
        if (distList.isEmpty()) distList.add(48);
        int[] distances = distList.stream().mapToInt(Integer::intValue).toArray();

        JsonObject horizonData = scanHorizon(world, center, distances);

        StringBuilder sb = new StringBuilder();
        sb.append(String.format(
                "=== 远望扫描（玩家位置 x=%d z=%d，最大%d格）===\n",
                center.getX(), center.getZ(), maxRadius));

        // 每个方向的详细信息
        for (int i = 0; i < DIR_EN.length; i++) {
            String dirEn = DIR_EN[i];
            String dirZh = DIR_ZH[i];
            JsonArray samples = horizonData.getAsJsonArray(dirEn);
            if (samples == null || samples.isEmpty()) continue;

            sb.append("  ").append(dirZh).append("：");
            for (var el : samples) {
                JsonObject s = el.getAsJsonObject();
                if (s.has("status")) {
                    sb.append(" 超出已加载范围（未探索区域）");
                    break;
                }
                int dist    = s.get("distance").getAsInt();
                String hint = s.has("hint")    ? s.get("hint").getAsString()    : "未知地形";
                int sy      = s.has("surface_y") ? s.get("surface_y").getAsInt() : 64;
                String terr = s.has("terrain") ? s.get("terrain").getAsString() : "";
                sb.append(String.format("\n    %d格: %s [地表y=%d, %s]", dist, hint, sy, terr));
            }
            sb.append("\n");
        }

        // 导航建议
        sb.append("\n【导航建议（根据以上地形，直接用 move_to 坐标前往）】\n");
        appendNavHints(sb, horizonData, center);

        return sb.toString();
    }

    // ── 内部：导航建议生成 ────────────────────────────────────────────────────

    /**
     * 从地平线数据中提炼出各类资源的方向和坐标建议。
     * 例如："木材/树木 → 东北48格 (x=150,z=-80)，那里是橡木森林"
     */
    private static void appendNavHints(StringBuilder sb, JsonObject horizonData, BlockPos center) {
        // 资源类别 → (描述, 方向距离坐标)
        Map<String, String> resourceHints = new LinkedHashMap<>();

        for (int i = 0; i < DIR_EN.length; i++) {
            JsonArray samples = horizonData.getAsJsonArray(DIR_EN[i]);
            if (samples == null) continue;

            for (var el : samples) {
                JsonObject s = el.getAsJsonObject();
                if (!s.has("biome")) continue;

                String biome   = s.get("biome").getAsString();
                int    dist    = s.get("distance").getAsInt();
                String hint    = s.has("hint") ? s.get("hint").getAsString() : biome;
                int tx = center.getX() + DIRS[i][0] * dist;
                int tz = center.getZ() + DIRS[i][1] * dist;

                String dirDesc = String.format("%s%d格 (x=%d,z=%d) — %s",
                        DIR_ZH[i], dist, tx, tz, hint);

                // 木材资源
                if (isWoodBiome(biome))
                    resourceHints.putIfAbsent("木材/树木", dirDesc);
                // 村庄/交易
                if (isVillageBiome(biome))
                    resourceHints.putIfAbsent("村庄（村民交易/绿宝石）", dirDesc);
                // 沙漠
                if (biome.contains("desert"))
                    resourceHints.putIfAbsent("沙子/仙人掌/沙漠神殿", dirDesc);
                // 山地矿石
                if (isMountainBiome(biome))
                    resourceHints.putIfAbsent("裸露矿石/煤炭（地表可见）", dirDesc);
                // 水域
                if (biome.contains("ocean") || biome.contains("river"))
                    resourceHints.putIfAbsent("水源/鱼类", dirDesc);
                // 竹子
                if (biome.contains("bamboo"))
                    resourceHints.putIfAbsent("竹子（竹林/熊猫）", dirDesc);
                // 蘑菇岛
                if (biome.contains("mushroom"))
                    resourceHints.putIfAbsent("蘑菇岛（极安全，哞菇）", dirDesc);
                // 恶地（金矿）
                if (biome.contains("badlands"))
                    resourceHints.putIfAbsent("恶地（金矿地表暴露）", dirDesc);
                // 下界
                if (biome.contains("nether"))
                    resourceHints.putIfAbsent("下界地形（下界石英/金矿）", dirDesc);
                // 冰/雪（蓝冰/冰刺）
                if (biome.contains("ice_spikes") || biome.contains("frozen"))
                    resourceHints.putIfAbsent("冰/蓝冰（冰刺地形）", dirDesc);
            }
        }

        if (resourceHints.isEmpty()) {
            sb.append("  当前已加载视野内未发现特殊资源地形（均为普通地形）。\n");
            sb.append("  建议：先移动探索更多区域，再用 look_around 扩大半径重新扫描。\n");
        } else {
            resourceHints.forEach((resource, dir) ->
                    sb.append("  ").append(resource).append("\n    -> ").append(dir).append("\n"));
        }
    }

    // ── 内部：生物群系分类辅助 ────────────────────────────────────────────────

    private static boolean isWoodBiome(String biome) {
        return biome.contains("forest") || biome.contains("jungle") ||
               biome.contains("taiga")  || biome.contains("birch")  ||
               biome.contains("dark_forest") || biome.contains("cherry") ||
               biome.contains("grove")  || biome.contains("savanna") ||
               biome.contains("mangrove");
    }

    private static boolean isVillageBiome(String biome) {
        return biome.equals("minecraft:plains")       ||
               biome.equals("minecraft:savanna")      ||
               biome.equals("minecraft:taiga")        ||
               biome.equals("minecraft:desert")       ||
               biome.equals("minecraft:snowy_plains") ||
               biome.equals("minecraft:meadow");
    }

    private static boolean isMountainBiome(String biome) {
        return biome.contains("peaks")      || biome.contains("stony") ||
               biome.contains("windswept")  || biome.contains("hills") ||
               biome.contains("slope")      || biome.equals("minecraft:jagged_peaks") ||
               biome.equals("minecraft:frozen_peaks");
    }

    // ── 公共：生物群系提示语 ──────────────────────────────────────────────────

    /**
     * 将 Minecraft 生物群系 ID 转换为中文说明（含关键资源信息）。
     */
    public static String getBiomeHint(String biomeId) {
        return switch (biomeId) {
            // ── 森林系 ──────────────────────────────────────────────────────
            case "minecraft:forest"                    -> "橡木森林（大量橡木，砍木首选）";
            case "minecraft:birch_forest"              -> "桦木森林（大量桦木）";
            case "minecraft:dark_forest"               -> "黑森林（深色橡木/蘑菇/暗处危险）";
            case "minecraft:flower_forest"             -> "花朵森林（橡木/各类花朵/蜜蜂）";
            case "minecraft:old_growth_birch_forest"   -> "古老桦木森林（超高桦木）";
            case "minecraft:old_growth_pine_taiga",
                 "minecraft:old_growth_taiga"          -> "古老针叶林（大量高大云杉）";
            case "minecraft:taiga"                     -> "针叶林（云杉木/甜浆果灌木）";
            case "minecraft:snowy_taiga"               -> "雪针叶林（云杉木/雪）";
            case "minecraft:windswept_forest"          -> "风吹森林（橡木+石质地形）";
            case "minecraft:jungle"                    -> "热带丛林（丛林木/可可豆/竹子/豹猫）";
            case "minecraft:bamboo_jungle"             -> "竹林（大量竹子/熊猫，竹子需求首选）";
            case "minecraft:sparse_jungle"             -> "稀疏丛林（热带鱼/可可豆）";
            case "minecraft:cherry_grove"              -> "樱花树林（樱花木，装饰性强）";
            case "minecraft:mangrove_swamp"            -> "红树林沼泽（红树木/泥土/蛙）";

            // ── 平原/草地系 ─────────────────────────────────────────────────
            case "minecraft:plains"                    -> "平原（村庄常见/羊牛猪马大量生成，建造首选）";
            case "minecraft:sunflower_plains"          -> "向日葵平原（向日葵/村庄，黄色染料来源）";
            case "minecraft:meadow"                    -> "草甸（蜜蜂/鲜花/羊，常在山脉附近）";
            case "minecraft:savanna"                   -> "稀树草原（金合欢木/村庄/马/羊驼）";
            case "minecraft:savanna_plateau"           -> "稀树草原高原（金合欢木/马）";
            case "minecraft:windswept_savanna"         -> "风吹稀树草原（金合欢木，地形陡峭）";

            // ── 沙漠/干旱系 ─────────────────────────────────────────────────
            case "minecraft:desert"                    -> "沙漠（沙子/仙人掌/沙漠神殿/村庄，无夜晚生物）";
            case "minecraft:badlands"                  -> "恶地（红砂/赤陶黏土，金矿地表暴露！）";
            case "minecraft:wooded_badlands"           -> "林地恶地（恶地地形+树木，金矿暴露）";
            case "minecraft:eroded_badlands"           -> "侵蚀恶地（奇特地形，金矿暴露）";

            // ── 沼泽系 ──────────────────────────────────────────────────────
            case "minecraft:swamp"                     -> "沼泽（女巫小屋/睡莲/蓝花楹/青蛙，宝藏粗糙黏土）";

            // ── 海岸/海洋系 ─────────────────────────────────────────────────
            case "minecraft:beach"                     -> "沙滩（沙子/砂岩，附近有海洋和水下遗迹）";
            case "minecraft:snowy_beach"               -> "雪沙滩（雪/沙子，寒冷）";
            case "minecraft:stony_shore"               -> "石质海岸（石头，矿石可能裸露）";
            case "minecraft:ocean"                     -> "海洋（鱼/墨鱼，水下可能有海底遗迹）";
            case "minecraft:deep_ocean"                -> "深海（海底神殿，守卫者/远古守卫者，满级宝物）";
            case "minecraft:cold_ocean"                -> "寒冷海洋（鳕鱼/鲑鱼，偶有冰山）";
            case "minecraft:frozen_ocean"              -> "冰冻海洋（北极熊/冰块/流浮冰）";
            case "minecraft:warm_ocean"                -> "温暖海洋（珊瑚礁/热带鱼，视觉最美）";
            case "minecraft:lukewarm_ocean"            -> "温水海洋（多鱼类/墨鱼）";
            case "minecraft:river"                     -> "河流（水源充足/鱼类/泥土）";
            case "minecraft:frozen_river"              -> "冻河（冰块，水源）";

            // ── 雪地/冰原系 ─────────────────────────────────────────────────
            case "minecraft:snowy_plains"              -> "雪原（雪/冰，村庄可能在此，夜晚危险）";
            case "minecraft:ice_spikes"                -> "冰刺（蓝冰大量，稀有地形，建筑材料）";
            case "minecraft:grove"                     -> "针叶树丛（云杉/雪，山区常见）";
            case "minecraft:snowy_slopes"              -> "雪坡（雪/石头，山区过渡地带）";

            // ── 山地系 ──────────────────────────────────────────────────────
            case "minecraft:windswept_hills"           -> "风吹丘陵（石头大量裸露，矿石可能在地表）";
            case "minecraft:windswept_gravelly_hills"  -> "风吹砾石丘陵（砾石/石头，矿石暴露可能性高）";
            case "minecraft:stony_peaks"               -> "石峰（大量裸石，煤/铁矿地表可见！）";
            case "minecraft:jagged_peaks"              -> "锯齿山峰（石头/雪，极高地形，视野好）";
            case "minecraft:frozen_peaks"              -> "冰封山峰（冰/雪/石头，山顶极寒）";

            // ── 特殊地形 ────────────────────────────────────────────────────
            case "minecraft:mushroom_fields"           -> "蘑菇岛（蘑菇/哞菇，无任何敌对生物刷新，极安全！）";
            case "minecraft:dripstone_caves"           -> "滴水石洞（钟乳石，地下洞穴系统入口区）";
            case "minecraft:lush_caves"                -> "繁茂洞穴（垂根/发光浆果，地下绿洲）";
            case "minecraft:deep_dark"                 -> "深暗之境（监守者极危险，古城遗迹含稀有战利品）";

            // ── 下界 ────────────────────────────────────────────────────────
            case "minecraft:nether_wastes"             -> "下界荒地（石英矿/下界金矿/恶魂）";
            case "minecraft:crimson_forest"            -> "绯红森林（绯红菌木/疣猪兽/猪灵）";
            case "minecraft:warped_forest"             -> "诡异森林（诡异菌木/末影人聚集，相对安全）";
            case "minecraft:soul_sand_valley"          -> "灵魂沙谷（灵魂沙/骷髅/恶魂，大量骨头）";
            case "minecraft:basalt_deltas"             -> "玄武岩三角洲（玄武岩/岩浆怪，地形崎岖）";

            // ── 末地 ────────────────────────────────────────────────────────
            case "minecraft:the_end"                   -> "末地中心（末影龙/末地石）";
            case "minecraft:end_highlands"             -> "末地高地（末影城/翅鞘，高等装备）";
            case "minecraft:end_midlands"              -> "末地中地（末地石/末影人）";
            case "minecraft:small_end_islands"         -> "末地小岛（末地石，末影人）";
            case "minecraft:end_barrens"               -> "末地荒原（末地石）";

            default -> biomeId.replace("minecraft:", "").replace("_", " ") + "（未知地形）";
        };
    }

    // ── 公共：地表地形提示 ────────────────────────────────────────────────────

    /**
     * 根据地表方块 ID 和高度生成地形提示文字。
     */
    public static String getTerrainHint(String surfaceBlock, int surfaceY) {
        if (surfaceY >= 130) return "极高地形/山顶（矿石可能大量裸露在地表）";
        if (surfaceY >= 100) return "高地/山地（石头裸露，可能有矿洞入口）";
        if (surfaceY >= 80)  return "丘陵地形（偶有石头裸露）";
        if (surfaceY <= 58)  return "低洼地形（可能有水域/沼泽）";

        if (surfaceBlock.contains("sand") || surfaceBlock.contains("sandstone"))
            return "沙质地表（沙漠/海滩）";
        if (surfaceBlock.contains("gravel"))
            return "砾石地表（河床/沙砾丘陵，常有铁矿伴生）";
        if (surfaceBlock.contains("stone") || surfaceBlock.contains("deepslate") ||
            surfaceBlock.contains("cobblestone"))
            return "石质裸露地表（矿洞入口可能在附近，可直接找矿）";
        if (surfaceBlock.contains("snow") || surfaceBlock.contains("powder_snow"))
            return "雪地地表（雪原/山地）";
        if (surfaceBlock.contains("ice") || surfaceBlock.contains("packed_ice") ||
            surfaceBlock.contains("blue_ice"))
            return "冰面（冻河/冰洋，蓝冰资源）";
        if (surfaceBlock.equals("minecraft:water"))
            return "水面（海洋/河流，需要方舟/呼吸药水）";
        if (surfaceBlock.contains("_log") || surfaceBlock.contains("_stem"))
            return "树木地表（森林密集区域，伐木效率高）";
        if (surfaceBlock.contains("moss") || surfaceBlock.contains("mud"))
            return "苔藓/泥土地表（沼泽/红树林区域）";
        if (surfaceBlock.contains("mycelium"))
            return "菌丝地表（蘑菇岛，极安全）";
        if (surfaceBlock.contains("podzol"))
            return "灰化土地表（针叶林/丛林区域）";
        if (surfaceBlock.contains("terracotta") || surfaceBlock.contains("clay"))
            return "陶土/黏土地表（恶地区域，金矿可能暴露）";
        if (surfaceBlock.contains("grass") || surfaceBlock.contains("dirt"))
            return "草地（正常平原/森林地表）";

        return "普通地表（y=" + surfaceY + "）";
    }
}
