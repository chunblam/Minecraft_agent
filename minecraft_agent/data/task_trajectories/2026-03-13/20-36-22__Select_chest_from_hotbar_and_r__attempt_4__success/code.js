async function placeChest(bot) {
    // 1. 检查背包是否有箱子
    let chest = bot.inventory.items().find(item => item.name === 'chest');
    if (!chest) {
        bot.chat('背包中没有箱子，需要合成。');
        // 检查木板数量
        let planks = bot.inventory.items().find(item => item.name.includes('planks'));
        if (!planks || planks.count < 8) {
            bot.chat(`木板不足8个，当前有 ${planks ? planks.count : 0} 个，需要收集或合成。`);
            // 收集原木并合成木板（使用已有技能）
            await collectWoodLogs(bot, { logType: 'spruce_log', targetCount: 2 });
            await craftItem(bot, 'spruce_planks', 8);
            bot.chat('已合成8个云杉木板。');
        } else {
            bot.chat(`已有 ${planks.count} 个 ${planks.name}，足够合成箱子。`);
        }
        // 合成箱子
        bot.chat('正在合成箱子...');
        await craftItem(bot, 'chest', 1);
        bot.chat('箱子合成完成。');
        chest = bot.inventory.items().find(item => item.name === 'chest');
        if (!chest) {
            throw new Error('合成箱子后仍未找到箱子');
        }
    } else {
        bot.chat('背包中已有箱子，准备放置。');
    }

    // 2. 装备箱子到主手
    await equipItem(bot, 'chest', 'hand');
    bot.chat('已装备箱子。');

    // 3. 寻找合适的放置位置
    // 先找附近的固体方块（如 grass_block、dirt、stone）
    const solidBlocks = ['grass_block', 'dirt', 'stone', 'podzol', 'coarse_dirt'];
    let foundBlock = null;
    for (const blockName of solidBlocks) {
        const blocks = bot.findNearbyBlocks(blockName, 32, 10);
        if (blocks.length > 0) {
            // 选择第一个方块，检查其上方是否是空气且离 bot 足够远
            for (const block of blocks) {
                const abovePos = { x: block.x, y: block.y + 1, z: block.z };
                const botPos = bot.entity.position;
                const distance = Math.sqrt((abovePos.x - botPos.x) ** 2 + (abovePos.y - botPos.y) ** 2 + (abovePos.z - botPos.z) ** 2);
                if (distance >= 2) {
                    // 检查上方是否是空气（简单假设，实际需要更精确检查，但 placeItem 会验证）
                    foundBlock = block;
                    break;
                }
            }
            if (foundBlock) break;
        }
    }
    if (!foundBlock) {
        // 如果没有找到合适的方块，则向随机方向探索直到找到
        bot.chat('附近没有合适的放置位置，开始探索寻找地面...');
        const directions = ['north', 'south', 'east', 'west'];
        const randomDir = directions[Math.floor(Math.random() * directions.length)];
        await exploreUntil(bot, randomDir, 64, () => {
            const blocks = bot.findNearbyBlocks('grass_block', 16, 5);
            return blocks.length > 0;
        });
        // 重新查找
        const blocks = bot.findNearbyBlocks('grass_block', 32, 10);
        if (blocks.length === 0) {
            throw new Error('探索后仍未找到合适的地面放置箱子');
        }
        foundBlock = blocks[0];
    }
    const placePos = { x: foundBlock.x, y: foundBlock.y + 1, z: foundBlock.z };
    bot.chat(`找到放置位置：${placePos.x}, ${placePos.y}, ${placePos.z}（在 ${foundBlock.name} 上方）`);

    // 4. 移动到放置位置附近（距离 2 格）
    await moveToPosition(bot, placePos.x, placePos.y, placePos.z, 2);
    bot.chat('已到达放置位置附近。');

    // 5. 放置箱子
    bot.chat(`正在放置箱子...`);
    await placeItem(bot, 'chest', placePos);
    bot.chat('箱子放置完成！');
}