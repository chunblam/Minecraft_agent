async function arrangePlanksInCraftingGrid(bot) {
    // 1. 检查背包云杉木板
    const planks = bot.inventory.items().find(item => item.name === "spruce_planks");
    if (!planks || planks.count < 4) {
        throw new Error("需要至少4个云杉木板来排列3x3图案");
    }
    bot.chat(`背包中有${planks.count}个云杉木板，足够排列。`);

    // 2. 寻找附近的工作台（利用 nearby_blocks 信息）
    const craftingTables = bot.findNearbyBlocks("crafting_table", 32, 5);
    if (craftingTables.length > 0) {
        const tablePos = craftingTables[0];
        bot.chat(`发现工作台在 ${tablePos.x}, ${tablePos.y}, ${tablePos.z}，前往使用。`);
        await moveToPosition(bot, tablePos.x, tablePos.y, tablePos.z, 2);
        await activateNearestBlock(bot, "crafting_table");
        bot.chat("工作台已打开，可以排列云杉木板成3x3图案（中心留空）。");
        return;
    }

    // 3. 检查背包是否有工作台
    const tableItem = bot.inventory.items().find(item => item.name === "crafting_table");
    if (!tableItem) {
        // 理论上不会发生，因为背包已有 crafting_tablex1
        bot.chat("背包没有工作台，合成一个。");
        await craftItem(bot, "crafting_table", 1);
    } else {
        bot.chat(`背包中有工作台（${tableItem.count}个），准备放置。`);
    }

    // 4. 寻找合适地面放置工作台
    const groundTypes = ["grass_block", "dirt", "stone", "coarse_dirt", "podzol"];
    let ground = null;
    for (const type of groundTypes) {
        const blocks = bot.findNearbyBlocks(type, 32, 10);
        if (blocks.length > 0) {
            ground = blocks[0];
            break;
        }
    }
    if (!ground) {
        throw new Error("找不到合适的地面放置工作台");
    }
    // 放置位置：地面上方一格
    const placePos = { x: ground.x, y: ground.y + 1, z: ground.z };
    // 检查 bot 当前位置，若水平距离太近则调整放置位置
    const botPos = bot.entity.position;
    const dx = Math.abs(botPos.x - placePos.x);
    const dz = Math.abs(botPos.z - placePos.z);
    if (dx < 2 && dz < 2) {
        // 向随机方向偏移 3 格
        const dir = Math.random() < 0.5 ? 1 : -1;
        placePos.x += dir * 3;
        placePos.z += dir * 3;
        bot.chat(`调整工作台放置位置以避免重叠，新位置 ${placePos.x}, ${placePos.y}, ${placePos.z}`);
    }
    bot.chat(`将工作台放置在 ${placePos.x}, ${placePos.y}, ${placePos.z}`);
    await placeItem(bot, "crafting_table", placePos);

    // 5. 移动到工作台旁边（保持水平距离 2 格，同一高度）
    // 计算目标点：从放置位置向 x 方向移动 2 格
    const targetX = placePos.x + 2;
    const targetZ = placePos.z;
    const targetY = placePos.y;
    await moveToPosition(bot, targetX, targetY, targetZ, 2);

    // 6. 激活工作台
    await activateNearestBlock(bot, "crafting_table");
    bot.chat("工作台已打开，云杉木板已准备好排列成3x3图案（中心留空）。");
}