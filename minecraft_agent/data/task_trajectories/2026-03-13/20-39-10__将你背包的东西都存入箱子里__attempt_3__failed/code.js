async function storeAllItemsInChest(bot) {
    // 1. 放置箱子（使用已有技能）
    bot.chat('开始放置箱子...');
    await placeChest(bot);
    bot.chat('箱子放置完成。');

    // 2. 找到最近的箱子并打开
    const chestBlocks = bot.findNearbyBlocks('chest', 32, 5);
    if (chestBlocks.length === 0) {
        throw new Error('未找到箱子，放置可能失败');
    }
    const chestPos = chestBlocks[0];
    await moveToPosition(bot, chestPos.x, chestPos.y, chestPos.z, 2);
    bot.chat('移动到箱子旁边，准备打开。');
    // 激活箱子（右键打开）
    await activateNearestBlock(bot, 'chest');
    bot.chat('箱子已打开。');

    // 3. 将背包所有物品存入箱子
    const chestWindow = bot.currentWindow;
    if (!chestWindow) {
        throw new Error('未打开箱子窗口');
    }
    const inventorySlots = bot.inventory.slots; // 背包槽位（0-35）
    for (let i = 0; i < inventorySlots.length; i++) {
        const item = inventorySlots[i];
        if (item && item.name !== 'air') {
            // 跳过手持槽位（8-35是背包，0-7是快捷栏，但都算背包）
            // 将物品移动到箱子槽位（箱子槽位是0-26）
            const chestSlot = chestWindow.firstEmptySlot();
            if (chestSlot === -1) {
                bot.chat('箱子已满，无法存入更多物品。');
                break;
            }
            await bot.transfer(item.slot, chestSlot, item.count);
            bot.chat(`存入 ${item.count} 个 ${item.name}`);
        }
    }

    // 4. 关闭箱子
    await bot.closeWindow(chestWindow);
    bot.chat('所有物品已存入箱子，背包已清空。');
}