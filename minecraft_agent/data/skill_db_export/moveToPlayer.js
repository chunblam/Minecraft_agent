async function moveToPlayer(bot, params = {}) { 
    const playerEntity = bot.nearestEntity(entity => entity.name === 'player' && entity !== bot.entity);
    if (!playerEntity) {
        bot.chat('未找到玩家，尝试探索寻找。');
        const directions = ['north', 'south', 'east', 'west'];
        const randomDir = directions[Math.floor(Math.random() * directions.length)];
        await exploreUntil(bot, randomDir, 100, () => {
            const found = bot.nearestEntity(entity => entity.name === 'player' && entity !== bot.entity);
            return found !== null;
        });
        const newPlayer = bot.nearestEntity(entity => entity.name === 'player' && entity !== bot.entity);
        if (!newPlayer) {
            throw new Error('探索后仍未找到玩家');
        }
        const targetPos = newPlayer.position;
        bot.chat(`找到玩家在 ${targetPos.x.toFixed(1)}, ${targetPos.y.toFixed(1)}, ${targetPos.z.toFixed(1)}`);
        await moveToPosition(bot, targetPos.x, targetPos.y, targetPos.z, 2);
        bot.chat('已到达玩家身边！');
        return;
    }
    const targetPos = playerEntity.position;
    bot.chat(`玩家在 ${targetPos.x.toFixed(1)}, ${targetPos.y.toFixed(1)}, ${targetPos.z.toFixed(1)}，正在移动过去。`);
    await moveToPosition(bot, targetPos.x, targetPos.y, targetPos.z, 2);
    bot.chat('已到达玩家身边！');
}