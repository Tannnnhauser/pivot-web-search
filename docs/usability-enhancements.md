---
name: Usability enhancement proposals
description: 7 usability problems with three-agent enhancement proposals, pending discussion and implementation decisions
type: project
---

# Usability Enhancement Proposals (2026-04-30)

7 usability problems identified through self-review, with solutions proposed by Codex, Gemini, and Claude agents.

## Status: Decisions Complete

---

## 1. Python Environment Dependency

**Problem:** Users need Python 3.10+. Older system Python causes unfriendly errors. No Docker/binary option.

**Proposed solutions:**
- A) `uv run` launcher in `.mcp.json` — auto-downloads correct Python (Gemini + Claude)
- B) `scripts/run-server.py` preflight script checking version + deps (Codex)
- C) Dockerfile for zero-host-dep installs (Gemini)

**Decision:** Use `uv` as the launcher in `.mcp.json` (`"command": "uv", "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "pivot_web_search_mcp"]`). Prerequisites require uv. No wrapper script fallback — keep it clean. uv is the Python ecosystem standard; if a user doesn't have it, they're not the target audience.

---

## 2. Startup Delay

**Problem:** SessionStart health check takes 5-10s every session, even if user won't search. DDG timeout worsens it.

**Proposed solutions:**
- A) Remove SessionStart hook entirely, lazy check on first search (Gemini + Claude)
- B) Keep hook but slim it to liveness-only, no DDG/proxy probing (Codex)
- C) Add `WebSearchConfig action="doctor"` for manual deep check (Codex)

**Decision:** Add `"async": true` to the SessionStart hook. Without it the hook blocks session startup for up to 10s (our timeout). Making it async lets users interact immediately while health check runs in background. No need for `asyncRewake` — health check results are informational, not actionable; real errors surface naturally when the user actually searches.

---

## 3. Hook Fragility

**Problem:** PreToolUse hook depends on Claude Code's hook system. Format changes or conflicts = silent breakage.

**Proposed solutions:**
- A) Version guard: if JSON parse fails or format unrecognized, exit 0 (allow) instead of exit 2 (All three)
- B) Downgrade to soft warning instead of hard block (Codex)
- C) Document how to disable hook manually (Claude)

**Decision:** A — fail-open。Hook 脚本加 try-catch，JSON 解析失败或格式不认识时 exit 0（放行）。确保即使 Claude Code 更新了 hook 输入格式，也不会误阻断其他工具。

---

## 4. Debugging Difficulty

**Problem:** MCP server via stdio swallows errors. If deps missing or server crashes, user sees only "tool unavailable."

**Proposed solutions:**
- A) Persistent log at `~/.cache/pivot-web-search/server.log` (All three)
- B) `scripts/diagnose.sh` — checks Python version, imports, MCP handshake (Gemini + Claude)
- C) `WebSearchConfig status` reports Python version, import failures, log path (Codex)
- D) `PIVOT_WEB_SEARCH_DEBUG=1` env var for verbose mode (Claude)

**Decision:** D + A 组合。默认不记日志；用户设置 `PIVOT_WEB_SEARCH_DEBUG=1` 环境变量后，verbose 日志写到 `~/.cache/pivot-web-search/server.log`。零侵入，只有主动排查时才产生日志文件。

---

## 5. DDG Reliability

**Problem:** DDG is the only free default. Frequent timeouts/403s mean "zero-config" users get poor experience.

**Proposed solutions:**
- A) Failure cooldown: 3 consecutive fails → demote DDG for rest of session (Codex + Claude)
- B) Reduce DDG timeout to 3s (Gemini + Claude)
- C) Add public SearXNG as "free tier 1.5" fallback (Gemini)
- D) Surface recommendation to configure free Brave key (Gemini + Claude)

**Decision:** A + D。技术层面：连续 3 次失败后本会话内降级 DDG（超时保持 10s 不变）。文档层面：README 里强烈建议配置至少一个免费 API key——Tavily（1000 credits/月，无需信用卡）或 Brave（1000 queries/月，需信用卡但可设 hard limit 不会超额）。DDG 定位为 fallback，不是主力。

---

## 6. Too Many Config Layers

**Problem:** userConfig env → YAML → defaults = 3 layers. Debugging which is active is painful.

**Proposed solutions:**
- A) `WebSearchConfig status` shows effective value + source annotation (env/yaml/default) for each setting (All three)
- B) Refactor loaders into one unified resolver object (Codex)

**Decision:** A。`WebSearchConfig status` 增强输出，每个配置项显示生效值 + 来源：环境变量标变量名，YAML 标完整文件路径+行号，默认值标 default。B（重构 resolver）不做，过度设计。

---

## 7. All Providers Unavailable — Poor Error Experience

**Problem:** MCP server running but all providers unavailable (DDG blocked, API keys expired, quota exhausted). User gets vague errors, indistinguishable from server crash.

**Proposed solutions:**
- A) Sentinel/heartbeat file for hook fallback (original proposals — too heavy for edge case)
- B) Clear actionable error message when all providers fail: list each provider's failure reason + suggest next steps ("run WebSearchConfig status" or "configure a valid API key")

**Decision:** B。搜索全部失败时，返回结构化的错误信息：列出每个 provider 的具体失败原因（超时/key 过期/quota 耗尽）+ 建议操作。让用户能自己定位问题。
