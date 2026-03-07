package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.minecraft.network.RegistryByteBuf;
import net.minecraft.network.codec.PacketCodec;
import net.minecraft.network.codec.PacketCodecs;
import net.minecraft.network.packet.CustomPayload;
import net.minecraft.util.Identifier;

/**
 * 服务端 → 客户端：请求客户端模拟按键/鼠标输入执行动作。
 * 与 Python 规划、技能库紧密联动，由服务端根据 action 指令下发。
 */
public record AgentInputPayload(
        String requestId,
        String actionType,
        double x,
        double y,
        double z
) implements CustomPayload {

    public static final CustomPayload.Id<AgentInputPayload> ID =
            new CustomPayload.Id<>(Identifier.of(MinecraftAgentMod.MOD_ID, "agent_input"));

    public static final PacketCodec<RegistryByteBuf, AgentInputPayload> CODEC = PacketCodec.tuple(
            PacketCodecs.STRING, AgentInputPayload::requestId,
            PacketCodecs.STRING, AgentInputPayload::actionType,
            PacketCodecs.DOUBLE, AgentInputPayload::x,
            PacketCodecs.DOUBLE, AgentInputPayload::y,
            PacketCodecs.DOUBLE, AgentInputPayload::z,
            AgentInputPayload::new
    );

    @Override
    public Id<? extends CustomPayload> getId() {
        return ID;
    }
}
