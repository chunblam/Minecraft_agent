package com.minecraftagent.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.minecraftagent.network.AgentWebSocketClient;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import com.minecraftagent.util.AgentLogger;
import com.minecraftagent.util.NativePathfinder;
import com.minecraftagent.util.WorldScanner;
import net.minecraft.block.Block;
import net.minecraft.block.Blocks;
import net.minecraft.enchantment.Enchantment;
import net.minecraft.enchantment.EnchantmentHelper;
import net.minecraft.entity.Entity;
import net.minecraft.entity.passive.PassiveEntity;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.item.Items;
import net.minecraft.recipe.CraftingRecipe;
import net.minecraft.recipe.Ingredient;
import net.minecraft.recipe.RecipeEntry;
import net.minecraft.recipe.RecipeType;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKey;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.collection.DefaultedList;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Vec3d;
import net.minecraft.world.Heightmap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 游戏行动执行器（v3 —— 客户端输入模拟优先）。
 *
 * move_to / mine_block：通过 AgentInputPayload 下发到客户端，模拟 WASD + 鼠标点击；
 * 客户端无 Mod 时回退到 PlayerTaskRunner 服务端直接操控。
 *
 * 其余行动为即时执行（无作弊指令）：
 *   craft_item / enchant_item / place_block / interact_entity / get_inventory 等
 *
 * 每次行动执行后附带 game_state 快照发回 Python。
 */
public class ActionExecutor {

    private static final Logger LOGGER = LoggerFactory.getLogger("ActionExecutor");

    private final MinecraftServer server;
    @SuppressWarnings("unused")
    private final AgentWebSocketClient wsClient;
    private final PlayerTaskRunner taskRunner;

    public ActionExecutor(MinecraftServer server, AgentWebSocketClient wsClient) {
        this.server = server;
        this.wsClient = wsClient;
        this.taskRunner = new PlayerTaskRunner();
        ServerTickEvents.END_SERVER_TICK.register(s -> taskRunner.tick());
    }

    // ── 执行入口 ──────────────────────────────────────────────────────────────

    /** 行动结果：观察字符串 + 游戏状态快照 + 是否成功（move_to 超时等为 false） */
    public record ActionResult(String observation, JsonObject gameStateUpdate, boolean success) {
        /** 兼容旧调用：无 success 时默认为 true */
        public ActionResult(String observation, JsonObject gameStateUpdate) {
            this(observation, gameStateUpdate, true);
        }
    }

    /**
     * 执行行动，完成后调用 onComplete。
     * move_to / mine_block 通过客户端模拟 WASD+鼠标输入执行（与 Python 规划/技能联动），
     * 其余动作为即时执行。
     *
     * @param requestId Python 下发的请求 ID，用于客户端回报时匹配
     */
    public void executeWithState(
            String requestId,
            String actionType, JsonObject params, String displayMessage,
            java.util.function.Consumer<ActionResult> onComplete) {
        if ("move_to".equals(actionType) || "mine_block".equals(actionType)) {
            executeAsyncAction(requestId, actionType, params, onComplete);
        } else {
            String observation = execute(actionType, params, displayMessage);
            onComplete.accept(new ActionResult(observation, buildStateUpdate(), true));
        }
    }

    private void executeAsyncAction(String requestId, String actionType, JsonObject params,
                                    java.util.function.Consumer<ActionResult> onComplete) {
        ServerPlayerEntity player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) {
            onComplete.accept(new ActionResult("无可用玩家", buildStateUpdate(), false));
            return;
        }

        ServerWorld world = (ServerWorld) player.getWorld();
        double x = getDouble(params, "x", player.getX());
        double y = getDouble(params, "y", player.getY());
        double z = getDouble(params, "z", player.getZ());

        if ("move_to".equals(actionType)) {
            BlockPos resolved = resolveMoveToTarget(world, player, params);
            if (resolved != null) {
                x = resolved.getX() + 0.5;
                y = resolved.getY();
                z = resolved.getZ() + 0.5;
            }
        }

        var completion = new com.minecraftagent.network.AgentNetworking.PendingCompletion(
                onComplete,
                this::buildStateUpdate
        );

        if ("move_to".equals(actionType)) {
            BlockPos from = player.getBlockPos();
            BlockPos to = new BlockPos((int) Math.round(x), (int) Math.round(y), (int) Math.round(z));
            var path = NativePathfinder.findPath(world, from, to);
            if (path != null && path.size() >= 2) {
                boolean sentPath = com.minecraftagent.network.AgentNetworking.sendPathToClient(
                        player, requestId, actionType, path, completion);
                if (sentPath) return;
            }
        }

        boolean sent = com.minecraftagent.network.AgentNetworking.sendInputToClient(
                player, requestId, actionType, x, y, z, completion);
        if (!sent) {
            // 回退：客户端无 Mod 时使用服务端直接操控
            if ("move_to".equals(actionType)) {
                taskRunner.runMoveTo(player, x, y, z)
                        .thenAccept(obs -> onComplete.accept(new ActionResult(obs, buildStateUpdate(), true)))
                        .exceptionally(ex -> {
                            onComplete.accept(new ActionResult("移动失败: " + ex.getMessage(), buildStateUpdate(), false));
                            return null;
                        });
            } else if ("mine_block".equals(actionType)) {
                taskRunner.runMineBlock(player, (int) x, (int) y, (int) z)
                        .thenAccept(obs -> onComplete.accept(new ActionResult(obs, buildStateUpdate(), true)))
                        .exceptionally(ex -> {
                            onComplete.accept(new ActionResult("挖掘失败: " + ex.getMessage(), buildStateUpdate(), false));
                            return null;
                        });
            }
        }
    }

    /**
     * 执行行动，返回人类可读的观察字符串。
     */
    public String execute(String actionType, JsonObject params, String displayMessage) {
        LOGGER.info("执行行动: {} | params={}", actionType, params);
        try {
            String result = switch (actionType) {
                case "chat"            -> executeChat(displayMessage);
                case "move_to"         -> "move_to 应由 executeWithState 异步执行";
                case "mine_block"      -> "mine_block 应由 executeWithState 异步执行";
                case "place_block"     -> executePlaceBlock(params);
                case "craft_item"      -> executeCraftItem(params);
                case "enchant_item"    -> executeEnchantItem(params);
                case "interact_entity" -> executeInteractEntity(params);
                case "get_inventory"   -> executeGetInventory();
                case "find_resource"   -> executeFindResource(params);
                case "scan_area"       -> executeScanArea(params);
                case "look_around"     -> executeLookAround(params);
                case "follow_player"   -> executeFollowPlayer(params);
                case "look_at"         -> executeLookAt(params);
                case "turn"            -> executeTurn(params);
                case "jump"            -> executeJump();
                case "stop"            -> executeStop();
                case "finish"          -> {
                    if (displayMessage != null && !displayMessage.isEmpty()) {
                        broadcastAgentMessage(displayMessage);
                    }
                    yield "任务已完成";
                }
                default -> {
                    LOGGER.warn("未知行动类型: {}", actionType);
                    yield "未知行动类型: " + actionType;
                }
            };
            // 记录到专属日志文件
            AgentLogger.action(actionType, params, result);
            return result;
        } catch (Exception e) {
            LOGGER.error("行动执行异常 [{}]: {}", actionType, e.getMessage(), e);
            AgentLogger.error("ActionExecutor", "行动异常 [" + actionType + "]: " + e.getMessage());
            return "行动执行失败: " + e.getMessage();
        }
    }

    // ── 状态快照 ──────────────────────────────────────────────────────────────

    /** 供 AgentNetworking 在客户端完成回调时获取最新状态 */
    public JsonObject buildStateUpdate() {
        JsonObject update = new JsonObject();
        ServerPlayerEntity player = PlayerContext.getCurrentOrFirst(server);
        if (player != null) {
            JsonObject pos = new JsonObject();
            pos.addProperty("x", Math.round(player.getX()));
            pos.addProperty("y", Math.round(player.getY()));
            pos.addProperty("z", Math.round(player.getZ()));
            update.add("position", pos);
            update.add("agent_position", pos); // 与 position 一致（兼容字段）
            update.add("inventory", buildFullInventory(player));
            update.addProperty("health", Math.round(player.getHealth()));
            update.addProperty("hunger", player.getHungerManager().getFoodLevel());
            update.addProperty("xp_level", player.experienceLevel);
        }
        return update;
    }

    /**
     * 采集玩家完整背包（41格：主背包36 + 盔甲4 + 副手1）。
     */
    public JsonArray buildFullInventory(ServerPlayerEntity player) {
        JsonArray inventory = new JsonArray();
        for (int i = 0; i < player.getInventory().size(); i++) {
            ItemStack stack = player.getInventory().getStack(i);
            if (!stack.isEmpty()) {
                JsonObject item = new JsonObject();
                item.addProperty("slot", i);
                item.addProperty("item", Registries.ITEM.getId(stack.getItem()).toString());
                item.addProperty("count", stack.getCount());
                item.addProperty("is_hotbar", i < 9);
                inventory.add(item);
            }
        }
        return inventory;
    }

    // ── 基础行动 ──────────────────────────────────────────────────────────────

    public void broadcastAgentMessage(String message) {
        server.getPlayerManager().broadcast(Text.literal("§b[晨曦]§r " + message), false);
        LOGGER.info("[晨曦] {}", message);
    }

    private String executeChat(String message) {
        if (message == null || message.isBlank()) return "消息为空，未发送";
        broadcastAgentMessage(message);
        return "成功发送消息: " + message;
    }

    @SuppressWarnings("unused")
    private String executeMoveTo(JsonObject params) {
        return "move_to 已由 tick 级 PlayerTaskRunner 执行";
    }

    @SuppressWarnings("unused")
    private String executeMineBlock(JsonObject params) {
        return "mine_block 已由 tick 级 PlayerTaskRunner 执行";
    }

    private String executePlaceBlock(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";

        ServerWorld world = (ServerWorld) player.getWorld();
        int x = getInt(params, "x", player.getBlockPos().getX());
        int y = getInt(params, "y", player.getBlockPos().getY());
        int z = getInt(params, "z", player.getBlockPos().getZ());
        String blockId = getString(params, "block", "minecraft:dirt");

        Identifier bid = Identifier.tryParse(blockId);
        if (bid == null) return "无效方块 ID: " + blockId;

        Block block = Registries.BLOCK.get(bid);
        if (block == Blocks.AIR && !blockId.equals("minecraft:air"))
            return "未知方块类型: " + blockId;

        boolean placed = world.setBlockState(new BlockPos(x, y, z), block.getDefaultState());
        return placed
                ? String.format("成功放置 [%s] 在 (%d,%d,%d)", blockId, x, y, z)
                : String.format("放置失败: (%d,%d,%d)", x, y, z);
    }

    // ── 合成（正当机制：查配方 → 检查材料 → 消耗材料 → 给予产物） ─────────────

    /**
     * craft_item：通过游戏配方系统合成物品。
     *
     * 流程：
     *   1. 从 RecipeManager 查找目标物品的合成配方
     *   2. 检查玩家背包是否拥有配方所需的全部材料
     *   3. 若材料不足，返回缺少哪些材料（供 LLM 规划采集步骤）
     *   4. 若材料充足，消耗材料并将产物放入玩家背包
     *
     * 不使用任何控制台指令，不绕过材料需求。
     *
     * params:
     *   item  (String) - 目标物品 ID（如 "minecraft:diamond_sword"）
     *   count (int)    - 需要的数量（默认 1，自动计算需要合成几次）
     */
    private String executeCraftItem(JsonObject params) {
        String itemId = getString(params, "item", null);
        int requestedCount = Math.max(1, getInt(params, "count", 1));

        if (itemId == null) return "缺少 item 参数（需要指定要合成什么）";

        ServerPlayerEntity player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "没有在线玩家";

        Identifier itemId2 = Identifier.tryParse(itemId);
        if (itemId2 == null) return "无效物品 ID: " + itemId;

        Item targetItem = Registries.ITEM.get(itemId2);
        if (targetItem == Items.AIR) return "未知物品类型: " + itemId;

        // ① 从 RecipeManager 搜索合成配方
        List<RecipeEntry<CraftingRecipe>> matchingRecipes = server.getRecipeManager()
                .listAllOfType(RecipeType.CRAFTING)
                .stream()
                .filter(e -> e.value().getResult(server.getRegistryManager()).isOf(targetItem))
                .toList();

        if (matchingRecipes.isEmpty()) {
            return "找不到 [" + itemId + "] 的合成配方。请检查物品 ID 是否正确，或该物品不能合成（需要其他方式获取）";
        }

        // ② 尝试每个配方，找到一个材料充足的
        for (RecipeEntry<CraftingRecipe> entry : matchingRecipes) {
            CraftingRecipe recipe = entry.value();
            ItemStack output = recipe.getResult(server.getRegistryManager());
            DefaultedList<Ingredient> ingredients = recipe.getIngredients();

            // 计算需要合成几次（向上取整）
            int craftTimes = (requestedCount + output.getCount() - 1) / output.getCount();

            // 检查材料是否充足（craftTimes 次的总材料量）
            String missingMsg = checkIngredients(player, ingredients, craftTimes);
            if (missingMsg != null) {
                return "合成 [" + itemId + "] 所需材料不足：" + missingMsg
                        + "。请先收集这些材料再尝试合成";
            }

            // ③ 材料充足：消耗材料
            consumeIngredients(player, ingredients, craftTimes);

            // ④ 将产物放入背包
            int totalOutput = output.getCount() * craftTimes;
            ItemStack result = new ItemStack(targetItem, totalOutput);
            boolean inserted = player.getInventory().insertStack(result);
            player.getInventory().markDirty();

            if (inserted || result.isEmpty()) {
                return String.format("成功合成 [%s] x%d（已消耗所需材料）", itemId, totalOutput);
            } else {
                return "合成成功但背包已满，物品已掉落在地上";
            }
        }

        // 所有配方的材料都不足时（取第一个配方报告缺少什么）
        CraftingRecipe firstRecipe = matchingRecipes.get(0).value();
        String missing = checkIngredients(player, firstRecipe.getIngredients(), 1);
        return "材料不足，无法合成 [" + itemId + "]。缺少：" + missing;
    }

    /**
     * 检查玩家背包是否有指定配方 times 次所需的全部材料。
     *
     * @return null = 材料充足；non-null = 缺少材料的描述字符串
     */
    private String checkIngredients(
            ServerPlayerEntity player,
            DefaultedList<Ingredient> ingredients,
            int times) {

        // 构建"需要消耗的材料表"：每个 Ingredient 需要 times 个
        List<Ingredient> needed = new ArrayList<>();
        for (int t = 0; t < times; t++) {
            for (Ingredient ingredient : ingredients) {
                if (!ingredient.isEmpty()) needed.add(ingredient);
            }
        }

        // 模拟扣除：记录每个槽位被预留了多少
        Map<Integer, Integer> slotUsed = new HashMap<>();
        List<String> missing = new ArrayList<>();

        for (Ingredient ingredient : needed) {
            boolean found = false;
            for (int i = 0; i < player.getInventory().size(); i++) {
                ItemStack stack = player.getInventory().getStack(i);
                int used = slotUsed.getOrDefault(i, 0);
                if (stack.getCount() - used > 0 && ingredient.test(stack)) {
                    slotUsed.merge(i, 1, Integer::sum);
                    found = true;
                    break;
                }
            }
            if (!found) {
                // 获取该材料的一个代表名称
                ItemStack[] candidates = ingredient.getMatchingStacks();
                String name = (candidates != null && candidates.length > 0)
                        ? Registries.ITEM.getId(candidates[0].getItem()).getPath()
                        : "某种材料";
                missing.add(name);
            }
        }

        return missing.isEmpty() ? null : String.join(", ", missing);
    }

    /**
     * 从玩家背包消耗配方所需材料（必须在 checkIngredients 返回 null 后调用）。
     */
    private void consumeIngredients(
            ServerPlayerEntity player,
            DefaultedList<Ingredient> ingredients,
            int times) {

        List<Ingredient> needed = new ArrayList<>();
        for (int t = 0; t < times; t++) {
            for (Ingredient ingredient : ingredients) {
                if (!ingredient.isEmpty()) needed.add(ingredient);
            }
        }

        Map<Integer, Integer> slotUsed = new HashMap<>();
        for (Ingredient ingredient : needed) {
            for (int i = 0; i < player.getInventory().size(); i++) {
                ItemStack stack = player.getInventory().getStack(i);
                int used = slotUsed.getOrDefault(i, 0);
                if (stack.getCount() - used > 0 && ingredient.test(stack)) {
                    slotUsed.merge(i, 1, Integer::sum);
                    break;
                }
            }
        }

        for (var e : slotUsed.entrySet()) {
            player.getInventory().getStack(e.getKey()).decrement(e.getValue());
        }
        player.getInventory().markDirty();
    }

    // ── 附魔（需要经验等级 + 青金石，正当消耗） ──────────────────────────────

    /**
     * enchant_item：对玩家手持物品附魔，需消耗经验等级和青金石。
     *
     * 消耗规则（相对简化，接近原版精神）：
     *   经验等级消耗 = 附魔等级 × 5
     *   青金石消耗   = 附魔等级
     *
     * params:
     *   slot        (String) - "mainhand"（默认）/ "offhand" / 数字槽位
     *   enchantment (String) - 附魔 ID（如 "minecraft:power"）
     *   level       (int)    - 附魔等级（1-5）
     */
    private String executeEnchantItem(JsonObject params) {
        ServerPlayerEntity player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "没有在线玩家";

        String slotStr    = getString(params, "slot", "mainhand");
        String enchantId  = getString(params, "enchantment", null);
        int    level      = Math.max(1, getInt(params, "level", 1));

        if (enchantId == null) return "缺少 enchantment 参数（需要指定附魔类型）";

        // 获取目标物品
        ItemStack stack = getItemFromSlot(player, slotStr);
        if (stack == null || stack.isEmpty())
            return "该槽位没有物品，请先将目标物品拿在手上";

        // 查找附魔注册
        Identifier enchantIdentifier = Identifier.tryParse(enchantId);
        if (enchantIdentifier == null) return "无效附魔 ID: " + enchantId;

        RegistryKey<Enchantment> key = RegistryKey.of(RegistryKeys.ENCHANTMENT, enchantIdentifier);
        var enchantEntry = server.getRegistryManager()
                .get(RegistryKeys.ENCHANTMENT)
                .getEntry(key);
        if (enchantEntry.isEmpty()) return "未知附魔类型: " + enchantId;

        // 检查经验等级（每级附魔需要 level×5 经验等级）
        int xpCost = level * 5;
        if (player.experienceLevel < xpCost) {
            return String.format(
                    "经验等级不足：%s %d 级需要 %d 级经验，当前只有 %d 级",
                    enchantId, level, xpCost, player.experienceLevel);
        }

        // 检查青金石（每级附魔需要 level 个青金石）
        int lapisNeeded = level;
        int lapisHave   = countItem(player, Items.LAPIS_LAZULI);
        if (lapisHave < lapisNeeded) {
            return String.format(
                    "青金石不足：需要 %d 个，背包中只有 %d 个",
                    lapisNeeded, lapisHave);
        }

        // 应用附魔（通过 EnchantmentHelper 修改 ItemStack 的附魔组件）
        var builder = new net.minecraft.component.type.ItemEnchantmentsComponent.Builder(
                EnchantmentHelper.getEnchantments(stack));
        builder.set(enchantEntry.get(), level);
        EnchantmentHelper.set(stack, builder.build());
        player.getInventory().markDirty();

        // 扣除资源
        player.addExperienceLevels(-xpCost);
        removeItem(player, Items.LAPIS_LAZULI, lapisNeeded);

        return String.format(
                "成功对 [%s] 施加附魔 %s %d 级（消耗 %d 级经验 + %d 个青金石）",
                stack.getItem().toString(), enchantId, level, xpCost, lapisNeeded);
    }

    // ── 实体互动 ──────────────────────────────────────────────────────────────

    /**
     * interact_entity：与附近指定类型实体互动。
     *
     * params:
     *   entity_type    (String) - 实体类型关键词（如 "sheep", "cow", "villager"）
     *   action         (String) - 互动类型：
     *     "find"              - 仅查找并报告坐标
     *     "move_to_pos"       - 将实体移动到指定坐标 (x, y, z)
     *     "teleport_to_agent" - 将实体传送到玩家当前位置（兼容旧名）
     *   x, y, z        (double) - 目标坐标（action=move_to_pos 时使用）
     *   max_distance   (double) - 搜索半径（默认 20 格）
     */
    private String executeInteractEntity(JsonObject params) {
        var ctrlPlayer = PlayerContext.getCurrentOrFirst(server);
        if (ctrlPlayer == null) return "无可用玩家";

        ServerWorld world  = (ServerWorld) ctrlPlayer.getWorld();
        String entityType  = getString(params, "entity_type", null);
        String action      = getString(params, "action", "find");
        double maxDist     = getDouble(params, "max_distance", 20.0);

        // 搜索附近匹配实体
        Box searchBox = ctrlPlayer.getBoundingBox().expand(maxDist);
        List<Entity> candidates = world.getOtherEntities(ctrlPlayer, searchBox, e -> {
            if (entityType == null) return true;
            return e.getType().toString().toLowerCase().contains(entityType.toLowerCase())
                    || e.getName().getString().toLowerCase().contains(entityType.toLowerCase());
        });

        if (candidates.isEmpty()) {
            return String.format("附近 %.0f 格内没有找到 [%s]，请扩大搜索范围或移动到生物聚集区域",
                    maxDist, entityType != null ? entityType : "指定实体");
        }

        // 找最近的实体
        Entity target = candidates.stream()
                .min(Comparator.comparingDouble(e -> e.squaredDistanceTo(ctrlPlayer)))
                .orElse(candidates.get(0));

        String desc = String.format("%s 在 (%.0f,%.0f,%.0f)",
                target.getName().getString(), target.getX(), target.getY(), target.getZ());

        return switch (action) {
            case "find" -> "找到 " + desc;

            case "teleport_to_agent" -> {
                target.teleport(
                        (ServerWorld) target.getWorld(),
                        ctrlPlayer.getX(), ctrlPlayer.getY(), ctrlPlayer.getZ(),
                        null, target.getYaw(), target.getPitch());
                yield "已将 " + target.getName().getString() + " 传送到玩家位置";
            }

            case "move_to_pos" -> {
                double tx = getDouble(params, "x", ctrlPlayer.getX());
                double ty = getDouble(params, "y", ctrlPlayer.getY());
                double tz = getDouble(params, "z", ctrlPlayer.getZ());
                if (target instanceof PassiveEntity mob) {
                    mob.getNavigation().startMovingTo(tx, ty, tz, 1.0);
                    yield String.format("引导 %s 向 (%.0f,%.0f,%.0f) 移动", target.getName().getString(), tx, ty, tz);
                } else {
                    target.teleport((ServerWorld) target.getWorld(), tx, ty, tz,
                            null, target.getYaw(), target.getPitch());
                    yield String.format("已将 %s 传送到 (%.0f,%.0f,%.0f)", target.getName().getString(), tx, ty, tz);
                }
            }

            default -> "未知互动动作: " + action + "（已找到 " + desc + "）";
        };
    }

    /** get_inventory：查询玩家完整背包并格式化为文字 */
    private String executeGetInventory() {
        ServerPlayerEntity player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "没有在线玩家";

        JsonArray inv = buildFullInventory(player);
        if (inv.isEmpty()) return "背包为空";

        StringBuilder sb = new StringBuilder("背包物品（XP等级: ")
                .append(player.experienceLevel).append("）：");
        inv.forEach(e -> {
            var item = e.getAsJsonObject();
            sb.append(item.get("item").getAsString())
              .append(" x").append(item.get("count").getAsInt()).append("  ");
        });
        return sb.toString().trim();
    }

    // ── 资源探索（核心新功能）────────────────────────────────────────────────

    /**
     * find_resource：在指定半径内搜索特定类型的方块或实体，返回坐标列表。
     *
     * 与 game_state 中自动上报的 nearby_resources 不同：
     * 该行动由 Agent 主动触发，可以指定更精确的目标和更大的搜索半径。
     *
     * params:
     *   type        (String) - 要查找的资源（支持游戏名称别名，见下方映射表）
     *                          例如："diamond"、"oak_log"、"sheep"、"crafting_table"
     *   radius      (int)    - 搜索半径（默认 24，最大 48 格）
     *   max_results (int)    - 最多返回几个坐标（默认 5）
     *
     * 内置别名支持（无需填完整 minecraft: 前缀）：
     *   "diamond" → diamond_ore + deepslate_diamond_ore
     *   "iron"    → iron_ore + deepslate_iron_ore
     *   "tree" / "log" → 所有原木类型
     *   "water"、"lava"、"sand" 等直接支持
     *
     * 同时也查找实体（如 "sheep"、"cow"、"villager"）。
     * 返回示例：
     *   "找到 3 处 [diamond]：(12,-58,-23) [minecraft:deepslate_diamond_ore]  (-5,-61,8) ..."
     */
    private String executeFindResource(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";

        ServerWorld world = (ServerWorld) player.getWorld();
        String resourceType = getString(params, "type", null);
        int radius      = Math.min(Math.max(getInt(params, "radius", 24), 1), 48);
        int maxResults  = Math.min(Math.max(getInt(params, "max_results", 5), 1), 20);

        if (resourceType == null) return "缺少 type 参数（例如：{\"type\": \"diamond\", \"radius\": 32}）";

        BlockPos centerPos = player.getBlockPos();

        // ① 先尝试作为实体类型查找
        Box entityBox = player.getBoundingBox().expand(radius);
        String finalResourceType = resourceType;
        List<Entity> entities = world.getOtherEntities(player, entityBox, e ->
                e.getType().toString().toLowerCase().contains(finalResourceType.toLowerCase()) ||
                e.getName().getString().toLowerCase().contains(finalResourceType.toLowerCase()));

        if (!entities.isEmpty()) {
            StringBuilder sb = new StringBuilder(
                    "找到 " + entities.size() + " 个 [" + resourceType + "]（实体）：");
            entities.stream()
                    .sorted(Comparator.comparingDouble(e -> e.squaredDistanceTo(player)))
                    .limit(maxResults)
                    .forEach(e -> sb.append(String.format(
                            " (%d,%d,%d)[%s]",
                            Math.round(e.getX()), Math.round(e.getY()), Math.round(e.getZ()),
                            e.getType().toString())));
            return sb.toString();
        }

        // ② 作为方块类型查找
        List<String> blockIds = resolveResourceToBlockIds(resourceType);

        List<BlockPos> found = new ArrayList<>();
        Map<BlockPos, String> blockIdMap = new HashMap<>();  // 记录每个坐标对应的方块 ID
        BlockPos.Mutable mutable = new BlockPos.Mutable();

        // 从近到远按层扫描，确保找到最近的目标
        outerLoop:
        for (int r = 1; r <= radius; r++) {
            for (int dx = -r; dx <= r; dx++) {
                for (int dz = -r; dz <= r; dz++) {
                    if (Math.abs(dx) < r && Math.abs(dz) < r) continue; // 只扫外边缘
                    for (int dy = -r; dy <= r; dy++) {
                        mutable.set(centerPos.getX() + dx,
                                    centerPos.getY() + dy,
                                    centerPos.getZ() + dz);

                        if (!world.isChunkLoaded(mutable)) continue;

                        var blockState = world.getBlockState(mutable);
                        if (blockState.isAir()) continue;

                        String bid = Registries.BLOCK.getId(blockState.getBlock()).toString();
                        boolean matches = blockIds.stream().anyMatch(id ->
                                bid.equals(id) || (id.startsWith("*") && bid.contains(id.substring(1))));

                        if (matches) {
                            BlockPos immutable = mutable.toImmutable();
                            found.add(immutable);
                            blockIdMap.put(immutable, bid);
                            if (found.size() >= maxResults * 3) break outerLoop;
                        }
                    }
                }
            }
        }

        if (found.isEmpty()) {
            return String.format(
                    "在 %d 格范围内未找到 [%s]。建议：扩大 radius、移动到其他区域，" +
                    "或参考 game_state.environment.depth_context 确认所在层级是否正确",
                    radius, resourceType);
        }

        // 排序并格式化输出
        found.sort(Comparator.comparingInt(p -> (int) centerPos.getSquaredDistance(p)));

        StringBuilder sb = new StringBuilder("找到 " + found.size() + " 处 [" + resourceType + "]：");
        found.stream().limit(maxResults).forEach(p ->
                sb.append(String.format(" (%d,%d,%d)[%s]",
                        p.getX(), p.getY(), p.getZ(), blockIdMap.get(p))));
        sb.append(" ← 按距离由近到远排列，move_to 第一个坐标即可到达最近处");
        return sb.toString();
    }

    /**
     * scan_area：全面扫描当前区域，返回各类资源的概览统计和代表坐标。
     * 用于 Agent 在不确定周围有什么时的探索行动，类似"环顾四周"。
     *
     * params:
     *   radius (int) - 扫描半径（默认 20，最大 32 格）
     *
     * 返回各类别（矿石/树木/水源/设施等）的数量 + 最近坐标。
     */
    private String executeScanArea(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";

        ServerWorld world = (ServerWorld) player.getWorld();
        int radius = Math.min(Math.max(getInt(params, "radius", 20), 4), 32);
        BlockPos center = player.getBlockPos();

        // 统计各类别方块数量和最近坐标
        Map<String, Integer> counts = new HashMap<>();
        Map<String, BlockPos> nearest = new HashMap<>();
        Map<String, String> nearestId = new HashMap<>();

        BlockPos.Mutable mutable = new BlockPos.Mutable();
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dz = -radius; dz <= radius; dz++) {
                for (int dy = -radius; dy <= radius; dy++) {
                    mutable.set(center.getX() + dx, center.getY() + dy, center.getZ() + dz);
                    if (!world.isChunkLoaded(mutable)) continue;

                    var blockState = world.getBlockState(mutable);
                    if (blockState.isAir()) continue;

                    String bid = Registries.BLOCK.getId(blockState.getBlock()).toString();
                    String cat = classifyBlockForScan(bid);
                    if (cat == null) continue;

                    counts.merge(cat, 1, Integer::sum);
                    BlockPos current = mutable.toImmutable();
                    if (!nearest.containsKey(cat) ||
                            center.getSquaredDistance(current) < center.getSquaredDistance(nearest.get(cat))) {
                        nearest.put(cat, current);
                        nearestId.put(cat, bid);
                    }
                }
            }
        }

        // 同时扫描实体
        Box entityBox = player.getBoundingBox().expand(radius);
        Map<String, Integer> entityCounts = new HashMap<>();
        Map<String, BlockPos> entityNearest = new HashMap<>();
        for (Entity e : world.getOtherEntities(player, entityBox)) {
            String typeName = e.getType().toString();
            entityCounts.merge(typeName, 1, Integer::sum);
            BlockPos ep = e.getBlockPos();
            if (!entityNearest.containsKey(typeName) ||
                    center.getSquaredDistance(ep) < center.getSquaredDistance(entityNearest.get(typeName))) {
                entityNearest.put(typeName, ep);
            }
        }

        if (counts.isEmpty() && entityCounts.isEmpty()) {
            return String.format("在 %d 格范围内未发现可用资源（可能在虚空或全空气区域）", radius);
        }

        StringBuilder sb = new StringBuilder(
                String.format("扫描完成（半径%d格，以玩家为中心）：\n", radius));

        if (!counts.isEmpty()) {
            sb.append("【方块资源】");
            counts.forEach((cat, cnt) -> {
                BlockPos np = nearest.get(cat);
                sb.append(String.format("\n  %s: %d个，最近坐标(%d,%d,%d)[%s]",
                        cat, cnt, np.getX(), np.getY(), np.getZ(), nearestId.get(cat)));
            });
        }

        if (!entityCounts.isEmpty()) {
            sb.append("\n【实体/生物】");
            entityCounts.forEach((type, cnt) -> {
                BlockPos np = entityNearest.get(type);
                sb.append(String.format("\n  %s: %d个，最近(%d,%d,%d)", type, cnt,
                        np.getX(), np.getY(), np.getZ()));
            });
        }

        return sb.toString();
    }

    /**
     * look_around：远望地平线，返回 8 方向 × 多距离 的生物群系和地形摘要。
     *
     * 这是 Agent 的"环顾四周"能力——不需要靠近就能知道远方是森林还是沙漠。
     * 底层使用 WorldScanner.generateSummary()，只扫描已加载区块（不触发区块生成）。
     *
     * params:
     *   radius (int) - 最大扫描半径（默认 192 格，最大 256 格）
     *
     * 返回示例：
     *   "=== 远望扫描（Agent位置 x=50 z=30，最大192格）===
     *    正北：48格: 橡木森林（大量橡木，砍木首选）[地表y=68, 草地]
     *          96格: 橡木森林（大量橡木，砍木首选）[地表y=72, 草地]
     *         192格: 石峰（大量裸石，煤/铁矿地表可见！）[地表y=118, 高地/山地]
     *    正东：48格: 平原（村庄常见/羊牛猪马大量生成）[地表y=64, 草地]
     *    ...
     *    【导航建议】
     *    木材/树木 -> 正北48格 (x=50,z=-18) — 橡木森林
     *    裸露矿石  -> 正北192格 (x=50,z=-162) — 石峰
     *    ..."
     */
    private String executeLookAround(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";

        int radius = Math.min(Math.max(getInt(params, "radius", 192), 32), 256);
        ServerWorld world = (ServerWorld) player.getWorld();

        return WorldScanner.generateSummary(world, player.getBlockPos(), radius);
    }

    /** scan_area 用的方块分类（比 ServerEventHandler 的分类更细致） */
    private static String classifyBlockForScan(String blockId) {
        if (blockId.contains("diamond_ore"))   return "钻石矿";
        if (blockId.contains("iron_ore"))      return "铁矿";
        if (blockId.contains("gold_ore"))      return "金矿";
        if (blockId.contains("coal_ore"))      return "煤矿";
        if (blockId.contains("copper_ore"))    return "铜矿";
        if (blockId.contains("lapis_ore"))     return "青金石矿";
        if (blockId.contains("redstone_ore"))  return "红石矿";
        if (blockId.contains("emerald_ore"))   return "绿宝石矿";
        if (blockId.endsWith("_log") || blockId.endsWith("_stem")) return "原木";
        if (blockId.equals("minecraft:water")) return "水源";
        if (blockId.equals("minecraft:lava"))  return "熔岩";
        if (blockId.equals("minecraft:gravel")) return "沙砾";
        if (blockId.equals("minecraft:sand") || blockId.equals("minecraft:red_sand")) return "沙子";
        if (blockId.equals("minecraft:crafting_table")) return "合成台";
        if (blockId.equals("minecraft:furnace") || blockId.equals("minecraft:blast_furnace")) return "熔炉";
        if (blockId.equals("minecraft:chest") || blockId.equals("minecraft:barrel")) return "箱子";
        if (blockId.equals("minecraft:enchanting_table")) return "附魔台";
        if (blockId.equals("minecraft:anvil") ||
            blockId.equals("minecraft:chipped_anvil") ||
            blockId.equals("minecraft:damaged_anvil")) return "铁砧";
        if (blockId.equals("minecraft:grass_block")) return "草方块";
        if (blockId.equals("minecraft:farmland"))    return "耕地";
        return null;
    }

    /**
     * 将用户输入的资源名称解析为对应的 Minecraft 方块 ID 列表。
     * 支持别名和变体（普通矿石 + 深板岩矿石）。
     */
    private static List<String> resolveResourceToBlockIds(String resourceName) {
        String lower = resourceName.toLowerCase().replace(" ", "_").replace("minecraft:", "");
        return switch (lower) {
            case "diamond", "diamond_ore" ->
                    List.of("minecraft:diamond_ore", "minecraft:deepslate_diamond_ore");
            case "iron", "iron_ore" ->
                    List.of("minecraft:iron_ore", "minecraft:deepslate_iron_ore");
            case "gold", "gold_ore" ->
                    List.of("minecraft:gold_ore", "minecraft:deepslate_gold_ore",
                            "minecraft:nether_gold_ore");
            case "coal", "coal_ore" ->
                    List.of("minecraft:coal_ore", "minecraft:deepslate_coal_ore");
            case "copper", "copper_ore" ->
                    List.of("minecraft:copper_ore", "minecraft:deepslate_copper_ore");
            case "emerald", "emerald_ore" ->
                    List.of("minecraft:emerald_ore", "minecraft:deepslate_emerald_ore");
            case "redstone", "redstone_ore" ->
                    List.of("minecraft:redstone_ore", "minecraft:deepslate_redstone_ore");
            case "lapis", "lapis_ore" ->
                    List.of("minecraft:lapis_ore", "minecraft:deepslate_lapis_ore");
            case "tree", "log", "wood", "timber" ->
                    List.of("minecraft:oak_log", "minecraft:birch_log", "minecraft:spruce_log",
                            "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log",
                            "minecraft:cherry_log", "minecraft:mangrove_log");
            case "oak_tree", "oak_log" -> List.of("minecraft:oak_log");
            case "birch_tree", "birch_log" -> List.of("minecraft:birch_log");
            case "spruce_tree", "spruce_log" -> List.of("minecraft:spruce_log");
            case "jungle_tree", "jungle_log" -> List.of("minecraft:jungle_log");
            case "crafting_table" -> List.of("minecraft:crafting_table");
            case "furnace" -> List.of("minecraft:furnace", "minecraft:blast_furnace");
            case "chest" -> List.of("minecraft:chest", "minecraft:trapped_chest");
            case "barrel" -> List.of("minecraft:barrel");
            case "enchanting_table" -> List.of("minecraft:enchanting_table");
            case "anvil" -> List.of("minecraft:anvil", "minecraft:chipped_anvil",
                    "minecraft:damaged_anvil");
            case "gravel" -> List.of("minecraft:gravel");
            case "sand" -> List.of("minecraft:sand", "minecraft:red_sand");
            case "water" -> List.of("minecraft:water");
            case "lava" -> List.of("minecraft:lava");
            case "grass" -> List.of("minecraft:grass_block");
            case "dirt" -> List.of("minecraft:dirt");
            case "stone" -> List.of("minecraft:stone", "minecraft:deepslate", "minecraft:cobblestone");
            default -> List.of("minecraft:" + lower);  // 直接尝试用原始名称
        };
    }

    private String executeFollowPlayer(JsonObject params) {
        var ctrlPlayer = PlayerContext.getCurrentOrFirst(server);
        if (ctrlPlayer == null) return "无可用玩家";

        ServerPlayerEntity target = findPlayer(getString(params, "player", null));
        if (target == null || target == ctrlPlayer) return "当前没有可跟随的玩家";

        // 操控主角：使用 move_to 走向目标玩家
        taskRunner.runMoveTo(ctrlPlayer, target.getX(), target.getY(), target.getZ());
        return "开始向玩家 " + target.getName().getString() + " 移动";
    }

    private String executeStop() {
        taskRunner.cancelCurrent();
        return "已停止当前行动";
    }

    private String executeLookAt(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";

        String playerName = getString(params, "player", null);
        if (playerName != null) {
            ServerPlayerEntity target = findPlayer(playerName);
            if (target != null) {
                double dx = target.getX() - player.getX();
                double dz = target.getZ() - player.getZ();
                float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
                player.setYaw(yaw);
                player.setHeadYaw(yaw);
                return "已转向玩家: " + playerName;
            }
        }

        double x = getDouble(params, "x", player.getX() + 1);
        double y = getDouble(params, "y", player.getEyeY());
        double z = getDouble(params, "z", player.getZ());
        double dx = x - player.getX();
        double dy = y - (player.getY() + player.getStandingEyeHeight());
        double dz = z - player.getZ();
        float yaw = (float) (Math.atan2(-dx, dz) * 180 / Math.PI);
        float pitch = (float) (-Math.atan2(dy, Math.sqrt(dx*dx+dz*dz)) * 180 / Math.PI);
        player.setYaw(yaw);
        player.setHeadYaw(yaw);
        player.setPitch(pitch);
        return String.format("已转向 (%.0f,%.0f,%.0f)", x, y, z);
    }

    /**
     * turn：按角度偏移转向（精细移动用，遇障时可先 turn 再 move_to）。
     * params: yaw_delta (double) - 角度偏移，正为右转，负为左转（度）
     */
    private String executeTurn(JsonObject params) {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";
        double delta = getDouble(params, "yaw_delta", 0);
        float newYaw = (float) (player.getYaw() + delta);
        while (newYaw > 180f) newYaw -= 360f;
        while (newYaw < -180f) newYaw += 360f;
        player.setYaw(newYaw);
        player.setHeadYaw(newYaw);
        return String.format("已转向 %.0f°（当前朝向 %.0f°）", delta, newYaw);
    }

    /**
     * jump：原地跳跃一次（用于上台阶、越过矮障碍）。
     */
    private String executeJump() {
        var player = PlayerContext.getCurrentOrFirst(server);
        if (player == null) return "无可用玩家";
        var v = player.getVelocity();
        player.setVelocity(v.x, 0.42, v.z);
        return "已跳跃";
    }

    // ── 背包工具方法 ──────────────────────────────────────────────────────────

    private ItemStack getItemFromSlot(ServerPlayerEntity player, String slotStr) {
        return switch (slotStr) {
            case "mainhand" -> player.getMainHandStack();
            case "offhand"  -> player.getOffHandStack();
            default -> {
                try { yield player.getInventory().getStack(Integer.parseInt(slotStr)); }
                catch (NumberFormatException e) { yield ItemStack.EMPTY; }
            }
        };
    }

    private int countItem(ServerPlayerEntity player, Item item) {
        int count = 0;
        for (int i = 0; i < player.getInventory().size(); i++) {
            ItemStack s = player.getInventory().getStack(i);
            if (s.isOf(item)) count += s.getCount();
        }
        return count;
    }

    private void removeItem(ServerPlayerEntity player, Item item, int amount) {
        int remaining = amount;
        for (int i = 0; i < player.getInventory().size() && remaining > 0; i++) {
            ItemStack s = player.getInventory().getStack(i);
            if (s.isOf(item)) {
                int remove = Math.min(s.getCount(), remaining);
                s.decrement(remove);
                remaining -= remove;
            }
        }
        player.getInventory().markDirty();
    }

    // ── 通用工具 ──────────────────────────────────────────────────────────────

    private ServerPlayerEntity findPlayer(String name) {
        if (name == null) {
            var list = server.getPlayerManager().getPlayerList();
            return list.isEmpty() ? null : list.get(0);
        }
        return server.getPlayerManager().getPlayer(name);
    }

    /**
     * 解析 move_to 目标：支持方向+距离、区域中心+半径、或直接 (x,y,z)。
     * 方向+距离：direction (north/south/east/west/...) 或 direction_deg (0=北,90=东) + distance。
     * 区域：region_center ([x,y,z] 或 {x,y,z}) + radius，取中心作为目标。
     */
    private static BlockPos resolveMoveToTarget(ServerWorld world, ServerPlayerEntity player, JsonObject params) {
        if (params == null) return null;
        BlockPos from = player.getBlockPos();

        // 方向 + 距离
        if (params.has("distance")) {
            int distance = getInt(params, "distance", 48);
            int dx = 0, dz = 0;
            if (params.has("direction_deg")) {
                double deg = getDouble(params, "direction_deg", 0);
                double rad = Math.toRadians(deg);
                dx = (int) Math.round(Math.sin(rad));
                dz = (int) -Math.round(Math.cos(rad));
            } else if (params.has("direction")) {
                String dir = getString(params, "direction", "north").toLowerCase();
                switch (dir) {
                    case "north"     -> { dx =  0; dz = -1; }
                    case "south"     -> { dx =  0; dz =  1; }
                    case "east"     -> { dx =  1; dz =  0; }
                    case "west"     -> { dx = -1; dz =  0; }
                    case "northeast" -> { dx =  1; dz = -1; }
                    case "northwest" -> { dx = -1; dz = -1; }
                    case "southeast" -> { dx =  1; dz =  1; }
                    case "southwest" -> { dx = -1; dz =  1; }
                    default -> { dx = 0; dz = -1; }
                }
            } else {
                return null;
            }
            if (dx == 0 && dz == 0) return null;
            int tx = from.getX() + dx * distance;
            int tz = from.getZ() + dz * distance;
            int ty = world.getTopY(Heightmap.Type.WORLD_SURFACE, tx, tz);
            return new BlockPos(tx, ty, tz);
        }

        // 区域中心 + 半径
        if (params.has("region_center") && params.has("radius")) {
            int cx, cy, cz;
            if (params.get("region_center").isJsonArray()) {
                JsonArray arr = params.getAsJsonArray("region_center");
                cx = arr.size() > 0 ? arr.get(0).getAsInt() : from.getX();
                cy = arr.size() > 1 ? arr.get(1).getAsInt() : from.getY();
                cz = arr.size() > 2 ? arr.get(2).getAsInt() : from.getZ();
            } else if (params.get("region_center").isJsonObject()) {
                JsonObject obj = params.getAsJsonObject("region_center");
                cx = getInt(obj, "x", from.getX());
                cy = getInt(obj, "y", from.getY());
                cz = getInt(obj, "z", from.getZ());
            } else {
                return null;
            }
            int r = Math.max(0, getInt(params, "radius", 2));
            int ty = world.getTopY(Heightmap.Type.WORLD_SURFACE, cx, cz);
            return new BlockPos(cx, ty, cz);
        }

        return null;
    }

    private static double getDouble(JsonObject o, String k, double def) {
        return (o != null && o.has(k)) ? o.get(k).getAsDouble() : def;
    }

    private static int getInt(JsonObject o, String k, int def) {
        return (o != null && o.has(k)) ? o.get(k).getAsInt() : def;
    }

    private static String getString(JsonObject o, String k, String def) {
        return (o != null && o.has(k) && !o.get(k).isJsonNull())
                ? o.get(k).getAsString() : def;
    }
}
