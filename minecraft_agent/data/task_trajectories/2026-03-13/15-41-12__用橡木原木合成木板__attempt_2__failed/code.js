async function craftPlanksFromLogs(bot) {
    // 检查背包中的橡木原木数量
    const logs = bot.inventory.items().find(item => item.name === "oak_log");
    const logCount = logs ? logs.count : 0;
    bot.chat(`背包中有 ${logCount} 个橡木原木`);

    if (logCount === 0) {
        bot.chat("没有橡木原木，需要先收集一些");
        // 使用已注入的技能 collectMoreLogs 收集橡木原木
        await collectMoreLogs(bot, { logType: "oak_log", targetCount: 1 });
    }

    // 合成木板（每个原木合成4个木板）
    // craftItem 会自动处理工作台放置和合成
    const planksToCraft = 4; // 每个原木合成4个木板
    await craftItem(bot, "oak_planks", planksToCraft);
    bot.chat("已合成橡木木板");

    // 检查合成后的木板数量
    const planks = bot.inventory.items().find(item => item.name === "oak_planks");
    const plankCount = planks ? planks.count : 0;
    bot.chat(`现在有 ${plankCount} 个橡木木板`);
}