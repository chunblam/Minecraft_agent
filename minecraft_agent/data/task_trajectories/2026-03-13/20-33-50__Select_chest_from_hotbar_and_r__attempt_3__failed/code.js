async function placeChest(bot) {
    // 1. 检查背包是否有箱子
    let chest = bot.inventory.items().find(item => item.name === 'chest');
    if (chest) {
        bot.chat('背包中已有箱子，准备放置。');
    } else {
        bot.chat('背包中没有箱子，需要合成。');
        // 2. 检查木板数量（任何种类）
        let planks = bot.inventory.items().find(item => item.name.includes('planks'));
        if (!planks || planks.count < 8) {
            // 实际上背包已有20个云杉木板，但以防万一
            bot.chat(`木板不足8个，当前有 ${planks ? planks.count : 0} 个，需要收集或合成。`);
            // 由于背包已有足够木板，此分支不应执行，但保留处理逻辑
            // 先尝试收集原木并合成木板（使用已有技能）
            await collectWoodLogs(bot, { logType: 'spruce_log', targetCount: 1 });
            // 合成木板：每个原木得4个木板，需要至少2个原木才能有8个木板
            await craftItem(bot, 'spruce_planks', 8);
            bot.chat('已合成8个云杉木板。');
        } else {
            bot.chat(`已有 ${planks.count} 个 ${planks.name}，足够合成箱子。`);
        }
        // 3. 合成箱子（需要8个木板）
        // 重新检查木板
        planks = bot.inventory.items().find(item => item.name.includes('planks'));
        if (!planks || planks.count < 8) {
            throw new Error(`木板不足8个，无法合成箱子，当前有 ${planks ? planks.count : 0} 个`);
        }
        bot.chat('正在合成箱子...');
        // 使用控制原语 craftItem，它会自动使用附近的工作台（Nearby blocks 中已有 crafting_table）
        await craftItem(bot, 'chest', 1);
        bot.chat('箱子合成完成。');
    }
    // 4. 装备箱子到主手
    await equipItem(bot, 'chest', 'hand');
    bot.chat('已装备箱子。');
    // 5. 放置箱子
    const pos = bot.entity.position;
    // 选择一个放置位置：当前位置旁边2格的地面（确保是固体方块上方的空气格）
    // 先找到脚下的固体方块（假设是地面）
    const groundX = Math.floor(pos.x);
    const groundY = Math.floor(pos.y) - 1;
    const groundZ = Math.floor(pos.z);
    // 箱子放在地面旁边的空气格（例如 x+2, 同一地面高度+1）
    const chestPos = { x: groundX + 2, y: groundY + 1, z: groundZ };
    bot.chat(`在位置 ${chestPos.x}, ${chestPos.y}, ${chestPos.z} 放置箱子。`);
    await placeItem(bot, 'chest', chestPos);
    bot.chat('箱子放置完成！');
}