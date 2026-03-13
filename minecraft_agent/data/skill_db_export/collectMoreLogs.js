async function collectMoreLogs(bot, params = {}) { 
    const logType = params.logType || 'oak_log';
    const targetCount = params.targetCount || 3;
    const maxAttempts = params.maxAttempts || 10;
    bot.chat(`当前有 ${bot.inventory.items().find(item => item.name === logType)?.count || 0} 个 ${logType}，需要再收集 ${targetCount} 个`);
    let currentCount = bot.inventory.items().find(item => item.name === logType)?.count || 0;
    let attempts = 0;
    while (currentCount < targetCount && attempts < maxAttempts) {
        attempts++;
        bot.chat(`开始第 ${attempts} 次尝试收集 ${logType} 原木`);
        await breakOneLog(bot, { logType });
        currentCount = bot.inventory.items().find(item => item.name === logType)?.count || 0;
        bot.chat(`收集后现在有 ${currentCount} 个 ${logType} 原木`);
        if (currentCount >= targetCount) {
            bot.chat(`已收集足够 ${logType} 原木，当前 ${currentCount} 个`);
            break;
        }
    }
    const finalCount = bot.inventory.items().find(item => item.name === logType)?.count || 0;
    if (finalCount >= targetCount) {
        bot.chat(`任务完成！总共收集了 ${finalCount} 个 ${logType} 原木`);
    } else {
        bot.chat(`未能收集足够 ${logType} 原木，当前只有 ${finalCount} 个`);
    }
}