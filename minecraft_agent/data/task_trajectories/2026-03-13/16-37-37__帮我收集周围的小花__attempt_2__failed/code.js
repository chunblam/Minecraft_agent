async function collectNearbyFlowers(bot) {
    // 1. 确保空手采集
    await equipItem(bot, "air", "hand");
    bot.chat("开始收集周围的小花...");

    // 定义要收集的花类型
    const flowerTypes = ["dandelion", "cornflower", "azure_bluet", "oxeye_daisy"];
    let totalCollected = 0;

    // 2. 循环采集每种花
    for (const flowerType of flowerTypes) {
        bot.chat(`寻找 ${flowerType}...`);
        // 查找附近该类型的花
        let flowers = bot.findNearbyBlocks(flowerType, 32, 20);
        if (flowers.length === 0) {
            bot.chat(`附近没有找到 ${flowerType}`);
            continue;
        }
        bot.chat(`找到 ${flowers.length} 个 ${flowerType}`);

        // 3. 采集每个花方块
        for (const flower of flowers) {
            // 移动到花附近（距离2格以内）
            await moveToPosition(bot, flower.x, flower.y, flower.z, 2);
            // 采集花（空手）
            await mineBlock(bot, flowerType, 1);
            totalCollected++;
            bot.chat(`采集了1个 ${flowerType} (总计 ${totalCollected} 个)`);
            // 捡起掉落物
            await pickupNearbyItems(bot);
        }
    }

    // 4. 最终报告
    const inventoryFlowers = bot.inventory.items().filter(item => 
        flowerTypes.includes(item.name)
    );
    let finalReport = "收集完成！背包中的花：";
    if (inventoryFlowers.length === 0) {
        finalReport += "无";
    } else {
        finalReport += inventoryFlowers.map(item => `${item.name}x${item.count}`).join(", ");
    }
    bot.chat(finalReport);
    bot.chat(`总共采集了 ${totalCollected} 朵花`);
}