async function punchTreeForLogs(bot) {
    // 任务：找到并击打一棵树，获得至少2个原木
    bot.chat("开始任务：击打树木获取至少2个原木。");
    
    // 检查当前背包中的原木数量（云杉原木）
    const logType = "spruce_log";
    let currentLogs = bot.inventory.items().find(item => item.name === logType);
    let currentCount = currentLogs ? currentLogs.count : 0;
    bot.chat(`当前背包中有 ${currentCount} 个 ${logType}。`);
    
    // 如果已有足够原木，直接完成
    if (currentCount >= 2) {
        bot.chat("已有至少2个原木，任务完成！");
        return;
    }
    
    // 使用已有技能收集更多原木
    bot.chat("开始收集云杉原木...");
    await collectMoreLogs(bot, { logType: logType, targetCount: 2 });
    
    // 最终检查
    currentLogs = bot.inventory.items().find(item => item.name === logType);
    currentCount = currentLogs ? currentLogs.count : 0;
    if (currentCount >= 2) {
        bot.chat(`成功收集了 ${currentCount} 个云杉原木，任务完成！`);
    } else {
        bot.chat(`只收集到 ${currentCount} 个云杉原木，未达到2个。`);
    }
}