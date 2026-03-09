/**
 * Mineflayer Agent Server
 *
 * 完全参照 Voyager 架构，替换原 Java Fabric Mod。
 * Python 端通过 HTTP 与此服务器通信：
 *
 *   POST /start   → 创建 Bot，连接 Minecraft 服务器
 *   POST /step    → 执行结构化 action，返回观察结果
 *   POST /observe → 只读取当前状态（不执行代码）
 *   POST /stop    → 断开 Bot
 */

"use strict";

const express = require("express");
const bodyParser = require("body-parser");
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals: {
    GoalNear, GoalNearXZ, GoalBlock, GoalXZ, GoalY,
    GoalGetToBlock, GoalLookAtBlock, GoalFollow, GoalCompositeAny,
}} = require("mineflayer-pathfinder");
const { plugin: collectBlockPlugin } = require("mineflayer-collectblock");
const { plugin: toolPlugin } = require("mineflayer-tool");
const { plugin: pvpPlugin } = require("mineflayer-pvp");
const Vec3 = require("vec3").Vec3;
const mcDataLoader = require("minecraft-data");

const primitives = require("./lib/primitives");

const app = express();
app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: false }));

const PORT = process.argv[2] || 3000;

let bot = null;
let mcData = null;

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// ── /start ───────────────────────────────────────────────────────────────────
app.post("/start", async (req, res) => {
    if (bot) {
        try { bot.end(); } catch (_) {}
        bot = null;
        await sleep(1000);
    }

    const {
        host = "localhost",
        port = 25565,
        username = "Agent",
        waitTicks = 5,
        reset = "soft",
    } = req.body;

    console.log(`[start] 连接 ${host}:${port} username=${username} reset=${reset}`);

    bot = mineflayer.createBot({
        host,
        port: Number(port),
        username,
        disableChatSigning: true,
        checkTimeoutInterval: 60 * 60 * 1000,
    });

    bot.waitTicks = waitTicks;
    bot.globalTickCounter = 0;
    bot.stuckTickCounter = 0;
    bot.stuckPosList = [];
    bot.chatLogs = [];

    // 20 TPS ≈ 50ms/tick
    bot.waitForTicks = (n) => new Promise(resolve => setTimeout(resolve, Math.max(1, n) * 50));

    bot.loadPlugin(pathfinder);
    bot.loadPlugin(collectBlockPlugin);
    bot.loadPlugin(toolPlugin);
    bot.loadPlugin(pvpPlugin);

    bot.on("chat", (username, message) => {
        bot.chatLogs.push({ username, message, time: Date.now() });
        if (bot.chatLogs.length > 50) bot.chatLogs.shift();
    });

    bot.on("mount", () => { try { bot.dismount(); } catch (_) {} });

    bot.on("kicked", (reason) => {
        console.log("[kicked]", reason);
        bot = null;
    });
    bot.on("error", (err) => {
        console.error("[bot error]", err.message);
    });

    await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("spawn timeout")), 30000);
        bot.once("spawn", () => {
            clearTimeout(timeout);
            resolve();
        });
        bot.once("error", (e) => {
            clearTimeout(timeout);
            reject(e);
        });
    }).catch(err => {
        res.status(500).json({ error: err.message });
        return;
    });

    if (!res.headersSent) {
        mcData = mcDataLoader(bot.version);

        const movements = new Movements(bot);
        movements.canDig = true;
        movements.allowSprinting = true;
        bot.pathfinder.setMovements(movements);

        primitives.inject(bot, mcData);

        if (reset === "hard") {
            bot.chat("/clear @s");
            await sleep(500);
        }

        await bot.waitForTicks(bot.waitTicks);

        const state = buildObservation(bot);
        console.log(`[start] ✅ Bot 已生成，位置: ${JSON.stringify(state.position)}`);
        res.json({ status: "ok", observation: state });
    }
});

// ── /step ────────────────────────────────────────────────────────────────────
app.post("/step", async (req, res) => {
    if (!bot) {
        return res.status(400).json({ error: "Bot not started. Call /start first." });
    }

    const { action_type, action_params = {}, display_message = "" } = req.body;
    if (!action_type) {
        return res.status(400).json({ error: "action_type is required" });
    }

    console.log(`[step] action=${action_type} params=${JSON.stringify(action_params)}`);

    bot.chatLogs = [];

    let observation = "";
    let success = true;

    try {
        observation = await executeAction(bot, mcData, action_type, action_params, display_message);
    } catch (err) {
        observation = `[ERROR] ${err.message}`;
        success = false;
        console.error(`[step] ❌ ${action_type} 失败:`, err.message);
    }

    await bot.waitForTicks(bot.waitTicks);
    const game_state = buildObservation(bot);

    res.json({ observation, success, game_state });
});

// ── /observe ─────────────────────────────────────────────────────────────────
app.post("/observe", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });
    await bot.waitForTicks(2);
    res.json(buildObservation(bot));
});

// ── /stop ────────────────────────────────────────────────────────────────────
app.post("/stop", (req, res) => {
    if (bot) {
        try { bot.end(); } catch (_) {}
        bot = null;
    }
    res.json({ status: "stopped" });
});

// ── /status ──────────────────────────────────────────────────────────────────
app.get("/status", (req, res) => {
    res.json({ connected: bot !== null, version: bot?.version || null });
});

// ── 动作执行分发 ───────────────────────────────────────────────────────────────
async function executeAction(bot, mcData, actionType, params, displayMessage) {
    switch (actionType) {

        case "move_to": {
            if (params.direction) {
                const dist = params.distance || 32;
                const dir = directionToVec3(params.direction);
                const target = bot.entity.position.plus(dir.scaled(dist));
                await bot.pathfinder.goto(new GoalNearXZ(target.x, target.z));
                return `向 ${params.direction} 移动了 ${dist} 格`;
            } else if (params.region_center) {
                const [cx, cy, cz] = params.region_center;
                const r = params.radius || 2;
                await bot.pathfinder.goto(new GoalNear(cx, cy, cz, r));
                return `已到达 (${cx.toFixed(1)},${cy.toFixed(1)},${cz.toFixed(1)}) 附近`;
            } else if (params.x !== undefined) {
                const r = params.radius || 1;
                await bot.pathfinder.goto(new GoalNear(params.x, params.y, params.z, r));
                return `已到达 (${params.x.toFixed(1)},${params.y.toFixed(1)},${params.z.toFixed(1)}) 附近`;
            }
            return "move_to: 参数不完整";
        }

        case "mine_block": {
            if (params.x !== undefined) {
                const pos = new Vec3(Math.floor(params.x), Math.floor(params.y), Math.floor(params.z));
                const block = bot.blockAt(pos);
                if (!block || block.type === 0) return `坐标 (${params.x},${params.y},${params.z}) 没有方块`;
                await bot.pathfinder.goto(new GoalGetToBlock(pos.x, pos.y, pos.z));
                await bot.dig(block);
                return `已挖掘 ${block.name} (${pos.x},${pos.y},${pos.z})`;
            } else if (params.name) {
                const count = params.count || 1;
                await bot.mineBlock(params.name, count);
                return `已挖掘 ${count} 个 ${params.name}`;
            }
            return "mine_block: 需要 x/y/z 或 name";
        }

        case "place_block": {
            const blockName = (params.block || "").replace("minecraft:", "");
            const item = bot.inventory.items().find(i => i.name === blockName);
            if (!item) return `背包中没有 ${blockName}`;
            const pos = new Vec3(Math.floor(params.x), Math.floor(params.y), Math.floor(params.z));
            await bot.placeBlock(pos, blockName);
            return `已放置 ${blockName} 在 (${pos.x},${pos.y},${pos.z})`;
        }

        case "craft_item": {
            const itemName = (params.item || "").replace("minecraft:", "");
            const count = params.count || 1;
            await bot.craftItem(itemName, count);
            return `已合成 ${count} 个 ${itemName}`;
        }

        case "smelt_item": {
            const itemName = (params.item || "").replace("minecraft:", "");
            const fuel = (params.fuel || "coal").replace("minecraft:", "");
            const count = params.count || 1;
            await bot.smeltItem(itemName, fuel, count);
            return `已熔炼 ${count} 个 ${itemName}`;
        }

        case "find_resource": {
            const resourceName = (params.type || "").replace("minecraft:", "");
            const radius = params.radius || 32;
            const results = bot.findNearbyBlocks(resourceName, radius, 10);
            if (results.length === 0) {
                return `在 ${radius} 格内未找到 ${resourceName}`;
            }
            const formatted = results.slice(0, 5).map(b =>
                `(${b.x},${b.y},${b.z})`
            ).join(", ");
            return `找到 ${results.length} 个 ${resourceName}：${formatted}`;
        }

        case "scan_area": {
            const radius = params.radius || 24;
            const scan = bot.scanNearby(radius);
            return `扫描结果（${radius}格）：${scan}`;
        }

        case "interact_entity": {
            const entityType = (params.entity_type || "").replace("minecraft:", "");
            const action = params.action || "find";
            const entity = bot.nearestEntity(e =>
                (e.name === entityType || e.displayName?.toLowerCase().includes(entityType)) &&
                e.position.distanceTo(bot.entity.position) < 32
            );
            if (!entity) return `附近没有 ${entityType}`;
            if (action === "find") {
                const pos = entity.position;
                return `找到 ${entityType} 在 (${pos.x.toFixed(1)},${pos.y.toFixed(1)},${pos.z.toFixed(1)})`;
            }
            if (action === "goto") {
                await bot.pathfinder.goto(new GoalNear(entity.position.x, entity.position.y, entity.position.z, 3));
                return `已移动到 ${entityType} 旁边`;
            }
            if (action === "attack") {
                await bot.pvp.attack(entity);
                return `攻击了 ${entityType}`;
            }
            if (action === "interact") {
                await bot.pathfinder.goto(new GoalNear(entity.position.x, entity.position.y, entity.position.z, 3));
                await bot.useOn(entity);
                return `与 ${entityType} 互动`;
            }
            return `未知交互动作 ${action}`;
        }

        case "get_inventory": {
            const items = bot.inventory.items();
            if (items.length === 0) return "背包为空";
            const inv = items.map(i => `${i.name} x${i.count}`).join(", ");
            return `背包（${bot.inventoryUsed()}/36）：${inv}`;
        }

        case "enchant_item": {
            return `附魔功能需要附魔台，请先放置附魔台并靠近`;
        }

        case "explore": {
            const direction = params.direction ? directionToVec3(params.direction) : new Vec3(1, 0, 1);
            const maxTime = params.max_time || 30;
            const target = params.find;

            let found = null;
            found = await bot.exploreUntil(direction, maxTime, () => {
                if (!target) return true;
                const targetName = target.replace("minecraft:", "");
                const block = bot.findBlock({
                    matching: b => b.name === targetName,
                    maxDistance: 32,
                });
                return block || null;
            });
            if (found) return `探索成功，找到 ${target || "目标"}`;
            return `探索 ${maxTime}s 未找到 ${target || "目标"}`;
        }

        case "chat": {
            const msg = displayMessage || params.message || "";
            if (msg) bot.chat(msg);
            return `已发送消息: ${msg}`;
        }

        case "look_at": {
            await bot.lookAt(new Vec3(params.x, params.y, params.z));
            return `已看向 (${params.x},${params.y},${params.z})`;
        }

        case "jump": {
            bot.setControlState("jump", true);
            await sleep(300);
            bot.setControlState("jump", false);
            return "已跳跃";
        }

        case "stop": {
            bot.pathfinder.setGoal(null);
            bot.clearControlStates();
            return "已停止移动";
        }

        case "finish": {
            const msg = displayMessage || params.message || "任务完成";
            bot.chat(msg);
            return msg;
        }

        case "look_around": {
            await bot.waitForTicks(5);
            return bot.scanNearby(params.radius || 96);
        }

        default:
            return `未知动作类型: ${actionType}`;
    }
}

function buildObservation(bot) {
    if (!bot || !bot.entity) return {};
    const pos = bot.entity.position;
    const block = bot.blockAt(pos);

    const inventory = bot.inventory.items().map(i => ({
        item: i.name,
        count: i.count,
        displayName: i.displayName,
    }));

    const slots = bot.inventory.slots;
    const equipment = {
        mainhand: bot.heldItem ? { item: bot.heldItem.name, count: bot.heldItem.count } : null,
        head: slots[5] ? { item: slots[5].name } : null,
        chest: slots[6] ? { item: slots[6].name } : null,
        legs: slots[7] ? { item: slots[7].name } : null,
        feet: slots[8] ? { item: slots[8].name } : null,
        offhand: slots[45] ? { item: slots[45].name } : null,
    };

    const nearby_entities = [];
    for (const id in bot.entities) {
        const e = bot.entities[id];
        if (!e.displayName || e.name === "player" || e.name === "item") continue;
        const dist = e.position.distanceTo(pos);
        if (dist <= 32) {
            nearby_entities.push({
                name: e.name,
                displayName: e.displayName,
                distance: parseFloat(dist.toFixed(1)),
                position: { x: e.position.x, y: e.position.y, z: e.position.z },
            });
        }
    }
    nearby_entities.sort((a, b) => a.distance - b.distance);

    const nearby_blocks = new Set();
    for (let dx = -8; dx <= 8; dx++) {
        for (let dy = -2; dy <= 2; dy++) {
            for (let dz = -8; dz <= 8; dz++) {
                const b = bot.blockAt(pos.offset(dx, dy, dz));
                if (b && b.type !== 0) nearby_blocks.add(b.name);
            }
        }
    }

    const nearby_resources = buildNearbyResources(bot, pos);

    const timeOfDay = bot.time?.timeOfDay || 0;
    let timeStr = "day";
    if (timeOfDay < 1000) timeStr = "sunrise";
    else if (timeOfDay < 6000) timeStr = "day";
    else if (timeOfDay < 13000) timeStr = "sunset";
    else if (timeOfDay < 18000) timeStr = "night";
    else timeStr = "midnight";

    return {
        position: { x: parseFloat(pos.x.toFixed(2)), y: parseFloat(pos.y.toFixed(2)), z: parseFloat(pos.z.toFixed(2)) },
        yaw: parseFloat(bot.entity.yaw.toFixed(2)),
        pitch: parseFloat(bot.entity.pitch.toFixed(2)),
        health: bot.health,
        hunger: bot.food,
        xp_level: bot.experience?.level || 0,
        biome: block?.biome?.name || "unknown",
        time: timeStr,
        inventory,
        equipment,
        nearby_entities: nearby_entities.slice(0, 20),
        nearby_blocks: Array.from(nearby_blocks),
        nearby_resources,
        chat_log: (bot.chatLogs || []).slice(-10),
        on_ground: bot.entity.onGround,
        is_in_water: bot.entity.isInWater,
        dimension: bot.game?.dimension || "overworld",
    };
}

function buildNearbyResources(bot, pos) {
    const RESOURCES = {
        "ores": ["diamond_ore", "iron_ore", "coal_ore", "gold_ore", "emerald_ore",
                 "deepslate_diamond_ore", "deepslate_iron_ore", "deepslate_coal_ore"],
        "logs": ["oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log", "mangrove_log"],
        "crafting": ["crafting_table", "furnace", "chest", "anvil", "enchanting_table"],
        "water": ["water"],
        "stone": ["stone", "cobblestone", "deepslate"],
    };

    const result = {};
    for (const [category, names] of Object.entries(RESOURCES)) {
        result[category] = [];
        for (const name of names) {
            if (!mcData?.blocksByName?.[name]) continue;
            const blocks = bot.findBlocks({
                matching: mcData.blocksByName[name].id,
                maxDistance: 32,
                count: 5,
            });
            for (const b of blocks) {
                result[category].push({
                    name,
                    x: b.x, y: b.y, z: b.z,
                    distance: parseFloat(b.distanceTo(pos).toFixed(1)),
                });
            }
        }
        result[category].sort((a, b) => a.distance - b.distance);
    }
    return result;
}

function directionToVec3(dir) {
    const map = {
        north: new Vec3(0, 0, -1), south: new Vec3(0, 0, 1),
        east: new Vec3(1, 0, 0),  west: new Vec3(-1, 0, 0),
        up: new Vec3(0, 1, 0),    down: new Vec3(0, -1, 0),
    };
    return map[dir.toLowerCase()] || new Vec3(1, 0, 0);
}

app.listen(PORT, () => {
    console.log(`Mineflayer Agent Server started on port ${PORT}`);
});
