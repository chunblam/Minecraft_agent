async function collectTenFlowers(bot) {
    // 定义小花的类型
    const flowerTypes = ["dandelion", "azure_bluet"];
    
    // 计算当前已有小花总数
    function countFlowers() {
        let total = 0;
        for (const flower of flowerTypes) {
            const item = bot.inventory.items().find(i => i.name === flower);
            if (item) total += item.count;
        }
        return total;
    }
    
    let totalFlowers = countFlowers();
    bot.chat(`当前有 ${totalFlowers} 朵小花，需要采集 ${10 - totalFlowers} 朵`);
    
    // 如果已经足够，直接返回
    if (totalFlowers >= 10) {
        bot.chat("已经收集了足够的小花！");
        return;
    }
    
    // 采集直到达到10朵
    while (totalFlowers < 10) {
        // 寻找附近的小花
        let nearestFlower = null;
        let nearestFlowerType = null;
        let nearestDistance = Infinity;
        
        for (const flowerType of flowerTypes) {
            const flowers = bot.findNearbyBlocks(flowerType, 32, 20);
            for (const flowerPos of flowers) {
                const dx = flowerPos.x - bot.entity.position.x;
                const dz = flowerPos.z - bot.entity.position.z;
                const distance = Math.sqrt(dx * dx + dz * dz);
                if (distance < nearestDistance) {
                    nearestDistance = distance;
                    nearestFlower = flowerPos;
                    nearestFlowerType = flowerType;
                }
            }
        }
        
        // 如果附近没有小花，探索寻找
        if (!nearestFlower) {
            bot.chat("附近没有小花，开始探索寻找...");
            const directions = ['north', 'south', 'east', 'west'];
            const randomDir = directions[Math.floor(Math.random() * directions.length)];
            await exploreUntil(bot, randomDir, 64, () => {
                for (const flowerType of flowerTypes) {
                    if (bot.findNearbyBlocks(flowerType, 32, 5).length > 0) {
                        return true;
                    }
                }
                return false;
            });
            // 探索后继续循环
            continue;
        }
        
        // 采集最近的小花
        bot.chat(`找到 ${nearestFlowerType} 在 ${nearestFlower.x}, ${nearestFlower.y}, ${nearestFlower.z}`);
        
        // 移动到小花附近
        await moveToPosition(bot, nearestFlower.x, nearestFlower.y, nearestFlower.z, 2);
        
        // 空手采集小花
        await equipItem(bot, "air", "hand");
        await mineBlock(bot, nearestFlowerType, 1);
        
        // 捡起掉落物
        await pickupNearbyItems(bot);
        
        // 更新计数
        totalFlowers = countFlowers();
        bot.chat(`采集后现在有 ${totalFlowers} 朵小花`);
    }
    
    bot.chat(`任务完成！成功采集了10朵小花`);
}