async function collectDirtBlocks(bot, params = {}) {
    const targetCount = params.targetCount || 20;
    const craftShovelIfMissing = params.craftShovelIfMissing !== false;
    
    // Check current dirt count
    const dirtCount = bot.inventory.items().filter(item => item.name === "dirt").reduce((sum, item) => sum + item.count, 0);
    if (dirtCount >= targetCount) {
        bot.chat(`Already have ${dirtCount} dirt blocks (needed ${targetCount})`);
        return dirtCount;
    }

    // Check for shovel
    const hasShovel = bot.inventory.items().some(item => item.name.includes("shovel"));
    
    if (!hasShovel && craftShovelIfMissing) {
        bot.chat("Crafting wooden shovel for faster digging...");
        await craftItem(bot, "wooden_shovel", 1);
        await equipItem(bot, "wooden_shovel", "hand");
    }

    // Calculate needed dirt
    const neededDirt = targetCount - dirtCount;
    bot.chat(`Mining ${neededDirt} dirt blocks...`);
    
    // Mine dirt blocks
    await mineBlock(bot, "dirt", neededDirt);
    
    // Collect any dropped items
    await pickupNearbyItems(bot);
    
    // Verify final count
    const finalDirt = bot.inventory.items().filter(item => item.name === "dirt").reduce((sum, item) => sum + item.count, 0);
    if (finalDirt >= targetCount) {
        bot.chat(`Successfully collected ${finalDirt} dirt blocks`);
    } else {
        bot.chat(`Collected ${finalDirt} dirt blocks (needed ${targetCount})`);
    }
    return finalDirt;
}