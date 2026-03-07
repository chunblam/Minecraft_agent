package com.minecraftagent.client;

import net.minecraft.client.input.Input;

/**
 * 可编程的 Input，用于模拟 WASD + 空格（跳跃）按键。
 * 由 ClientInputSimulator 每 tick 更新。
 */
public class SimulatedInput extends Input {

    /** 朝目标移动：模拟按 W 键（配合外部设置玩家朝向目标） */
    public void setForward() {
        movementForward = 1f;
        movementSideways = 0;
        pressingForward = true;
        pressingBack = false;
        pressingLeft = false;
        pressingRight = false;
    }

    /** 控制跳跃键（遇障碍物时设为 true，通过原版物理引擎自动跳跃） */
    public void setJump(boolean doJump) {
        jumping = doJump;
    }

    public void stop() {
        movementForward = 0;
        movementSideways = 0;
        pressingForward = false;
        pressingBack = false;
        pressingLeft = false;
        pressingRight = false;
        jumping = false;
        sneaking = false;
    }
}
