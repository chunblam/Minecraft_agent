package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.minecraft.network.RegistryByteBuf;
import net.minecraft.network.codec.PacketCodec;
import net.minecraft.network.codec.PacketCodecs;
import net.minecraft.network.packet.CustomPayload;
import net.minecraft.util.Identifier;

/**
 * 客户端 → 服务端：move_to 执行过程中请求根据当前游戏状态重新规划路径。
 * 服务端用当前坐标与最终目标重新寻路并下发新路径点，实现「运行中结合游戏状态更新最优路径」。
 */
public record PathUpdateRequestPayload(
        String requestId,
        double currentX,
        double currentY,
        double currentZ,
        double finalTargetX,
        double finalTargetY,
        double finalTargetZ
) implements CustomPayload {

    public static final CustomPayload.Id<PathUpdateRequestPayload> ID =
            new CustomPayload.Id<>(Identifier.of(MinecraftAgentMod.MOD_ID, "path_update_request"));

    public static final PacketCodec<RegistryByteBuf, PathUpdateRequestPayload> CODEC = PacketCodec.of(
            PathUpdateRequestPayload::encode,
            PathUpdateRequestPayload::decode
    );

    /** ValueFirstEncoder: (payload, buf) -> void */
    private static void encode(PathUpdateRequestPayload p, RegistryByteBuf buf) {
        PacketCodecs.STRING.encode(buf, p.requestId());
        PacketCodecs.DOUBLE.encode(buf, p.currentX());
        PacketCodecs.DOUBLE.encode(buf, p.currentY());
        PacketCodecs.DOUBLE.encode(buf, p.currentZ());
        PacketCodecs.DOUBLE.encode(buf, p.finalTargetX());
        PacketCodecs.DOUBLE.encode(buf, p.finalTargetY());
        PacketCodecs.DOUBLE.encode(buf, p.finalTargetZ());
    }

    private static PathUpdateRequestPayload decode(RegistryByteBuf buf) {
        return new PathUpdateRequestPayload(
                PacketCodecs.STRING.decode(buf),
                PacketCodecs.DOUBLE.decode(buf),
                PacketCodecs.DOUBLE.decode(buf),
                PacketCodecs.DOUBLE.decode(buf),
                PacketCodecs.DOUBLE.decode(buf),
                PacketCodecs.DOUBLE.decode(buf),
                PacketCodecs.DOUBLE.decode(buf)
        );
    }

    @Override
    public Id<? extends CustomPayload> getId() {
        return ID;
    }
}
