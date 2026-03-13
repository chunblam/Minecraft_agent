async function craftWorkbenchFromPlanks(bot) {
    // 检查是否已有工作台
    const workbench = bot.inventory.items().find(item => item.name === "crafting_table");
    if (workbench) {
        bot.chat("背包中已有工作台，任务完成。");
        return;
    }

    // 确保手持空手进行合成
    await equipItem(bot, "air", "hand");

    // 合成工作台
    bot.chat("开始用橡木木板合成工作台。");
    await craftItem(bot, "crafting_table", 1);
    bot.chat("成功合成工作台。");
}