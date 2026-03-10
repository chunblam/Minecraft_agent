async function craftWoodenTool(bot, params = {}) {
    const { toolType = "axe", logType = "acacia_log", quantity = 1 } = params;
    
    // Validate tool type
    const validTools = ["axe", "pickaxe", "shovel", "hoe", "sword"];
    if (!validTools.includes(toolType)) {
        throw new Error(`Invalid tool type: ${toolType}. Must be one of: ${validTools.join(', ')}`);
    }
    
    // Calculate required materials
    const materials = {
        "axe": { logs: 3, planks: 3, sticks: 2 },
        "pickaxe": { logs: 3, planks: 3, sticks: 2 },
        "shovel": { logs: 1, planks: 1, sticks: 2 },
        "hoe": { logs: 2, planks: 2, sticks: 2 },
        "sword": { logs: 2, planks: 2, sticks: 1 }
    };
    
    const requiredLogs = materials[toolType].logs * quantity;
    const requiredPlanks = materials[toolType].planks * quantity;
    const requiredSticks = materials[toolType].sticks * quantity;
    
    // Collect logs if needed
    const currentLogs = bot.inventory.items().filter(item => item.name === logType).reduce((sum, item) => sum + item.count, 0);
    if (currentLogs < requiredLogs) {
        const toCollect = requiredLogs - currentLogs;
        bot.chat(`Need ${toCollect} more ${logType}`);
        try {
            await mineBlock(bot, logType, toCollect);
        } catch (err) {
            bot.chat("Couldn't pathfind to logs, exploring nearby...");
            const directions = ["north", "south", "east", "west"];
            const randomDir = directions[Math.floor(Math.random() * directions.length)];
            await exploreUntil(bot, randomDir, 16, () => {
                return bot.findNearbyBlocks(logType, 32).length > 0;
            });
            await mineBlock(bot, logType, toCollect);
        }
        await pickupNearbyItems(bot);
    }
    
    // Convert logs to planks
    const plankType = logType.replace('_log', '_planks');
    bot.chat(`Making ${plankType}...`);
    await craftItem(bot, plankType, Math.ceil(requiredPlanks / 4));
    
    // Craft sticks
    bot.chat("Crafting sticks...");
    await craftItem(bot, "stick", Math.ceil(requiredSticks / 4));
    
    // Craft and place crafting table if not already available
    if (!bot.inventory.items().some(item => item.name === "crafting_table")) {
        bot.chat("Crafting crafting table...");
        await craftItem(bot, "crafting_table", 1);
        bot.chat("Placing crafting table...");
        const position = bot.entity.position.offset(1, 0, 0);
        await placeItem(bot, "crafting_table", position);
    }
    
    // Craft the tool
    bot.chat(`Crafting wooden ${toolType}...`);
    await craftItem(bot, `wooden_${toolType}`, quantity);
    
    // Equip the tool
    bot.chat(`Equipping wooden ${toolType}...`);
    await equipItem(bot, `wooden_${toolType}`, "hand");
    
    bot.chat(`Wooden ${toolType} crafted and equipped!`);
}