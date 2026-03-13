async function craftWorkbenchFromPlanks(bot) {
    // 检查是否已有工作台
    const workbench = bot.inventory.items().find(item => item.name === "crafting_table");
    if (workbench) {
        bot.chat("背包中已有工作台，任务完成。");
        return;
    }

    // 检查是否有足够的橡木木板（需要4个）
    const planks = bot.inventory.items().find(item => item.name === "oak_planks");
    if (!planks || planks.count < 4) {
        bot.chat("橡木木板不足，需要至少4个。");
        // 如果有橡木原木，可以合成木板
        const logs = bot.inventory.items().find(item => item.name === "oak_log");
        if (logs && logs.count >= 1) {
            bot.chat("用橡木原木合成橡木木板。");
            await craftItem(bot, "oak_planks", 4); // 1个原木合成4个木板
        } else {
            throw new Error("没有足够的橡木原木来合成木板。");
        }
    }

    // 合成工作台
    bot.chat("开始用橡木木板合成工作台。");
    await craftItem(bot, "crafting_table", 1);
    bot.chat("成功合成工作台。");
}