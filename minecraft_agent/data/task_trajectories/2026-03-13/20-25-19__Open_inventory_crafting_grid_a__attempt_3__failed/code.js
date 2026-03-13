async function convertLogsToPlanks(bot) {
    // 1. 检查背包中的云杉原木
    let logs = bot.inventory.items().find(item => item.name === 'spruce_log');
    if (!logs || logs.count === 0) {
        bot.chat('背包中没有云杉原木，需要先收集一些。');
        await collectMoreLogs(bot, { logType: 'spruce_log', targetCount: 3 });
        logs = bot.inventory.items().find(item => item.name === 'spruce_log');
        if (!logs || logs.count === 0) {
            bot.chat('收集后仍然没有云杉原木，无法合成木板。');
            return;
        }
    }
    const logCount = logs.count;
    bot.chat(`背包中有 ${logCount} 个云杉原木。`);

    // 2. 装备空手
    await equipItem(bot, 'air', 'hand');

    // 3. 检查附近是否有工作台
    const nearbyTables = bot.findNearbyBlocks('crafting_table', 32, 5);
    let workbenchPlaced = false;
    if (nearbyTables.length > 0) {
        bot.chat('发现附近有工作台，前往使用。');
        const tablePos = nearbyTables[0];
        await moveToPosition(bot, tablePos.x, tablePos.y, tablePos.z, 3);
    } else {
        bot.chat('附近没有工作台，需要制作一个。');
        // 检查是否有云杉木板
        let planks = bot.inventory.items().find(item => item.name === 'spruce_planks');
        if (!planks || planks.count < 4) {
            bot.chat('云杉木板不足4个，先用玩家合成网格合成一些。');
            // 在玩家网格中一次合成一个原木（得到4个木板）
            const logsToUse = Math.min(logCount, 1); // 先合成一个原木
            await craftItem(bot, 'spruce_planks', logsToUse * 4);
            bot.chat('在玩家网格中合成了4个云杉木板。');
            planks = bot.inventory.items().find(item => item.name === 'spruce_planks');
        }
        if (planks && planks.count >= 4) {
            // 合成工作台
            bot.chat('用云杉木板合成工作台。');
            await craftItem(bot, 'crafting_table', 1);
            // 放置工作台在附近空地
            const playerPos = bot.entity.position;
            const placePos = { x: Math.floor(playerPos.x) + 2, y: Math.floor(playerPos.y), z: Math.floor(playerPos.z) };
            bot.chat(`在工作台放置在 ${placePos.x}, ${placePos.y}, ${placePos.z}`);
            await placeItem(bot, 'crafting_table', placePos);
            workbenchPlaced = true;
            await moveToPosition(bot, placePos.x, placePos.y, placePos.z, 3);
        } else {
            bot.chat('无法获得足够的云杉木板来制作工作台。');
            return;
        }
    }

    // 4. 使用工作台合成所有云杉木板
    // 重新计算原木数量（可能因合成工作台而减少）
    const currentLogs = bot.inventory.items().find(item => item.name === 'spruce_log');
    if (!currentLogs || currentLogs.count === 0) {
        bot.chat('没有云杉原木可合成木板。');
        return;
    }
    const planksToCraft = currentLogs.count * 4;
    bot.chat(`开始在工作台中将 ${currentLogs.count} 个云杉原木合成为 ${planksToCraft} 个云杉木板。`);
    await craftItem(bot, 'spruce_planks', planksToCraft);
    bot.chat(`成功合成 ${planksToCraft} 个云杉木板。`);

    // 5. 如果放置了工作台，可以捡回（可选）
    if (workbenchPlaced) {
        // 破坏工作台捡回
        const tableBlocks = bot.findNearbyBlocks('crafting_table', 32, 5);
        if (tableBlocks.length > 0) {
            const tablePos = tableBlocks[0];
            await moveToPosition(bot, tablePos.x, tablePos.y, tablePos.z, 2);
            await mineBlock(bot, 'crafting_table', 1);
            await pickupNearbyItems(bot);
            bot.chat('已回收工作台。');
        }
    }
}