async function convertLogsToPlanks(bot) {
    // 检查背包中的原木
    const logs = bot.inventory.items().find(item => item.name === 'spruce_log');
    if (!logs || logs.count === 0) {
        bot.chat('背包中没有云杉原木，需要先收集一些。');
        // 使用Retrieved Skills中的collectMoreLogs函数收集云杉原木
        await collectMoreLogs(bot, { logType: 'spruce_log', targetCount: 3 });
    }

    // 装备空手以便合成
    await equipItem(bot, 'air', 'hand');
    bot.chat('手持空手准备合成。');

    // 计算可合成的木板数量：每个原木合成4个木板
    const logCount = bot.inventory.items().find(item => item.name === 'spruce_log')?.count || 0;
    const planksToCraft = logCount * 4;
    if (planksToCraft === 0) {
        bot.chat('没有原木可用于合成木板。');
        return;
    }

    bot.chat(`开始将 ${logCount} 个云杉原木合成为云杉木板。`);
    // craftItem会自动使用玩家合成网格或工作台
    await craftItem(bot, 'spruce_planks', planksToCraft);
    bot.chat(`成功合成 ${planksToCraft} 个云杉木板。`);
}