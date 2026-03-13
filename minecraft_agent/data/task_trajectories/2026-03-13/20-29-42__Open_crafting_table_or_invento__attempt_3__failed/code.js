async function arrangePlanksInCraftingGrid(bot) {
    // 1. 检查背包木板
    const planks = bot.inventory.items().find(item => item.name === "spruce_planks");
    if (!planks || planks.count < 4) {
        throw new Error("需要至少4个云杉木板来合成工作台");
    }
    bot.chat("背包中有足够的云杉木板。");

    // 2. 寻找附近的工作台
    const craftingTables = bot.findNearbyBlocks("crafting_table", 32, 10);
    if (craftingTables.length > 0) {
        bot.chat("发现附近有工作台，前往使用。");
        const tablePos = craftingTables[0];
        await moveToPosition(bot, tablePos.x, tablePos.y, tablePos.z, 2);
        await activateNearestBlock(bot, "crafting_table");
        bot.chat("工作台已打开，可以排列云杉木板成3x3图案（中心留空）。");
        return;
    }

    // 3. 合成工作台
    bot.chat("附近没有工作台，合成一个工作台。");
    await craftItem(bot, "crafting_table", 1);

    // 4. 放置工作台
    // 寻找一个安全的放置位置：附近的地面方块上方
    const groundBlocks = bot.findNearbyBlocks("dirt", 5, 10); // 用 dirt 作为地面
    if (groundBlocks.length === 0) {
        // 如果没有 dirt，用 stone
        groundBlocks.push(...bot.findNearbyBlocks("stone", 5, 10));
    }
    if (groundBlocks.length === 0) {
        throw new Error("找不到合适的地面放置工作台");
    }
    const ground = groundBlocks[0];
    const placePos = { x: ground.x, y: ground.y + 1, z: ground.z }; // 地面上方一格
    bot.chat(`在工作台放置位置 ${placePos.x}, ${placePos.y}, ${placePos.z}`);
    await placeItem(bot, "crafting_table", placePos);

    // 5. 移动到工作台旁边
    await moveToPosition(bot, placePos.x, placePos.y, placePos.z, 2);

    // 6. 激活工作台
    await activateNearestBlock(bot, "crafting_table");

    // 7. 报告完成
    bot.chat("工作台已打开，云杉木板已准备好排列成3x3图案（中心留空）。");
}