/**
 * Mineflayer Agent Server v3 — Voyager 架构
 *
 *   POST /start          → 创建 Bot，连接 Minecraft 服务器
 *   POST /execute_code   → ★ 执行 LLM 生成的 async JS 函数（Voyager 方式）
 *   POST /step           → 兼容旧的 JSON action（保留用于简单操作）
 *   POST /observe        → 只读取当前状态
 *   POST /stop           → 断开 Bot
 *   GET  /status         → 连接状态
 */

"use strict";

const express    = require("express");
const bodyParser = require("body-parser");
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals: {
    GoalNear, GoalNearXZ, GoalBlock, GoalXZ, GoalY,
    GoalGetToBlock, GoalLookAtBlock, GoalFollow, GoalCompositeAny,
}} = require("mineflayer-pathfinder");
const { plugin: collectBlockPlugin } = require("mineflayer-collectblock");
const { plugin: toolPlugin }         = require("mineflayer-tool");
const { plugin: pvpPlugin }          = require("mineflayer-pvp");
const Vec3        = require("vec3").Vec3;
const mcDataLoader = require("minecraft-data");
const primitives  = require("./lib/primitives");

const app  = express();
app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: false }));

// 防止未捕获异常/未处理 Promise 导致进程直接退出（agent 会“自己退出游戏”）
process.on("uncaughtException", (err) => {
    console.error("[server] uncaughtException:", err.message);
});
process.on("unhandledRejection", (reason, p) => {
    console.error("[server] unhandledRejection:", reason);
});

const PORT = process.argv[2] || 3000;

let bot    = null;
let mcData = null;

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ── 确保 /start 始终返回 HTTP 响应，避免 Python 端收到 Server disconnected ───
function sendJson(res, status, body) {
    if (res.headersSent) return;
    res.status(status).json(body);
}

app.post("/start", async (req, res) => {
    try {
        if (bot) { try { bot.end(); } catch (_) {} bot = null; await sleep(1000); }

        const {
            host = "localhost", port = 25565,
            username = "Agent", waitTicks = 5, reset = "soft",
        } = req.body;

        console.log(`[start] 连接 ${host}:${port} username=${username}`);

        bot = mineflayer.createBot({
            host, port: Number(port), username,
            disableChatSigning: true,
            checkTimeoutInterval: 60 * 60 * 1000,
        });

        bot.waitTicks = waitTicks;
        bot.chatLogs  = [];
        bot.waitForTicks = (n) => new Promise(r => setTimeout(r, Math.max(1, n) * 50));

        bot.loadPlugin(pathfinder);
        bot.loadPlugin(collectBlockPlugin);
        bot.loadPlugin(toolPlugin);
        bot.loadPlugin(pvpPlugin);

        bot.on("chat", (u, m) => {
            bot.chatLogs.push({ username: u, message: m, time: Date.now() });
            if (bot.chatLogs.length > 50) bot.chatLogs.shift();
        });
        bot.on("mount",  () => { try { bot.dismount(); } catch (_) {} });
        // 踢出/错误统一前缀，便于在终端和日志中判别退出原因
        bot.on("kicked", (r) => {
            const reason = (typeof r === "string") ? r : JSON.stringify(r);
            console.log("[MC-KICK] reason=" + reason);
            bot = null;
        });
        bot.on("error", (e) => {
            const msg = (e && e.message) ? e.message : String(e);
            console.error("[MC-ERROR] " + msg);
        });
        bot.on("end", (reason) => {
            const s = (reason != null) ? String(reason) : "connection_closed";
            console.log("[MC-END] " + s);
        });

        try {
            await new Promise((resolve, reject) => {
                bot.once("spawn", resolve);
                bot.once("error", reject);
                setTimeout(() => reject(new Error("spawn timeout (30s)")), 30000);
            });

            mcData = mcDataLoader(bot.version);
            primitives.inject(bot, mcData);

            const mv = new Movements(bot);
            mv.allowSprinting = true;
            bot.pathfinder.setMovements(mv);

            if (reset === "hard") {
                bot.chat("/clear @s");
                bot.chat("/kill @s");
                await bot.waitForTicks(20);
            }

            await bot.waitForTicks(waitTicks);
            console.log(`[start] ✅ Bot 已加入，位置: ${JSON.stringify(bot.entity.position)}`);
            sendJson(res, 200, { status: "started", game_state: buildObservation(bot) });
        } catch (err) {
            const msg = (err && err.message) ? String(err.message) : String(err);
            if (bot) { try { bot.end(); } catch (_) {} bot = null; }
            console.error(`[start] 连接 MC 失败: ${msg}`);
            sendJson(res, 500, { error: msg });
        }
    } catch (outerErr) {
        const msg = (outerErr && outerErr.message) ? String(outerErr.message) : String(outerErr);
        console.error("[start] 内部错误:", msg);
        sendJson(res, 500, { error: msg });
    }
});

// ── /execute_code ★ 核心端点 ─────────────────────────────────────────────────
// 接收 LLM 生成的 async function，注入 bot 执行，返回 stdout + game_state
//
// Body:
//   code           : string   — "async function taskName(bot) { ... }"
//   timeout_ms     : number  — 默认 60000ms
//   injected_skills: string[] — 检索到的技能代码列表，与用户代码同作用域，供按名调用
//
// Response:
//   { success, output, error, game_state }
//   output: bot.chat() 和 console.log() 的所有输出（作为 observation）

app.post("/execute_code", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started. Call /start first." });

    const { code = "", timeout_ms = 60000, injected_skills } = req.body;
    if (!code.trim()) return res.status(400).json({ error: "code is empty" });
    const skillBlocks = Array.isArray(injected_skills) ? injected_skills : [];

    // ── 捕获所有输出作为 observation ─────────────────────────────────────────
    const outputLines = [];
    const origChat = bot.chat.bind(bot);

    // 劫持 bot.chat 记录中间输出
    bot.chat = (msg) => {
        outputLines.push(`[chat] ${msg}`);
        origChat(msg);
    };

    // 劫持 console.log（限本次执行期间）
    const origLog = console.log;
    console.log = (...args) => {
        const line = args.map(a => (typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
        outputLines.push(line);
        origLog(...args);
    };

    let error   = null;
    let success = true;

    try {
        // ── 构建执行沙箱：pathfinder 目标 + 控制原语（供 LLM 生成代码调用）────────
        const contextCode = `
const Vec3 = __ctx.Vec3;
const GoalNear = __ctx.GoalNear;
const GoalNearXZ = __ctx.GoalNearXZ;
const GoalXZ = __ctx.GoalXZ;
const GoalGetToBlock = __ctx.GoalGetToBlock;
const GoalLookAtBlock = __ctx.GoalLookAtBlock;
const GoalFollow = __ctx.GoalFollow;
const mcData = __ctx.mcData;
const mineBlock = __ctx.mineBlock;
const craftItem = __ctx.craftItem;
const smeltItem = __ctx.smeltItem;
const placeItem = __ctx.placeItem;
const killMob = __ctx.killMob;
const pickupNearbyItems = __ctx.pickupNearbyItems;
const moveToPosition = __ctx.moveToPosition;
const exploreUntil = __ctx.exploreUntil;
const equipItem = __ctx.equipItem;
const eatFood = __ctx.eatFood;
const activateNearestBlock = __ctx.activateNearestBlock;
`;
        const injectedSkillsCode = skillBlocks.filter(Boolean).join("\n\n");
        const wrappedCode = `
${contextCode}
${injectedSkillsCode}
${code}

// 自动调用：提取函数名并执行
const fnMatch = ${JSON.stringify(code)}.match(/async\\s+function\\s+(\\w+)/);
const fnName = fnMatch ? fnMatch[1] : null;
if (fnName && typeof eval(fnName) === 'function') {
    await eval(fnName)(__ctx.bot);
}
`;
        const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
        const fn = new AsyncFunction("__ctx", wrappedCode);

        const ctx = {
            bot, mcData, Vec3,
            GoalNear, GoalNearXZ, GoalXZ,
            GoalGetToBlock, GoalLookAtBlock, GoalFollow,
            mineBlock: primitives.mineBlock,
            craftItem: primitives.craftItem,
            smeltItem: primitives.smeltItem,
            placeItem: primitives.placeItem,
            killMob: primitives.killMob,
            pickupNearbyItems: primitives.pickupNearbyItems,
            moveToPosition: primitives.moveToPosition,
            exploreUntil: primitives.exploreUntil,
            equipItem: primitives.equipItem,
            eatFood: primitives.eatFood,
            activateNearestBlock: primitives.activateNearestBlock,
        };

        // ── 带超时执行 ─────────────────────────────────────────────────────
        await Promise.race([
            fn(ctx),
            new Promise((_, reject) =>
                setTimeout(() => reject(new Error(`Execution timeout after ${timeout_ms}ms`)), timeout_ms)
            ),
        ]);

    } catch (err) {
        error   = err.message;
        success = false;
        outputLines.push(`[ERROR] ${err.message}`);
        console.error(`[execute_code] ❌`, err.message);
    } finally {
        // 恢复原始函数（若执行期间 bot 被 kick/断开则 bot 可能已为 null，避免访问导致进程崩溃）
        if (bot) {
            try { bot.chat = origChat; } catch (_) {}
            try { await bot.waitForTicks(bot.waitTicks || 5); } catch (_) {}
        }
        console.log = origLog;
    }

    const game_state = bot ? buildObservation(bot) : {};
    res.json({
        success,
        output: outputLines.join("\n"),
        error:  error ?? null,
        game_state,
    });
});

// ── /step（兼容旧 JSON action）────────────────────────────────────────────────
app.post("/step", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started." });

    const { action_type, action_params = {}, display_message = "" } = req.body;
    if (!action_type) return res.status(400).json({ error: "action_type is required" });

    console.log(`[step] action=${action_type}`);
    bot.chatLogs = [];

    let observation = "", success = true;
    try {
        observation = await executeAction(bot, mcData, action_type, action_params, display_message);
    } catch (err) {
        observation = `[ERROR] ${err.message}`;
        success = false;
    }

    await bot.waitForTicks(bot.waitTicks);
    res.json({ observation, success, game_state: buildObservation(bot) });
});

// ── /observe ─────────────────────────────────────────────────────────────────
app.post("/observe", async (req, res) => {
    if (!bot) return res.status(400).json({ error: "Bot not started" });
    await bot.waitForTicks(2);
    res.json(buildObservation(bot));
});

// ── /stop ─────────────────────────────────────────────────────────────────────
app.post("/stop", (req, res) => {
    if (bot) { try { bot.end(); } catch (_) {} bot = null; }
    res.json({ status: "stopped" });
});

// ── /status ───────────────────────────────────────────────────────────────────
app.get("/status", (req, res) => {
    res.json({ connected: bot !== null, version: bot?.version || null });
});

// ─────────────────────────────────────────────────────────────────────────────
// 游戏状态构建
// ─────────────────────────────────────────────────────────────────────────────
function buildObservation(bot) {
    if (!bot || !bot.entity) return {};

    const pos  = bot.entity.position;
    const inv  = bot.inventory.items().map(i => ({ item: i.name, count: i.count, slot: i.slot }));
    const equip = {
        mainhand: bot.heldItem?.name || null,
        armor: {
            head:  bot.inventory.slots[5]?.name || null,
            chest: bot.inventory.slots[6]?.name || null,
            legs:  bot.inventory.slots[7]?.name || null,
            feet:  bot.inventory.slots[8]?.name || null,
        },
    };

    // 附近方块（24格内，步长2）
    const nearbyBlocks = {};
    if (mcData) {
        const scanRadius = 24;
        for (let x = -scanRadius; x <= scanRadius; x += 2) {
            for (let y = -8; y <= 8; y += 2) {
                for (let z = -scanRadius; z <= scanRadius; z += 2) {
                    const block = bot.blockAt(pos.offset(x, y, z));
                    if (block && block.name !== "air" && block.name !== "cave_air") {
                        nearbyBlocks[block.name] = (nearbyBlocks[block.name] || 0) + 1;
                    }
                }
            }
        }
    }

    // 附近实体（48格内）
    const nearbyEntities = [];
    for (const entity of Object.values(bot.entities)) {
        if (entity === bot.entity) continue;
        const dist = entity.position.distanceTo(pos);
        if (dist < 48) {
            nearbyEntities.push({
                name: entity.name || entity.username || "unknown",
                type: entity.type,
                distance: Math.round(dist * 10) / 10,
                position: { x: Math.round(entity.position.x), y: Math.round(entity.position.y), z: Math.round(entity.position.z) },
            });
        }
    }
    nearbyEntities.sort((a, b) => a.distance - b.distance);

    return {
        position:       { x: Math.round(pos.x * 10) / 10, y: Math.round(pos.y * 10) / 10, z: Math.round(pos.z * 10) / 10 },
        inventory:      inv,
        equipment:      equip,
        health:         bot.health,
        food:           bot.food,
        xp_level:       bot.experience?.level || 0,
        time:           bot.time?.timeOfDay || 0,
        biome:          bot.world?.getBiome ? String(bot.world.getBiome(pos)) : "unknown",
        nearby_blocks:  nearbyBlocks,
        nearby_entities: nearbyEntities.slice(0, 10),
        chat_log:       bot.chatLogs || [],
        dimension:      bot.game?.dimension || "overworld",
    };
}

// ─────────────────────────────────────────────────────────────────────────────
// 兼容旧 JSON action 的分发（/step 端点使用）
// ─────────────────────────────────────────────────────────────────────────────
async function executeAction(bot, mcData, actionType, params, displayMessage) {
    switch (actionType) {
        case "move_to": {
            if (params.direction) {
                const dirMap = { north: [0,0,-1], south: [0,0,1], east: [1,0,0], west: [-1,0,0] };
                const d = dirMap[params.direction] || [0,0,1];
                const dist = params.distance || 32;
                const pos = bot.entity.position;
                const target = new Vec3(pos.x + d[0]*dist, pos.y, pos.z + d[2]*dist);
                await bot.pathfinder.goto(new GoalNear(target.x, target.y, target.z, 2));
                return `已向 ${params.direction} 移动 ${dist} 格`;
            }
            if (params.region_center || (params.x !== undefined)) {
                const c = params.region_center || [params.x, params.y, params.z];
                await bot.pathfinder.goto(new GoalNear(c[0], c[1], c[2], params.radius || 2));
                return `已到达 (${c[0]},${c[1]},${c[2]}) 附近`;
            }
            return "move_to: 缺少参数";
        }
        case "get_inventory": {
            const items = bot.inventory.items();
            if (items.length === 0) return "背包为空";
            return `背包: ${items.map(i => `${i.name}x${i.count}`).join(", ")}`;
        }
        case "chat": {
            bot.chat(displayMessage || params.message || "");
            return "已发送消息";
        }
        case "finish": {
            return params.message || "任务完成";
        }
        default:
            return `[step] 未知 action: ${actionType}，请使用 /execute_code`;
    }
}

app.listen(PORT, () => {
    console.log(`[server] ✅ mineflayer HTTP 服务器启动，端口 ${PORT}`);
    console.log(`[server] 端点：/start  /execute_code  /step  /observe  /stop  /status`);
});
