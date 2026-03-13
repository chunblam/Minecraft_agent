async function collectLogsAndCraftTableSticks(bot) {
    // 1. 采集橡木原木
    bot.chat("开始采集橡木原木");
    const neededLogs = 4;
    const currentLogs = bot.inventory.items().find(item => item.name === "oak_log")?.count || 0;
    if (currentLogs < neededLogs) {
        await mineBlock(bot, "oak_log", neededLogs - currentLogs);
        await pickupNearbyItems(bot);
    }
    bot.chat(`已采集橡木原木，当前数量: ${bot.inventory.items().find(item => item.name === "oak_log")?.count || 0}`);

    // 2. 合成工作台
    bot.chat("尝试合成工作台");
    try {
        await craftItem(bot, "crafting_table", 1);
        bot.chat("工作台合成成功");
    } catch (error) {
        bot.chat(`合成工作台失败: ${error.message}`);
        // 如果失败，尝试先合成木板
        bot.chat("尝试先合成木板");
        // 确保有工作台可用？但还没有工作台。所以可能需要放置一个工作台？但还没有合成。
        // 我们将尝试合成木板，如果失败，则报告错误。
        await craftItem(bot, "oak_planks", 4);
        bot.chat("木板合成成功，再次尝试合成工作台");
        await craftItem(bot, "crafting_table", 1);
        bot.chat("工作台合成成功");
    }

    // 3. 合成木棍
    bot.chat("准备合成木棍");
    // 检查木板数量
    const planks = bot.inventory.items().find(item => item.name === "oak_planks")?.count || 0;
    if (planks < 2) {
        // 合成更多木板
        const logs = bot.inventory.items().find(item => item.name === "oak_log")?.count || 0;
        if (logs > 0) {
            await craftItem(bot, "oak_planks", Math.min(logs * 4, 4)); // 合成最多4个木板
        } else {
            bot.chat("没有原木来合成木板，需要更多原木");
            await mineBlock(bot, "oak_log", 1);
            await pickupNearbyItems(bot);
            await craftItem(bot, "oak_planks", 4);
        }
    }
    await craftItem(bot, "stick", 4); // 2个木板合成4个木棍
    bot.chat("木棍合成成功");

    // 4. 报告完成
    const finalLogs = bot.inventory.items().find(item => item.name === "oak_log")?.count || 0;
    const finalPlanks = bot.inventory.items().find(item => item.name === "oak_planks")?.count || 0;
    const finalSticks = bot.inventory.items().find(item => item.name === "stick")?.count || 0;
    const finalTable = bot.inventory.items().find(item => item.name === "crafting_table")?.count || 0;
    bot.chat(`任务完成！原木: ${finalLogs}, 木板: ${finalPlanks}, 木棍: ${finalSticks}, 工作台: ${finalTable}`);
}