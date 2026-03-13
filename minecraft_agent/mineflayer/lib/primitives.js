/**
 * 控制原语 v3（Voyager 对等版）
 *
 * 参照 Voyager control_primitives_context，注入到 bot 对象上。
 * LLM 生成的 JS 函数直接调用这些原语，无需知道底层 mineflayer API。
 *
 * 注入后可用（与 Voyager 原版保持一致的命名）：
 *   await mineBlock(bot, name, count)
 *   await craftItem(bot, name, count)
 *   await smeltItem(bot, name, fuel, count)
 *   await placeItem(bot, name, position)
 *   await killMob(bot, name, timeout)
 *   await pickupNearbyItems(bot)
 *   await moveToPosition(bot, x, y, z, minDistance)
 *   await exploreUntil(bot, direction, maxDistance, callback)
 *   await equipItem(bot, name, destination)
 *   await eatFood(bot)
 *   await sleep(bot, bedPosition)
 *   await activateNearestBlock(bot, name)
 *   await interactWithEntity(bot, name, action)
 *   await waitForMobRemoved(bot, mobName, radius, timeout)
 *   await depositToChest(bot, chestPosition, itemsToDeposit)
 *   await withdrawFromChest(bot, chestPosition, itemsToWithdraw)
 *   bot.inventoryUsed()
 *   bot.findNearbyBlocks(name, radius, maxCount)
 *   bot.scanNearby(radius)
 */

"use strict";

const { goals: {
    GoalNear, GoalNearXZ, GoalGetToBlock, GoalLookAtBlock, GoalFollow
}} = require("mineflayer-pathfinder");
const Vec3 = require("vec3").Vec3;

function inject(bot, mcData) {

    // ─────────────────────────────────────────────────────────────────────────
    // 工具方法
    // ─────────────────────────────────────────────────────────────────────────

    bot.inventoryUsed = () =>
        bot.inventory.slots.slice(9, 45).filter(i => i !== null).length;

    bot.findNearbyBlocks = (name, radius = 32, maxCount = 10) => {
        const cleanName = name.replace("minecraft:", "");
        const blockDef = mcData.blocksByName[cleanName];
        if (!blockDef) return [];
        const positions = bot.findBlocks({ matching: [blockDef.id], maxDistance: radius, count: maxCount });
        return positions.map(p => ({ x: p.x, y: p.y, z: p.z }));
    };

    bot.scanNearby = (radius = 24) => {
        const counts = {};
        const pos = bot.entity.position;
        for (let dx = -radius; dx <= radius; dx += 2)
        for (let dy = -8;      dy <= 8;      dy += 2)
        for (let dz = -radius; dz <= radius; dz += 2) {
            const b = bot.blockAt(pos.offset(dx, dy, dz));
            if (b && b.name !== "air" && b.name !== "cave_air")
                counts[b.name] = (counts[b.name] || 0) + 1;
        }
        return counts;
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ mineBlock — 寻路 + 挖掘 N 个指定方块
    // ─────────────────────────────────────────────────────────────────────────
    bot.mineBlock = async (name, count = 1) => {
        const cleanName = name.replace("minecraft:", "");
        const blockDef = mcData.blocksByName[cleanName];
        if (!blockDef) throw new Error(`未知方块: ${cleanName}`);

        let mined = 0;
        while (mined < count) {
            const positions = bot.findBlocks({
                matching: [blockDef.id],
                maxDistance: 32,
                count: (count - mined) * 2 + 2,
            });
            if (positions.length === 0) {
                bot.chat(`附近没有更多 ${cleanName}`);
                break;
            }
            const targets = positions
                .slice(0, count - mined)
                .map(p => bot.blockAt(p))
                .filter(Boolean);
            try {
                await bot.collectBlock.collect(targets, { ignoreNoPath: true, count: count - mined });
                mined += targets.length;
            } catch (err) {
                bot.chat(`挖掘 ${cleanName} 时出错: ${err.message}`);
                break;
            }
        }
        bot.chat(`已挖掘 ${mined} 个 ${cleanName}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ craftItem — 自动寻找工作台合成
    // ─────────────────────────────────────────────────────────────────────────
    bot.craftItem = async (name, count = 1) => {
        const cleanName = name.replace("minecraft:", "");
        const item = mcData.itemsByName[cleanName];
        if (!item) throw new Error(`未知物品: ${cleanName}`);

        // 先尝试不用工作台（2x2）
        let recipes = bot.recipesFor(item.id, null, 1, null);
        let table = null;

        if (recipes.length === 0) {
            // 需要工作台（3x3）
            const tablePositions = bot.findBlocks({
                matching: [mcData.blocksByName["crafting_table"].id],
                maxDistance: 32, count: 1,
            });
            if (tablePositions.length === 0) {
                // 没有工作台，尝试先放置一个
                const tableItem = bot.inventory.items().find(i => i.name === "crafting_table");
                if (tableItem) {
                    const placePos = bot.entity.position.offset(1, 0, 0).floored();
                    await bot.pathfinder.goto(new GoalNear(placePos.x, placePos.y, placePos.z, 2));
                    await bot.equip(tableItem, "hand");
                    const refBlock = bot.blockAt(placePos.offset(0, -1, 0));
                    if (refBlock) await bot._placeBlock(refBlock, new Vec3(0, 1, 0));
                    await bot.waitForTicks(5);
                } else {
                    throw new Error(`合成 ${cleanName} 需要工作台，但附近没有且背包中也没有`);
                }
            }
            const tablePos = bot.findBlocks({ matching: [mcData.blocksByName["crafting_table"].id], maxDistance: 32, count: 1 });
            if (tablePos.length > 0) {
                table = bot.blockAt(tablePos[0]);
                await bot.pathfinder.goto(new GoalGetToBlock(table.position.x, table.position.y, table.position.z));
            }
            recipes = bot.recipesFor(item.id, null, 1, table);
        }

        if (recipes.length === 0) throw new Error(`没有合成配方: ${cleanName}`);

        await bot.craft(recipes[0], count, table);
        bot.chat(`已合成 ${count} 个 ${cleanName}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ smeltItem — 熔炉冶炼
    // ─────────────────────────────────────────────────────────────────────────
    bot.smeltItem = async (name, fuel = "coal", count = 1) => {
        const cleanName = name.replace("minecraft:", "");
        const cleanFuel = fuel.replace("minecraft:", "");

        // 找熔炉
        const furnacePositions = bot.findBlocks({
            matching: [mcData.blocksByName["furnace"].id],
            maxDistance: 32, count: 1,
        });
        if (furnacePositions.length === 0) throw new Error("附近没有熔炉");

        const furnaceBlock = bot.blockAt(furnacePositions[0]);
        await bot.pathfinder.goto(new GoalGetToBlock(furnaceBlock.position.x, furnaceBlock.position.y, furnaceBlock.position.z));

        const furnace = await bot.openFurnace(furnaceBlock);

        // 放入原材料
        const inputItem = bot.inventory.items().find(i => i.name === cleanName);
        if (!inputItem) { furnace.close(); throw new Error(`背包中没有 ${cleanName}`); }
        await furnace.putInput(inputItem.type, null, count);

        // 放入燃料
        const fuelItem = bot.inventory.items().find(i => i.name === cleanFuel);
        if (!fuelItem) { furnace.close(); throw new Error(`背包中没有燃料 ${cleanFuel}`); }
        await furnace.putFuel(fuelItem.type, null, Math.ceil(count / 8) + 1);

        // 等待冶炼完成
        await new Promise((resolve) => {
            const check = setInterval(async () => {
                if (furnace.outputItem()) {
                    await furnace.takeOutput();
                    clearInterval(check);
                    furnace.close();
                    resolve();
                }
            }, 1000);
            setTimeout(() => { clearInterval(check); furnace.close(); resolve(); }, count * 12000 + 5000);
        });

        bot.chat(`已冶炼 ${count} 个 ${cleanName}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ placeItem — 在指定位置放置方块
    // ─────────────────────────────────────────────────────────────────────────
    bot._placeBlock = bot.placeBlock;
    bot.placeItem = async (name, position) => {
        const cleanName = name.replace("minecraft:", "");
        const item = bot.inventory.items().find(i => i.name === cleanName);
        if (!item) throw new Error(`背包中没有 ${cleanName}`);

        await bot.pathfinder.goto(new GoalNear(position.x, position.y, position.z, 3));
        await bot.equip(item, "hand");

        const refBlock = bot.blockAt(new Vec3(position.x, position.y - 1, position.z));
        if (!refBlock) throw new Error("无法找到放置面");
        await bot._placeBlock(refBlock, new Vec3(0, 1, 0));
        bot.chat(`已放置 ${cleanName} 在 (${position.x},${position.y},${position.z})`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ killMob — 寻找并击杀指定怪物
    // ─────────────────────────────────────────────────────────────────────────
    bot.killMob = async (name, timeout = 300) => {
        const cleanName = name.replace("minecraft:", "").toLowerCase();
        const startTime = Date.now();

        while (Date.now() - startTime < timeout * 1000) {
            const mob = bot.nearestEntity(e => {
                const en = (e.name || "").toLowerCase();
                return (en === cleanName || en.includes(cleanName)) &&
                       e.position.distanceTo(bot.entity.position) < 48;
            });

            if (!mob) {
                bot.chat(`附近没有 ${cleanName}，尝试探索...`);
                // 随机方向探索
                const dirs = ["north", "south", "east", "west"];
                const dir  = dirs[Math.floor(Math.random() * dirs.length)];
                await bot.exploreUntil(dir, 64, () =>
                    bot.nearestEntity(e => (e.name || "").toLowerCase().includes(cleanName))
                );
                continue;
            }

            await bot.pathfinder.goto(new GoalFollow(mob, 2));
            bot.pvp.attack(mob);

            await new Promise((resolve) => {
                const check = setInterval(() => {
                    if (!mob.isValid || mob.health <= 0) {
                        clearInterval(check);
                        bot.pvp.stop();
                        resolve();
                    }
                }, 500);
                setTimeout(() => { clearInterval(check); bot.pvp.stop(); resolve(); }, 30000);
            });

            bot.chat(`已击杀 ${cleanName}`);
            await pickupNearbyItems(bot);
            return;
        }

        throw new Error(`击杀 ${cleanName} 超时`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ equipItem — 装备物品
    // ─────────────────────────────────────────────────────────────────────────
    bot.equipItem = async (name, destination = "hand") => {
        const cleanName = (name || "").replace("minecraft:", "");
        // air = 空手：不查背包，直接卸下手中物品（mineflayer 的 unequip 接受 destination "hand"）
        if (cleanName === "air") {
            if (bot.heldItem) {
                await bot.unequip("hand");
                bot.chat("已空手");
            }
            return;
        }
        const item = bot.inventory.items().find(i => i.name === cleanName);
        if (!item) throw new Error(`背包中没有 ${cleanName}`);
        await bot.equip(item, destination);
        bot.chat(`已装备 ${cleanName} 到 ${destination}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ eatFood — 自动吃食物（按饱腹度排序）
    // ─────────────────────────────────────────────────────────────────────────
    bot.eatFood = async () => {
        const foodItems = bot.inventory.items().filter(i => mcData.itemsByName[i.name]?.foodPoints > 0);
        if (foodItems.length === 0) throw new Error("背包中没有食物");

        // 选饱腹度最高的食物
        foodItems.sort((a, b) =>
            (mcData.itemsByName[b.name]?.foodPoints || 0) - (mcData.itemsByName[a.name]?.foodPoints || 0)
        );
        const food = foodItems[0];
        await bot.equip(food, "hand");
        await bot.consume();
        bot.chat(`已吃了 ${food.name}，饥饿度 +${mcData.itemsByName[food.name]?.foodPoints || "?"}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ pickupNearbyItems — 捡起附近掉落物
    // ─────────────────────────────────────────────────────────────────────────
    bot.pickupNearbyItems = async () => {
        const items = Object.values(bot.entities).filter(
            e => e.type === "object" && e.objectType === "Item" &&
                 e.position.distanceTo(bot.entity.position) < 8
        );
        for (const item of items) {
            await bot.pathfinder.goto(new GoalNear(item.position.x, item.position.y, item.position.z, 1));
        }
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ moveToPosition — 寻路到指定坐标
    // ─────────────────────────────────────────────────────────────────────────
    bot.moveToPosition = async (x, y, z, minDistance = 2) => {
        await bot.pathfinder.goto(new GoalNear(x, y, z, minDistance));
        bot.chat(`已到达 (${Math.round(x)},${Math.round(y)},${Math.round(z)}) 附近`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ exploreUntil — 朝某方向移动，直到 callback 返回真值
    // ─────────────────────────────────────────────────────────────────────────
    bot.exploreUntil = async (direction, maxDistance = 64, callback = null) => {
        const dirMap = { north: [0,0,-1], south: [0,0,1], east: [1,0,0], west: [-1,0,0] };
        const d = dirMap[direction] || dirMap.north;

        return new Promise((resolve) => {
            let traveled = 0;
            const step = 8;
            let interval, timeout;

            const cleanup = () => { clearInterval(interval); clearTimeout(timeout); };

            const move = async () => {
                if (traveled >= maxDistance) { cleanup(); resolve(null); return; }
                const pos = bot.entity.position;
                const target = new Vec3(pos.x + d[0] * step, pos.y, pos.z + d[2] * step);
                const goal = new GoalNearXZ(target.x, target.z);
                bot.pathfinder.setGoal(goal, true);
                traveled += step;

                if (callback) {
                    try {
                        const result = callback();
                        if (result) { cleanup(); bot.chat("探索成功"); resolve(result); }
                    } catch (_) {}
                }
            };

            interval = setInterval(move, 2000);
            timeout  = setTimeout(() => { cleanup(); bot.chat("探索超时"); resolve(null); }, maxDistance * 500);
            move();
        });
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ activateNearestBlock — 右键点击最近的指定方块
    // ─────────────────────────────────────────────────────────────────────────
    bot.activateNearestBlock = async (name) => {
        const cleanName = name.replace("minecraft:", "");
        const blockDef  = mcData.blocksByName[cleanName];
        if (!blockDef) throw new Error(`未知方块: ${cleanName}`);

        const pos = bot.findBlock({ matching: blockDef.id, maxDistance: 32 });
        if (!pos) throw new Error(`附近没有 ${cleanName}`);

        const block = bot.blockAt(pos);
        await bot.pathfinder.goto(new GoalGetToBlock(block.position.x, block.position.y, block.position.z));
        await bot.activateBlock(block);
        bot.chat(`已激活 ${cleanName}`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ depositToChest / withdrawFromChest
    // ─────────────────────────────────────────────────────────────────────────
    bot.depositToChest = async (chestPosition, itemsToDeposit) => {
        // itemsToDeposit: [{name, count}]
        const chestBlock = bot.blockAt(new Vec3(chestPosition.x, chestPosition.y, chestPosition.z));
        if (!chestBlock) throw new Error("箱子不存在");

        await bot.pathfinder.goto(new GoalNear(chestPosition.x, chestPosition.y, chestPosition.z, 2));
        const chest = await bot.openChest(chestBlock);

        for (const { name, count } of itemsToDeposit) {
            const cleanName = name.replace("minecraft:", "");
            const item = bot.inventory.items().find(i => i.name === cleanName);
            if (item) {
                await chest.deposit(item.type, null, Math.min(count || item.count, item.count));
            }
        }
        chest.close();
        bot.chat(`已存入物品到箱子`);
    };

    bot.withdrawFromChest = async (chestPosition, itemsToWithdraw) => {
        const chestBlock = bot.blockAt(new Vec3(chestPosition.x, chestPosition.y, chestPosition.z));
        if (!chestBlock) throw new Error("箱子不存在");

        await bot.pathfinder.goto(new GoalNear(chestPosition.x, chestPosition.y, chestPosition.z, 2));
        const chest = await bot.openChest(chestBlock);

        for (const { name, count } of itemsToWithdraw) {
            const cleanName = name.replace("minecraft:", "");
            const item = chest.containerItems().find(i => i.name === cleanName);
            if (item) {
                await chest.withdraw(item.type, null, Math.min(count || item.count, item.count));
            }
        }
        chest.close();
        bot.chat(`已从箱子取出物品`);
    };

    // ─────────────────────────────────────────────────────────────────────────
    // ★ waitForMobRemoved — 等待附近某类怪物消失
    // ─────────────────────────────────────────────────────────────────────────
    bot.waitForMobRemoved = async (mobName, radius = 32, timeout = 60) => {
        const cleanName = mobName.replace("minecraft:", "").toLowerCase();
        return new Promise((resolve) => {
            const check = setInterval(() => {
                const found = bot.nearestEntity(e =>
                    (e.name || "").toLowerCase().includes(cleanName) &&
                    e.position.distanceTo(bot.entity.position) < radius
                );
                if (!found) { clearInterval(check); clearTimeout(t); resolve(true); }
            }, 1000);
            const t = setTimeout(() => { clearInterval(check); resolve(false); }, timeout * 1000);
        });
    };

    console.log("[Primitives] ✅ 控制原语 v3 注入完毕（Voyager 对等版）");
}

// 导出独立函数供 LLM 生成代码调用（函数式风格，兼容 Voyager 写法）
async function mineBlock(bot, name, count = 1)               { await bot.mineBlock(name, count); }
async function craftItem(bot, name, count = 1)               { await bot.craftItem(name, count); }
async function smeltItem(bot, name, fuel = "coal", count = 1){ await bot.smeltItem(name, fuel, count); }
async function placeItem(bot, name, position)                { await bot.placeItem(name, position); }
async function killMob(bot, name, timeout = 300)             { await bot.killMob(name, timeout); }
async function pickupNearbyItems(bot)                        { await bot.pickupNearbyItems(); }
async function moveToPosition(bot, x, y, z, minDist = 2)    { await bot.moveToPosition(x, y, z, minDist); }
async function exploreUntil(bot, dir, maxDist, callback)     { return await bot.exploreUntil(dir, maxDist, callback); }
async function equipItem(bot, name, dest = "hand")           { await bot.equipItem(name, dest); }
async function eatFood(bot)                                  { await bot.eatFood(); }
async function activateNearestBlock(bot, name)               { await bot.activateNearestBlock(name); }

module.exports = {
    inject,
    mineBlock, craftItem, smeltItem, placeItem,
    killMob, pickupNearbyItems, moveToPosition,
    exploreUntil, equipItem, eatFood, activateNearestBlock,
};
