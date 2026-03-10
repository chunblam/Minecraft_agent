async function moveToPlayer(bot, params = {}) {
    // Get player entity position
    const player = bot.nearestEntity(entity => entity.type === 'player');
    if (!player) {
        bot.chat("I don't see any players nearby");
        return;
    }

    const targetPos = player.position;
    bot.chat(`Moving to player at ${targetPos.x}, ${targetPos.y}, ${targetPos.z}`);
    
    // Move to player with minimum distance of 1 block
    await bot.pathfinder.goto(new GoalNear(targetPos.x, targetPos.y, targetPos.z, 1));
    
    bot.chat("Arrived at player's location");
}