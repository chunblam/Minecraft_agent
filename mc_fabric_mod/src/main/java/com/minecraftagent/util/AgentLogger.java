package com.minecraftagent.util;

import net.fabricmc.loader.api.FabricLoader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;

/**
 * Minecraft Agent 专属日志工具（AgentLogger）。
 *
 * 在 Minecraft 标准日志（logs/latest.log）之外，
 * 额外写出一个只含 Agent 相关信息的独立日志文件：
 *
 *   <游戏目录>/logs/mc_agent_latest.log    ← 本次启动的完整记录（每次启动覆盖）
 *   <游戏目录>/logs/mc_agent_YYYY-MM-DD_HH-mm-ss.log ← 带时间戳的归档副本
 *
 * 两个文件内容完全相同，区别：
 *   - latest.log 每次启动都清空重写，方便实时查看
 *   - 带时间戳的文件永久保留，方便测试后回溯
 *
 * 使用方式（在其他 Java 类中）：
 *   AgentLogger.info("ActionExecutor", "执行行动: craft_item");
 *   AgentLogger.warn("WebSocket", "连接断开，等待重连");
 *   AgentLogger.error("ActionExecutor", "合成失败: " + e.getMessage());
 *   AgentLogger.action("craft_item", params, observation);   // 专用行动日志
 *
 * 初始化：在 MinecraftAgentMod.onInitialize() 中调用 AgentLogger.init()
 * 关闭：  在 SERVER_STOPPING 事件中调用 AgentLogger.close()
 */
public class AgentLogger {

    private static final Logger CONSOLE = LoggerFactory.getLogger("AgentLogger");
    private static final DateTimeFormatter TIMESTAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS");
    private static final DateTimeFormatter FILE_TS   = DateTimeFormatter.ofPattern("yyyy-MM-dd_HH-mm-ss");

    // 同时写入两个文件：latest（覆盖）+ 归档（追加）
    private static PrintWriter latestWriter  = null;
    private static PrintWriter archiveWriter = null;

    private static boolean initialized = false;

    // ── 初始化 ─────────────────────────────────────────────────────────────

    /**
     * 初始化日志文件。在服务器启动时调用一次。
     *
     * 写入位置：<Minecraft游戏目录>/logs/
     *   Windows: %APPDATA%\.minecraft\logs\
     *   开发环境(run/): <项目>/run/logs/
     */
    public static synchronized void init() {
        if (initialized) return;

        try {
            Path logDir = FabricLoader.getInstance().getGameDir().resolve("logs");
            Files.createDirectories(logDir);

            // ① latest 文件（每次启动覆盖，append=false）
            Path latestPath = logDir.resolve("mc_agent_latest.log");
            latestWriter = new PrintWriter(
                    new BufferedWriter(new FileWriter(latestPath.toFile(), false)), true);

            // ② 归档文件（带时间戳，append=true 防止同秒冲突）
            String archiveName = "mc_agent_" + LocalDateTime.now().format(FILE_TS) + ".log";
            Path archivePath = logDir.resolve(archiveName);
            archiveWriter = new PrintWriter(
                    new BufferedWriter(new FileWriter(archivePath.toFile(), false)), true);

            initialized = true;

            // 写入文件头
            String header = "=".repeat(60);
            writeLine("INFO", "AgentLogger", header);
            writeLine("INFO", "AgentLogger", "  Minecraft Agent 日志启动");
            writeLine("INFO", "AgentLogger", "  时间: " + LocalDateTime.now().format(TIMESTAMP));
            writeLine("INFO", "AgentLogger", "  latest 文件: " + latestPath.toAbsolutePath());
            writeLine("INFO", "AgentLogger", "  归档文件: " + archivePath.toAbsolutePath());
            writeLine("INFO", "AgentLogger", header);

            CONSOLE.info("AgentLogger 初始化成功 → {}", latestPath.toAbsolutePath());

        } catch (IOException e) {
            CONSOLE.error("AgentLogger 初始化失败: {}", e.getMessage());
        }
    }

    /**
     * 关闭日志文件。在服务器停止时调用。
     */
    public static synchronized void close() {
        if (!initialized) return;
        writeLine("INFO", "AgentLogger", "=== Minecraft Agent 日志关闭 ===");
        if (latestWriter  != null) latestWriter.close();
        if (archiveWriter != null) archiveWriter.close();
        initialized = false;
        CONSOLE.info("AgentLogger 已关闭");
    }

    // ── 公共日志方法 ───────────────────────────────────────────────────────

    /** INFO 级别日志 */
    public static void info(String module, String message) {
        writeLine("INFO ", module, message);
        CONSOLE.info("[{}] {}", module, message);
    }

    /** WARN 级别日志 */
    public static void warn(String module, String message) {
        writeLine("WARN ", module, message);
        CONSOLE.warn("[{}] {}", module, message);
    }

    /** ERROR 级别日志 */
    public static void error(String module, String message) {
        writeLine("ERROR", module, message);
        CONSOLE.error("[{}] {}", module, message);
    }

    /** DEBUG 级别日志（只写文件，不打控制台，避免刷屏） */
    public static void debug(String module, String message) {
        writeLine("DEBUG", module, message);
        // 不写控制台
    }

    /**
     * 专用行动日志：记录每次 Agent 行动的完整三元组（行动类型、参数、结果）。
     *
     * 格式示例：
     *   [ACTION] craft_item | params={"item":"diamond_sword","count":1}
     *            result=成功合成 [minecraft:diamond_sword] x1
     */
    public static void action(String actionType, Object params, String result) {
        String paramsStr = (params != null) ? params.toString() : "{}";
        writeLine("ACT  ", "Action", ">>> " + actionType + " | params=" + paramsStr);
        writeLine("ACT  ", "Action", "<<< result=" + result);
        CONSOLE.info("[ACTION] {} → {}", actionType, abbreviate(result, 80));
    }

    /**
     * 专用 WebSocket 消息日志。
     *
     * @param direction "SEND" 或 "RECV"
     * @param msgType   消息类型（如 "action"、"observation"）
     * @param summary   消息摘要（不记录完整 JSON，避免日志过大）
     */
    public static void ws(String direction, String msgType, String summary) {
        String tag = direction.equals("SEND") ? "WS>>>" : "WS<<<";
        writeLine("WS   ", tag, "[" + msgType + "] " + summary);
    }

    /**
     * 专用任务分解日志：记录子任务规划结果。
     */
    public static void plan(String task, String subtasks) {
        writeLine("PLAN ", "Planner", "任务: " + task);
        writeLine("PLAN ", "Planner", "分解: " + subtasks);
        CONSOLE.info("[PLAN] {} → {}", abbreviate(task, 40), abbreviate(subtasks, 60));
    }

    /**
     * 专用技能学习日志。
     */
    public static void skill(String skillName, String skillType) {
        writeLine("SKILL", "SkillLib", "学会新技能: [" + skillType + "] " + skillName);
        CONSOLE.info("[SKILL] 学会: {} ({})", skillName, skillType);
    }

    // ── 内部方法 ───────────────────────────────────────────────────────────

    private static synchronized void writeLine(String level, String module, String message) {
        if (!initialized) return;
        String line = String.format("[%s] [%s] [%s] %s",
                LocalDateTime.now().format(TIMESTAMP), level, module, message);
        if (latestWriter  != null) latestWriter.println(line);
        if (archiveWriter != null) archiveWriter.println(line);
    }

    private static String abbreviate(String s, int maxLen) {
        if (s == null) return "";
        return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
    }
}
