package com.minecraftagent.network;

import com.minecraftagent.MinecraftAgentMod;
import net.minecraft.network.RegistryByteBuf;
import net.minecraft.network.codec.PacketCodec;
import net.minecraft.network.codec.PacketCodecs;
import net.minecraft.network.packet.CustomPayload;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.Vec3d;

import java.util.ArrayList;
import java.util.List;

/**
 * 服务端 → 客户端：带路径点的移动请求（寻路结果），客户端按顺序依次前往各路径点。
 */
public record AgentPathPayload(
        String requestId,
        String actionType,
        List<Vec3d> waypoints
) implements CustomPayload {

    public static final CustomPayload.Id<AgentPathPayload> ID =
            new CustomPayload.Id<>(Identifier.of(MinecraftAgentMod.MOD_ID, "agent_path"));

    private static final PacketCodec<RegistryByteBuf, List<Vec3d>> LIST_VEC3D = new PacketCodec<>() {
        @Override
        public List<Vec3d> decode(RegistryByteBuf buf) {
            int n = PacketCodecs.VAR_INT.decode(buf);
            List<Vec3d> list = new ArrayList<>(n);
            for (int i = 0; i < n; i++) {
                double x = PacketCodecs.DOUBLE.decode(buf);
                double y = PacketCodecs.DOUBLE.decode(buf);
                double z = PacketCodecs.DOUBLE.decode(buf);
                list.add(new Vec3d(x, y, z));
            }
            return list;
        }

        @Override
        public void encode(RegistryByteBuf buf, List<Vec3d> value) {
            PacketCodecs.VAR_INT.encode(buf, value.size());
            for (Vec3d v : value) {
                PacketCodecs.DOUBLE.encode(buf, v.x);
                PacketCodecs.DOUBLE.encode(buf, v.y);
                PacketCodecs.DOUBLE.encode(buf, v.z);
            }
        }
    };

    public static final PacketCodec<RegistryByteBuf, AgentPathPayload> CODEC = PacketCodec.tuple(
            PacketCodecs.STRING, AgentPathPayload::requestId,
            PacketCodecs.STRING, AgentPathPayload::actionType,
            LIST_VEC3D, AgentPathPayload::waypoints,
            AgentPathPayload::new
    );

    @Override
    public Id<? extends CustomPayload> getId() {
        return ID;
    }
}
