package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.minecraft.network.RegistryByteBuf;
import net.minecraft.network.codec.PacketCodec;
import net.minecraft.network.codec.PacketCodecs;
import net.minecraft.network.packet.CustomPayload;
import net.minecraft.util.Identifier;

/**
 * 客户端 → 服务端：客户端完成输入模拟后回报结果。
 */
public record AgentActionCompletePayload(
        String requestId,
        boolean success,
        String observation
) implements CustomPayload {

    public static final CustomPayload.Id<AgentActionCompletePayload> ID =
            new CustomPayload.Id<>(Identifier.of(MinecraftAgentMod.MOD_ID, "agent_action_complete"));

    public static final PacketCodec<RegistryByteBuf, AgentActionCompletePayload> CODEC = PacketCodec.tuple(
            PacketCodecs.STRING, AgentActionCompletePayload::requestId,
            PacketCodecs.BOOL, AgentActionCompletePayload::success,
            PacketCodecs.STRING, AgentActionCompletePayload::observation,
            AgentActionCompletePayload::new
    );

    @Override
    public Id<? extends CustomPayload> getId() {
        return ID;
    }
}
