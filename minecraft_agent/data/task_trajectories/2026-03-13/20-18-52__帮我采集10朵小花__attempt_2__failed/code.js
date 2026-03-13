async function collectTenFlowers(bot) {
    // 定义小花的类型
    const flowerTypes = ["dandelion", "azure_bluet"];
    let totalFlowers = 0;
    for (const flower of flowerTypes) {
        const item = bot.inventory.items().find(i => i.name === flower);
        if (item) totalFlowers += item.count;
    }
    bot.chat(`当前有 ${totalFlowers} 朵小花，需要采集 ${10 - totalFlowers} 朵`);

    // 如果已经足够，直接返回
    if (totalFlowers >= 10) {
        bot.chat("已经收集了足够的小花！");
        return;
    }

    // 采集直到达到10朵
    while (totalFlowers < 10) {
        // 寻找附近的小花
        let nearbyFlowers = [];
        for (const flower of flowerTypes) {
            const flowers = bot.findNearbyBlocks(flower, 32, 20);
            nearbyFlowers = nearbyFlowers.concat(flowers);
        }
        
        if (nearbyFlowers.length === 0) {
            bot.chat("附近没有小花，开始探索寻找...");
            // 随机选择一个方向探索
            const directions = ['north', 'south', 'east', 'west'];
            const randomDir = directions[Math.floor(Math.random() * directions.length)];
            await exploreUntil(bot, randomDir, 64, () => {
                let found = false;
                for (const flower of flowerTypes) {
                    if (bot.findNearbyBlocks(flower, 32, 5).length > 0) {
                        found = true;
                        break;
                    }
                }
                return found;
            });
            // 探索后重新检查附近的小花
            continue;
        }

        // 采集最近的一朵小花
        const targetFlower = nearbyFlowers[0];
        bot.chat(`找到小花在 ${targetFlower.x}, ${targetFlower.y}, ${targetFlower.z}`);
        
        // 移动到小花附近
        await moveToPosition(bot, targetFlower.x, targetFlower.y, targetFlower.z, 2);
        
        // 采集小花（空手即可）
        await equipItem(bot, "air", "hand");
        await mineBlock(bot, targetFlower.name, 1);
        
        // 捡起掉落物
        await pickupNearbyItems(bot);
        
        // 更新计数
        totalFlowers = 0;
        for (const flower of flowerTypes) {
            const item = bot.inventory.items().find(i => i.name === flower);
            if (item) totalFlowers += item.count;
        }
        bot.chat(`采集后现在有 ${totalFlowers} 朵小花`);
    }
    
    bot.chat(`任务完成！成功采集了10朵小花`);
}