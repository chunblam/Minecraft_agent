async function breakOneLog(bot, params = {}) { 
    const logType = params.logType || 'oak_log';
    // 1. 寻找附近的原木
    const logs = bot.findNearbyBlocks(logType, 32, 10);
    if (logs.length === 0) {
        bot.chat(`附近没有找到 ${logType} 原木，开始探索寻找。`);
        // 随机选择一个方向探索
        const directions = ['north', 'south', 'east', 'west'];
        const randomDir = directions[Math.floor(Math.random() * directions.length)];
        await exploreUntil(bot, randomDir, 64, () => {
            const foundLogs = bot.findNearbyBlocks(logType, 32, 10);
            return foundLogs.length > 0;
        });
    }
    // 重新获取原木位置
    const logsAfter = bot.findNearbyBlocks(logType, 32, 10);
    if (logsAfter.length === 0) {
        throw new Error(`探索后仍未找到 ${logType} 原木`);
    }
    const targetLog = logsAfter[0];
    bot.chat(`找到 ${logType} 原木在 ${targetLog.x}, ${targetLog.y}, ${targetLog.z}`);

    // 2. 移动到原木附近（距离2格以内）
    await moveToPosition(bot, targetLog.x, targetLog.y, targetLog.z, 2);

    // 3. 破坏1个原木（mineBlock会自动使用空手）
    await mineBlock(bot, logType, 1);

    // 4. 捡起掉落的物品
    await pickupNearbyItems(bot);
    bot.chat(`成功破坏1个 ${logType} 原木并捡起掉落物。`);
}