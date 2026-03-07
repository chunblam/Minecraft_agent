package com.minecraftagent.action;

import net.minecraft.server.network.ServerPlayerEntity;

/**
 * 存储当前与 Agent 对话的玩家（发送 chat 的玩家）。
 * 所有任务执行动作均作用于该玩家（操控主角模式）。
 */
public final class PlayerContext {

    private static volatile ServerPlayerEntity currentPlayer;

    public static void setCurrentPlayer(ServerPlayerEntity player) {
        currentPlayer = player;
    }

    public static ServerPlayerEntity getCurrentPlayer() {
        return currentPlayer;
    }

    /** 获取当前玩家，若不存在则取服务器中第一个在线玩家（单人生存） */
    public static ServerPlayerEntity getCurrentOrFirst(net.minecraft.server.MinecraftServer server) {
        if (currentPlayer != null && !currentPlayer.isDisconnected()) {
            return currentPlayer;
        }
        var list = server.getPlayerManager().getPlayerList();
        return list.isEmpty() ? null : list.get(0);
    }
}
