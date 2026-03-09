/**
 * 控制原语（Control Primitives）
 *
 * 参照 Voyager control_primitives，注入到 bot 对象上，
 * 提供高级、带错误处理的操作封装。
 *
 * 注入后可用：
 *   bot.mineBlock(name, count)
 *   bot.craftItem(name, count)
 *   bot.smeltItem(name, fuel, count)
 *   bot.findNearbyBlocks(name, radius, maxCount)
 *   bot.scanNearby(radius)
 *   bot.exploreUntil(direction, maxTime, callback)
 *   bot.inventoryUsed()
 *   bot.placeBlock(pos, blockName)    ← 覆盖原版，先走到再放
 */

"use strict";

const { goals: { GoalNear, GoalNearXZ, GoalGetToBlock, GoalLookAtBlock } } = require("mineflayer-pathfinder");
const Vec3 = require("vec3").Vec3;

function inject(bot, mcData) {

    // ── inventoryUsed ────────────────────────────────────────────────────────
    bot.inventoryUsed = () => {
        return bot.inventory.slots.slice(9, 45).filter(i => i !== null).length;
    };

    // ── mineBlock ────────────────────────────────────────────────────────────
    /**
     * 找到并挖掘最近的 count 个指定方块。
     * 会自动寻路靠近每个方块。
     */
    bot.mineBlock = async (name, count = 1) => {
        const blockDef = mcData.blocksByName[name];
        if (!blockDef) throw new Error(`未知方块: ${name}`);

        const blocks = bot.findBlocks({
            matching: [blockDef.id],
            maxDistance: 32,
            count: count * 3,  // 多找一些以防有的到不了
        });

        if (blocks.length === 0) {
            bot.chat(`附近没有 ${name}，需要先探索`);
            return;
        }

        const targets = blocks.slice(0, count).map(p => bot.blockAt(p)).filter(Boolean);
        await bot.collectBlock.collect(targets, { ignoreNoPath: true, count });
        bot.chat(`已挖掘 ${Math.min(targets.length, count)} 个 ${name}`);
    };

    // ── craftItem ────────────────────────────────────────────────────────────
    /**
     * 合成指定物品。
     * 会自动寻找附近工作台（如果配方需要的话）。
     */
    bot.craftItem = async (name, count = 1) => {
        const item = mcData.itemsByName[name];
        if (!item) throw new Error(`未知物品: ${name}`);

        // 找工作台
        const tableBlock = bot.findBlock({
            matching: mcData.blocksByName.crafting_table.id,
            maxDistance: 32,
        });

        if (tableBlock) {
            await bot.pathfinder.goto(new GoalLookAtBlock(tableBlock.position, bot.world));
        }

        const recipe = bot.recipesFor(item.id, null, 1, tableBlock)[0];
        if (!recipe) throw new Error(`没有 ${name} 的合成配方（检查材料是否足够）`);

        await bot.craft(recipe, count, tableBlock);
        bot.chat(`合成了 ${count} 个 ${name}`);
    };

    // ── smeltItem ────────────────────────────────────────────────────────────
    /**
     * 在附近熔炉中熔炼物品。
     */
    bot.smeltItem = async (itemName, fuelName = "coal", count = 1) => {
        const item = mcData.itemsByName[itemName];
        const fuel = mcData.itemsByName[fuelName];
        if (!item) throw new Error(`未知物品: ${itemName}`);
        if (!fuel) throw new Error(`未知燃料: ${fuelName}`);

        const furnaceBlock = bot.findBlock({
            matching: mcData.blocksByName.furnace.id,
            maxDistance: 32,
        });
        if (!furnaceBlock) throw new Error("附近没有熔炉，请先放置一个");

        await bot.pathfinder.goto(new GoalLookAtBlock(furnaceBlock.position, bot.world));
        const furnace = await bot.openFurnace(furnaceBlock);

        let smelted = 0;
        for (let i = 0; i < count; i++) {
            if (!bot.inventory.findInventoryItem(item.id, null)) {
                bot.chat(`背包中没有 ${itemName} 了`);
                break;
            }
            // 补充燃料
            if (furnace.fuelSeconds < 15) {
                if (!bot.inventory.findInventoryItem(fuel.id, null)) {
                    throw new Error(`背包中没有燃料 ${fuelName}`);
                }
                await furnace.putFuel(fuel.id, null, 1);
                await bot.waitForTicks(20);
            }
            await furnace.putInput(item.id, null, 1);
            await bot.waitForTicks(12 * 20);  // 等待熔炼（约12秒）
            if (furnace.outputItem()) {
                await furnace.takeOutput();
                smelted++;
            }
        }
        furnace.close();
        bot.chat(`熔炼了 ${smelted} 个 ${itemName}`);
    };

    // ── findNearbyBlocks ──────────────────────────────────────────────────────
    /**
     * 找到附近的指定方块，返回坐标列表（按距离排序）。
     */
    bot.findNearbyBlocks = (name, radius = 32, maxCount = 10) => {
        const blockName = name.replace("minecraft:", "");
        const blockDef = mcData.blocksByName[blockName];
        if (!blockDef) return [];

        const blocks = bot.findBlocks({
            matching: blockDef.id,
            maxDistance: radius,
            count: maxCount,
        });

        return blocks.map(p => ({
            x: p.x, y: p.y, z: p.z,
            distance: parseFloat(p.distanceTo(bot.entity.position).toFixed(1)),
        })).sort((a, b) => a.distance - b.distance);
    };

    // ── scanNearby ────────────────────────────────────────────────────────────
    /**
     * 扫描附近环境，返回可读的文字摘要。
     */
    bot.scanNearby = (radius = 24) => {
        const pos = bot.entity.position;
        const found = new Map();

        const IMPORTANT = [
            "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log",
            "diamond_ore", "iron_ore", "coal_ore", "gold_ore", "deepslate_diamond_ore",
            "deepslate_iron_ore", "water", "lava", "chest", "crafting_table", "furnace",
            "iron_block", "diamond_block", "sand", "gravel",
        ];

        for (const name of IMPORTANT) {
            const def = mcData.blocksByName[name];
            if (!def) continue;
            const blocks = bot.findBlocks({ matching: def.id, maxDistance: radius, count: 3 });
            if (blocks.length > 0) {
                const nearest = blocks[0];
                found.set(name, {
                    count: blocks.length,
                    nearest: `(${nearest.x},${nearest.y},${nearest.z})`,
                    dist: parseFloat(nearest.distanceTo(pos).toFixed(1)),
                });
            }
        }

        if (found.size === 0) return `${radius}格内无特殊资源`;
        const parts = [];
        for (const [name, info] of found) {
            parts.push(`${name} x${info.count}（最近=${info.nearest}，距离=${info.dist}格）`);
        }
        return parts.join("；");
    };

    // ── exploreUntil ──────────────────────────────────────────────────────────
    /**
     * 参照 Voyager exploreUntil。
     * 朝指定方向探索，直到 callback 返回非 null 或超时。
     */
    bot.exploreUntil = (direction, maxTime = 60, callback = () => null) => {
        // 快速检查
        const test = callback();
        if (test) return Promise.resolve(test);

        return new Promise((resolve) => {
            let interval, timeout;

            const cleanup = () => {
                clearInterval(interval);
                clearTimeout(timeout);
                bot.pathfinder.setGoal(null);
            };

            const explore = () => {
                const rand = () => Math.floor(Math.random() * 30 + 15);
                const target = bot.entity.position.plus(direction.scaled(rand()));
                const goal = direction.y === 0
                    ? new GoalNearXZ(target.x, target.z)
                    : new GoalNear(target.x, target.y, target.z, 5);
                bot.pathfinder.setGoal(goal, true);

                try {
                    const result = callback();
                    if (result) { cleanup(); bot.chat("探索成功！"); resolve(result); }
                } catch (_) { cleanup(); resolve(null); }
            };

            interval = setInterval(explore, 2000);
            timeout = setTimeout(() => {
                cleanup();
                bot.chat("探索超时");
                resolve(null);
            }, maxTime * 1000);
        });
    };

    // ── placeBlock (增强版) ───────────────────────────────────────────────────
    bot._placeBlock = bot.placeBlock;
    bot.placeBlock = async (targetPos, blockName) => {
        const item = bot.inventory.items().find(i => i.name === blockName);
        if (!item) throw new Error(`背包中没有 ${blockName}`);

        await bot.equip(item, "hand");
        const faceBlock = bot.blockAt(targetPos.offset(0, -1, 0));
        if (!faceBlock) throw new Error("无法找到放置面");

        await bot.pathfinder.goto(new GoalNear(targetPos.x, targetPos.y, targetPos.z, 3));
        await bot._placeBlock(faceBlock, new Vec3(0, 1, 0));
    };

    console.log("[Primitives] 控制原语注入完毕");
}

module.exports = { inject };
