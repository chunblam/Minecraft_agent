async function convertLogsToPlanks(bot) {
    // 1. 检查背包中的云杉原木
    let logs = bot.inventory.items().find(item => item.name === 'spruce_log');
    if (!logs || logs.count === 0) {
        bot.chat('背包中没有云杉原木，需要先收集一些。');
        await collectMoreLogs(bot, { logType: 'spruce_log', targetCount: 1 });
        logs = bot.inventory.items().find(item => item.name === 'spruce_log');
        if (!logs || logs.count === 0) {
            bot.chat('收集后仍然没有云杉原木，无法合成木板。');
            return;
        }
    }
    const logCount = logs.count;
    bot.chat(`背包中有 ${logCount} 个云杉原木。`);

    // 2. 检查附近是否有工作台
    const nearbyTables = bot.findNearbyBlocks('crafting_table', 32, 1);
    if (nearbyTables.length === 0) {
        bot.chat('附近没有工作台，需要合成一个。');
        // 检查背包是否有4个云杉木板
        let planks = bot.inventory.items().find(item => item.name === 'spruce_planks');
        if (!planks || planks.count < 4) {
            bot.chat('云杉木板不足4个，用1个云杉原木合成4个木板。');
            // 装备空手
            await equipItem(bot, 'air', 'hand');
            // 在玩家合成网格中合成4个云杉木板（需要1个原木）
            await craftItem(bot, 'spruce_planks', 4);
            bot.chat('已合成4个云杉木板。');
        }
        // 现在合成工作台
        bot.chat('开始合成工作台。');
        await craftItem(bot, 'crafting_table', 1);
        bot.chat('成功合成工作台。');
        // 放置工作台以便使用
        const playerPos = bot.entity.position;
        const placePos = { x: Math.floor(playerPos.x) + 2, y: Math.floor(playerPos.y), z: Math.floor(playerPos.z) };
        await placeItem(bot, 'crafting_table', placePos);
        bot.chat('已放置工作台。');
    } else {
        bot.chat('附近已有工作台，直接使用。');
        // 移动到最近的工作台
        const tablePos = nearbyTables[0];
        await moveToPosition(bot, tablePos.x, tablePos.y, tablePos.z, 3);
    }

    // 3. 使用工作台将剩余原木合成为木板
    // 重新获取原木数量
    logs = bot.inventory.items().find(item => item.name === 'spruce_log');
    if (!logs || logs.count === 0) {
        bot.chat('没有原木可合成。');
        return;
    }
    const remainingLogs = logs.count;
    const planksToCraft = remainingLogs * 4;
    bot.chat(`使用工作台将 ${remainingLogs} 个云杉原木合成为 ${planksToCraft} 个云杉木板。`);
    // 装备空手以打开工作台
    await equipItem(bot, 'air', 'hand');
    // 激活工作台
    await activateNearestBlock(bot, 'crafting_table');
    // 使用 craftItem 原语，它会自动处理工作台合成
    await craftItem(bot, 'spruce_planks', planksToCraft);
    bot.chat(`成功合成 ${planksToCraft} 个云杉木板。`);
}