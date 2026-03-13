async function placeWorkbench(bot) {
    // 1. 检查背包中是否有工作台
    const workbench = bot.inventory.items().find(item => item.name === 'crafting_table');
    if (!workbench) {
        bot.chat("背包中没有工作台，需要先合成一个。");
        // 检查是否有足够的橡木木板来合成工作台
        const planks = bot.inventory.items().find(item => item.name === 'oak_planks');
        if (!planks || planks.count < 4) {
            bot.chat("橡木木板不足，需要先制作木板。");
            // 直接合成木板（背包中已有原木或木板）
            await craftItem(bot, 'oak_planks', 4);
        }
        // 合成工作台
        await craftItem(bot, 'crafting_table', 1);
        bot.chat("已合成工作台。");
    } else {
        bot.chat(`背包中有 ${workbench.count} 个工作台。`);
    }

    // 2. 寻找合适的放置位置
    // 从当前位置附近找一个固体方块（例如草方块、泥土、石头）
    let solidBlocks = bot.findNearbyBlocks('grass_block', 32, 10)
        .concat(bot.findNearbyBlocks('dirt', 32, 10))
        .concat(bot.findNearbyBlocks('stone', 32, 10));
    
    // 过滤掉bot脚下的位置
    const botPos = bot.entity.position;
    solidBlocks = solidBlocks.filter(block => {
        const dx = block.x - botPos.x;
        const dz = block.z - botPos.z;
        return dx * dx + dz * dz >= 4; // 距离至少2格
    });

    if (solidBlocks.length === 0) {
        bot.chat("附近没有找到合适的固体方块，移动一下寻找。");
        // 随机方向探索寻找固体方块
        const directions = ['north', 'south', 'east', 'west'];
        const randomDir = directions[Math.floor(Math.random() * directions.length)];
        await exploreUntil(bot, randomDir, 64, () => {
            const found = bot.findNearbyBlocks('grass_block', 32, 10)
                .concat(bot.findNearbyBlocks('dirt', 32, 10))
                .concat(bot.findNearbyBlocks('stone', 32, 10));
            // 过滤掉太近的方块
            const filtered = found.filter(block => {
                const dx = block.x - botPos.x;
                const dz = block.z - botPos.z;
                return dx * dx + dz * dz >= 4;
            });
            return filtered.length > 0;
        });
        // 重新获取固体方块
        solidBlocks = bot.findNearbyBlocks('grass_block', 32, 10)
            .concat(bot.findNearbyBlocks('dirt', 32, 10))
            .concat(bot.findNearbyBlocks('stone', 32, 10))
            .filter(block => {
                const dx = block.x - botPos.x;
                const dz = block.z - botPos.z;
                return dx * dx + dz * dz >= 4;
            });
        if (solidBlocks.length === 0) {
            throw new Error("探索后仍未找到合适的固体方块放置工作台");
        }
    }
    
    // 选择第一个固体方块，取其上方一格作为放置位置
    const baseBlock = solidBlocks[0];
    const placePos = { x: baseBlock.x, y: baseBlock.y + 1, z: baseBlock.z };
    
    bot.chat(`将在位置 ${placePos.x}, ${placePos.y}, ${placePos.z} 放置工作台。`);
    
    // 3. 放置工作台
    await placeItem(bot, 'crafting_table', placePos);
    bot.chat("工作台放置完成！");
}