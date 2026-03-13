async function craftPlanksFromLogs(bot) {
    // 1. 检查当前橡木原木数量
    const logs = bot.inventory.items().find(item => item.name === "oak_log");
    const logCount = logs ? logs.count : 0;
    bot.chat(`当前有 ${logCount} 个橡木原木`);

    // 2. 如果原木数量不足，先采集
    if (logCount < 4) {
        bot.chat("橡木原木不足，开始采集...");
        const targetLogs = 4; // 采集4个原木，足够合成16个木板
        await collectMoreLogs(bot, { logType: "oak_log", targetCount: targetLogs });
    }

    // 3. 重新检查原木数量
    const logsAfterCollect = bot.inventory.items().find(item => item.name === "oak_log");
    const newLogCount = logsAfterCollect ? logsAfterCollect.count : 0;
    if (newLogCount === 0) {
        bot.chat("采集后仍然没有橡木原木，无法合成木板");
        return;
    }

    // 4. 合成木板（每次合成4个，对应1个原木）
    const planksToCraft = newLogCount * 4;
    bot.chat(`将使用 ${newLogCount} 个橡木原木合成 ${planksToCraft} 个木板`);
    
    let craftedCount = 0;
    for (let i = 0; i < newLogCount; i++) {
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

    // 5. 报告最终结果
    const finalPlanks = bot.inventory.items().find(item => item.name === "oak_planks");
    const plankCount = finalPlanks ? finalPlanks.count : 0;
    const remainingLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const remainingLogCount = remainingLogs ? remainingLogs.count : 0;
    
    bot.chat(`合成完成：现在有 ${plankCount} 个橡木木板，剩余 ${remainingLogCount} 个橡木原木`);
}