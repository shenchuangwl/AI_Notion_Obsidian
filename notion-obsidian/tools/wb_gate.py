#!/usr/bin/env python3
"""Human gate for write-back decisions (IMPL_CONTRACT §2 D-05, §5.4; WRITEBACK_SPEC §10).

    tty = require_human("apply")      # raises GateRefused unless a person at a real terminal runs this
    code = challenge_code()           # fresh 6-char code, shown only on /dev/tty
    ok = confirm_phrase(phrase, lines)
    ok = confirm_yes_no("拒绝这些项？")

Rule (D-05): refuse unless stdin AND stdout are TTYs AND /dev/tty opens; refuse if any of
CLAUDECODE, AI_AGENT, CI, GITHUB_ACTIONS is set or any variable name starts with CLAUDE_CODE_.
INVOCATION_ID / JOURNAL_STREAM are deliberately NOT refused (desktop terminals may carry them;
timers have no TTY anyway). The answer is always read from /dev/tty, never from stdin.

Everything written to /dev/tty goes through display_safe(): C0/C1 control characters (ESC, CR,
LF inside an item, DEL, ...), Unicode line/paragraph separators and bidi overrides are shown as
visible escapes, so a page title or path can never move the cursor, erase or reorder lines of
the list being approved.

There is no bypass: no flag, argument, environment variable or file can skip these checks.
Code paths that must run without a person (tests of the executor) call the internal executor with
an explicit confirmation record instead of going through this module.
"""
import os
import secrets
import sys
import unicodedata

AGENT_ENV_VARS = ("CLAUDECODE", "AI_AGENT", "CI", "GITHUB_ACTIONS")
AGENT_ENV_PREFIXES = ("CLAUDE_CODE_",)
YES_ANSWERS = ("y", "yes", "是")
_TTY = "/dev/tty"
# bidi embedding/override/isolate marks (can visually reorder an approval line)
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


class GateRefused(Exception):
    """The human gate refused; `reason` is a Chinese explanation for the terminal."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _unsafe_char(ch):
    if ch == "\t":
        return False
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI


def display_safe(text) -> str:
    """`text` (str() of non-strings) with terminal-unsafe characters shown as \\xNN / \\uNNNN.

    Escaped: every control character except TAB (C0 incl. CR and LF, DEL, C1), U+2028/U+2029 and
    bidi embedding/override/isolate marks. Everything else (CJK, emoji, ...) is unchanged."""
    s = text if isinstance(text, str) else str(text)
    if not any(_unsafe_char(ch) for ch in s):
        return s
    return "".join((f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}") if _unsafe_char(ch) else ch
                   for ch in s)


def agent_env_markers(env=None) -> list:
    """Names of environment variables that mark an automated or AI-agent session (sorted)."""
    env = os.environ if env is None else env
    return sorted(k for k in env if k in AGENT_ENV_VARS or k.startswith(AGENT_ENV_PREFIXES))


def _stream_is_tty(stream):
    try:
        return stream is not None and stream.isatty() and os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def require_human(action: str) -> str:
    """Return the terminal name (e.g. /dev/pts/3) or raise GateRefused (D-05)."""
    action = display_safe(action)
    marks = agent_env_markers()
    if marks:
        raise GateRefused(
            f"拒绝执行「{action}」：检测到自动化/AI 代理环境变量 {', '.join(marks)}。"
            "此操作只能由用户本人在普通交互终端中运行（不要在 Claude Code、CI、定时器或脚本中运行；"
            "若终端由带 Claude 扩展的编辑器启动，请改用独立终端）。")
    if not (_stream_is_tty(sys.stdin) and _stream_is_tty(sys.stdout)):
        raise GateRefused(
            f"拒绝执行「{action}」：标准输入和标准输出必须都是交互终端（检测到管道、重定向或 here-doc）。"
            "请在终端中直接运行，不要通过管道或脚本调用。")
    try:
        fd = os.open(_TTY, os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        raise GateRefused(f"拒绝执行「{action}」：无法打开 /dev/tty（{exc.strerror or exc}），当前进程没有控制终端。") from None
    os.close(fd)
    try:
        return os.ttyname(sys.stdin.fileno())
    except OSError:
        return _TTY


def challenge_code() -> str:
    """6 uppercase hex characters from `secrets` (never stored in plan.json or preview.md)."""
    return secrets.token_hex(3).upper()


def _norm(s):
    """NFKC + collapse whitespace: full-width spaces/letters typed via an IME compare equal."""
    return " ".join(unicodedata.normalize("NFKC", s).split())


def confirm_phrase(expected_phrase: str, lines_to_show: list) -> bool:
    """Show `lines_to_show` and the phrase on /dev/tty, read one line from /dev/tty, compare.

    Match = equal after strip (plus NFKC and whitespace collapsing). Runs the require_human
    checks first (raises GateRefused). Returns False on mismatch or EOF.
    Each element of `lines_to_show` is printed as ONE line through display_safe(); a phrase that
    contains terminal-unsafe characters is refused with ValueError (it could not be typed back).
    """
    require_human("确认短语")
    if not isinstance(expected_phrase, str) or not _norm(expected_phrase):
        raise ValueError("expected_phrase must be a non-empty string")
    if display_safe(expected_phrase) != expected_phrase:
        raise ValueError("expected_phrase contains control characters")
    shown = [display_safe(ln) for ln in (lines_to_show or [])]
    import termios
    with open(_TTY, "r", encoding="utf-8", errors="replace") as rd, \
            open(_TTY, "w", encoding="utf-8", errors="replace") as wr:
        try:
            termios.tcflush(rd.fileno(), termios.TCIFLUSH)  # drop type-ahead before the prompt
        except termios.error:
            pass
        for ln in shown:
            wr.write(f"{ln}\n")
        wr.write(f"\n请原样输入下面这一行以确认（输入其他内容即取消）：\n  {expected_phrase}\n> ")
        wr.flush()
        ans = rd.readline()
        ok = ans != "" and _norm(ans) == _norm(expected_phrase)
        wr.write("短语一致。\n" if ok else "短语不符，已取消。\n")
        wr.flush()
    return ok


def confirm_yes_no(question: str) -> bool:
    """Ask on /dev/tty; True only for y / yes / 是. Runs the require_human checks first."""
    require_human("确认")
    question = display_safe(question)
    import termios
    with open(_TTY, "r", encoding="utf-8", errors="replace") as rd, \
            open(_TTY, "w", encoding="utf-8", errors="replace") as wr:
        try:
            termios.tcflush(rd.fileno(), termios.TCIFLUSH)
        except termios.error:
            pass
        wr.write(f"{question} [y/N] ")
        wr.flush()
        ans = rd.readline()
    return _norm(ans).lower() in YES_ANSWERS
