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

    // 2. 装备空手以使用合成网格
    await equipItem(bot, 'air', 'hand');

    // 3. 在玩家合成网格中将所有原木合成为木板
    // 每个原木合成4个木板
    const planksToCraft = logCount * 4;
    bot.chat(`开始在玩家合成网格中将 ${logCount} 个云杉原木合成为 ${planksToCraft} 个云杉木板。`);
    await craftItem(bot, 'spruce_planks', planksToCraft);
    bot.chat(`成功合成 ${planksToCraft} 个云杉木板。`);
}