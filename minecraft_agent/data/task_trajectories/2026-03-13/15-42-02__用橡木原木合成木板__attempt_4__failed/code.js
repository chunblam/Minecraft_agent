async function craftAllPlanksFromLogs(bot) {
    // 检查背包中的橡木原木数量
    const logs = bot.inventory.items().find(item => item.name === "oak_log");
    const logCount = logs ? logs.count : 0;
    bot.chat(`背包中有 ${logCount} 个橡木原木`);

    if (logCount === 0) {
        bot.chat("没有橡木原木，无法合成木板");
        return;
    }

    // 计算需要合成的木板总数（每个原木合成4个木板）
    const planksToCraft = logCount * 4;
    bot.chat(`将使用所有 ${logCount} 个橡木原木合成 ${planksToCraft} 个木板`);

    // 分批合成：每次合成4个木板（对应1个原木）
    let craftedCount = 0;
    for (let i = 0; i < logCount; i++) {
        bot.chat(`正在合成第 ${i + 1} 组木板（4个）...`);
        try {
            await craftItem(bot, "oak_planks", 4);
            craftedCount += 4;
            bot.chat(`已合成 ${craftedCount} 个木板`);
        } catch (err) {
            bot.chat(`合成第 ${i + 1} 组时出错: ${err.message}`);
            // 继续尝试下一组
        }
        // 短暂等待确保状态更新
        await bot.waitForTicks(5);
    }

    // 检查合成后的木板数量
    const planks = bot.inventory.items().find(item => item.name === "oak_planks");
    const plankCount = planks ? planks.count : 0;
    const logsAfter = bot.inventory.items().find(item => item.name === "oak_log");
    const remainingLogs = logsAfter ? logsAfter.count : 0;
    
    bot.chat(`合成完成：现在有 ${plankCount} 个橡木木板，剩余 ${remainingLogs} 个橡木原木`);
}