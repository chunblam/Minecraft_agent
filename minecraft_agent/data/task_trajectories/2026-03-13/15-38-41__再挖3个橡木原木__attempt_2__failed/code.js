async function collectThreeMoreOakLogs(bot) {
    // 检查当前橡木原木数量
    const currentLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const currentCount = currentLogs ? currentLogs.count : 0;
    bot.chat(`当前有 ${currentCount} 个橡木原木，需要再挖3个`);
    
    // 需要挖掘的数量
    const neededLogs = 3;
    
    // 循环挖掘需要的橡木原木
    for (let i = 0; i < neededLogs; i++) {
        bot.chat(`开始挖掘第 ${i + 1} 个橡木原木`);
        
        // 使用breakOneLog技能挖掘1个橡木原木
        await breakOneLog(bot, { logType: "oak_log" });
        
        // 检查挖掘后的数量
        const updatedLogs = bot.inventory.items().find(item => item.name === "oak_log");
        const updatedCount = updatedLogs ? updatedLogs.count : 0;
        bot.chat(`已挖掘 ${i + 1} 个，现在有 ${updatedCount} 个橡木原木`);
    }
    
    // 最终检查
    const finalLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const finalCount = finalLogs ? finalLogs.count : 0;
    bot.chat(`任务完成！总共收集了 ${finalCount} 个橡木原木`);
}