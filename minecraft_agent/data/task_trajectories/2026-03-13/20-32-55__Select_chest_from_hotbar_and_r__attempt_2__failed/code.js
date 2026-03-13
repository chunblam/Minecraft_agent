async function placeChest(bot) {
    // 1. 检查背包是否有箱子
    let chest = bot.inventory.items().find(item => item.name === 'chest');
    if (chest) {
        bot.chat('背包中已有箱子，准备放置。');
    } else {
        bot.chat('背包中没有箱子，需要合成。');
        // 2. 检查是否有木板
        let planks = bot.inventory.items().find(item => item.name.includes('planks'));
        if (!planks) {
            bot.chat('没有木板，需要从原木制作木板。');
            // 检查是否有原木
            let logs = bot.inventory.items().find(item => item.name.includes('log'));
            if (!logs) {
                bot.chat('没有原木，需要先收集原木。');
                // 使用已有技能收集原木（这里假设是云杉原木，因为背包有 spruce_log）
                await collectWoodLogs(bot, { logType: 'spruce_log', targetCount: 1 });
            } else {
                bot.chat(`有 ${logs.count} 个 ${logs.name}，可以制作木板。`);
            }
            // 重新获取原木数量
            logs = bot.inventory.items().find(item => item.name.includes('log'));
            if (!logs || logs.count < 1) {
                throw new Error('没有足够原木制作木板');
            }
            // 3. 放置工作台（因为附近没有工作台）
            bot.chat('放置工作台以合成木板。');
            // 找一个放置位置：当前位置附近的地面
            const pos = bot.entity.position;
            const placePos = { x: Math.floor(pos.x) + 2, y: Math.floor(pos.y), z: Math.floor(pos.z) };
            // 确保是固体方块上方的空气格（这里假设地面是固体）
            await placeItem(bot, 'crafting_table', placePos);
            bot.chat('工作台已放置。');
            // 4. 合成木板（每个原木合成4个木板）
            // 使用控制原语 craftItem，它会自动使用附近的工作台
            await craftItem(bot, 'spruce_planks', logs.count * 4);
            bot.chat(`已合成 ${logs.count * 4} 个云杉木板。`);
        } else {
            bot.chat(`已有 ${planks.count} 个木板，足够合成箱子。`);
        }
        // 5. 合成箱子（需要8个木板）
        // 重新检查木板数量
        planks = bot.inventory.items().find(item => item.name.includes('planks'));
        if (!planks || planks.count < 8) {
            throw new Error('木板不足8个，无法合成箱子');
        }
        bot.chat('正在合成箱子...');
        await craftItem(bot, 'chest', 1);
        bot.chat('箱子合成完成。');
    }
    // 6. 装备箱子到主手
    await equipItem(bot, 'chest', 'hand');
    bot.chat('已装备箱子。');
    // 7. 放置箱子
    const pos = bot.entity.position;
    // 选择一个放置位置：当前位置旁边2格的地面（确保是固体方块上方的空气格）
    const chestPos = { x: Math.floor(pos.x) + 2, y: Math.floor(pos.y), z: Math.floor(pos.z) };
    bot.chat(`在位置 ${chestPos.x}, ${chestPos.y}, ${chestPos.z} 放置箱子。`);
    await placeItem(bot, 'chest', chestPos);
    bot.chat('箱子放置完成！');
}