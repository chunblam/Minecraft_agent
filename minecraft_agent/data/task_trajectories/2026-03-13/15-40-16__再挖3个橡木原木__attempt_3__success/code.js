async function collectThreeMoreOakLogs(bot) {
    // 检查当前橡木原木数量
    const currentLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const currentCount = currentLogs ? currentLogs.count : 0;
    bot.chat(`当前有 ${currentCount} 个橡木原木，需要再收集3个`);
    
    // 需要收集的总数
    const targetCount = currentCount + 3;
    let attempts = 0;
    const maxAttempts = 10; // 防止无限循环
    
    while (currentCount < targetCount && attempts < maxAttempts) {
        attempts++;
        bot.chat(`开始第 ${attempts} 次尝试收集橡木原木`);
        
        // 使用已有的 breakOneLog 技能来破坏一个橡木原木
        await breakOneLog(bot, { logType: "oak_log" });
        
        // 更新当前数量
        const updatedLogs = bot.inventory.items().find(item => item.name === "oak_log");
        const updatedCount = updatedLogs ? updatedLogs.count : 0;
        bot.chat(`收集后现在有 ${updatedCount} 个橡木原木`);
        
        if (updatedCount >= targetCount) {
            bot.chat(`已收集足够橡木原木，当前 ${updatedCount} 个`);
            break;
        }
    }
    
    // 最终检查
    const finalLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const finalCount = finalLogs ? finalLogs.count : 0;
    if (finalCount >= targetCount) {
        bot.chat(`任务完成！总共收集了 ${finalCount} 个橡木原木`);
    } else {
        bot.chat(`未能收集足够橡木原木，当前只有 ${finalCount} 个`);
    }
}