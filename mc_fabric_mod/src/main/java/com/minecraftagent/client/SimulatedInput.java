package com.minecraftagent.client;

import net.minecraft.client.input.Input;

/**
 * 可编程的 Input，用于模拟 WASD 按键。
 * 由 ClientInputSimulator 每 tick 更新 movementForward / movementSideways。
 */
public class SimulatedInput extends Input {

    /** 朝目标移动：直接按 W（配合外部设置 player 朝向目标） */
    public void setForward() {
        movementForward = 1f;
        movementSideways = 0;
        pressingForward = true;
        pressingBack = false;
        pressingLeft = false;
        pressingRight = false;
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
