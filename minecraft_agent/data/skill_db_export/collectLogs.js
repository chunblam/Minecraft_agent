async function collectLogs(bot, params = {}) {
    const { logType = "oak_log", quantity = 5 } = params;
    
    // Check current inventory
    const logsInInventory = bot.inventory.items().filter(item => item.name === logType).reduce((sum, item) => sum + item.count, 0);
    if (logsInInventory >= quantity) {
        bot.chat(`Already have enough ${logType}s`);
        return;
    }

    bot.chat(`Starting to collect ${logType}s`);
    const logsNeeded = quantity - logsInInventory;
    
    // Try mining first
    try {
        await mineBlock(bot, logType, logsNeeded);
    } catch (err) {
        bot.chat(`Couldn't pathfind to ${logType}s, trying to explore nearby`);
        // If mining fails, explore north/south/east/west randomly
        const directions = ["north", "south", "east", "west"];
        const randomDir = directions[Math.floor(Math.random() * directions.length)];
        await exploreUntil(bot, randomDir, 16, () => {
            return bot.findNearbyBlocks(logType, 32).length > 0;
        });
        // Try mining again after exploration
        await mineBlock(bot, logType, logsNeeded);
    }
    
    await pickupNearbyItems(bot);
    
    // Verify final count
    const finalCount = bot.inventory.items().filter(item => item.name === logType).reduce((sum, item) => sum + item.count, 0);
    if (finalCount >= quantity) {
        bot.chat(`Successfully collected ${finalCount} ${logType}s`);
    } else {
        bot.chat(`Only collected ${finalCount} ${logType}s, need more`);
    }
}