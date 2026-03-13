async function collectThreeMoreOakLogs(bot) {
    // 检查当前橡木原木数量
    const currentLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const currentCount = currentLogs ? currentLogs.count : 0;
    bot.chat(`当前有 ${currentCount} 个橡木原木，需要再收集3个`);
    
    // 需要收集的数量
    const neededLogs = 3;
    let collected = 0;
    
    // 确保手持空手
    await equipItem(bot, "air", "hand");
    
    while (collected < neededLogs) {
        // 寻找附近的橡木原木
        const oakLogs = bot.findNearbyBlocks("oak_log", 32, 10);
        
        if (oakLogs.length === 0) {
            bot.chat("附近没有橡木原木，开始探索寻找");
            // 随机选择一个方向探索
            const directions = ['north', 'south', 'east', 'west'];
            const randomDir = directions[Math.floor(Math.random() * directions.length)];
            await exploreUntil(bot, randomDir, 64, () => {
                const foundLogs = bot.findNearbyBlocks("oak_log", 32, 10);
                return foundLogs.length > 0;
            });
            continue;
        }
        
        // 选择最近的橡木原木
        const targetLog = oakLogs[0];
        bot.chat(`找到橡木原木在 ${targetLog.x}, ${targetLog.y}, ${targetLog.z}`);
        
        // 移动到原木附近
        await moveToPosition(bot, targetLog.x, targetLog.y, targetLog.z, 2);
        
        // 确保手持空手（再次确认）
        await equipItem(bot, "air", "hand");
        
        // 挖掘1个橡木原木
        bot.chat(`开始挖掘第 ${collected + 1} 个橡木原木`);
        await mineBlock(bot, "oak_log", 1);
        
        // 捡起掉落物
        await pickupNearbyItems(bot);
        
        // 更新收集计数
        collected++;
        
        // 检查当前数量
        const updatedLogs = bot.inventory.items().find(item => item.name === "oak_log");
        const updatedCount = updatedLogs ? updatedLogs.count : 0;
        bot.chat(`已收集 ${collected} 个，现在有 ${updatedCount} 个橡木原木`);
    }
    
    // 最终检查
    const finalLogs = bot.inventory.items().find(item => item.name === "oak_log");
    const finalCount = finalLogs ? finalLogs.count : 0;
    bot.chat(`任务完成！总共收集了 ${finalCount} 个橡木原木`);
}