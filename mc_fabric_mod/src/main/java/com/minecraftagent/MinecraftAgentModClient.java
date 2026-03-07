package com.minecraftagent;

import com.minecraftagent.client.ClientInputSimulator;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.api.EnvType;
import net.fabricmc.api.Environment;

/**
 * 客户端入口：注册输入模拟器，接收服务端下发的 move_to / mine_block，
 * 通过模拟 WASD 和鼠标点击操控玩家，与 Python 规划/技能紧密联动。
 */
@Environment(EnvType.CLIENT)
public class MinecraftAgentModClient implements ClientModInitializer {

    @Override
    public void onInitializeClient() {
        // 网络包类型已在 MinecraftAgentMod.onInitialize 中注册，此处仅注册客户端接收器
        ClientInputSimulator.register();
    }
}
